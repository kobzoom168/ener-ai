"""ener-vdo: turn a news item into a short funny Thai video (render core, v1).

Pipeline: OpenRouter comedy-Thai script -> gTTS voice -> timed ASS captions ->
FFmpeg vertical 1080x1920 compose -> MP4. The caller (endpoint) sends it to
Telegram for preview/approval. No auto-posting here.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import re
import subprocess
import time

from app.agents.channels import ChannelProfile, RETENTION_CORE, get_profile

VDO_DIR = "/app/data/vdo"
SCRIPT_MODEL = "minimax/minimax-m3"  # picks real documented mysteries; cheap, engaging Thai
# Researcher agent uses a web-search model so facts are grounded in real sources (not the
# writer model's memory). Falls back to SCRIPT_MODEL if the search model isn't available.
RESEARCH_MODEL = os.environ.get("VDO_RESEARCH_MODEL", "perplexity/sonar")
# Per-channel history of recent clips (for the Originality Guard / Angle Diversifier).
_CLIP_HISTORY_KEY = "vdo_clip_history"
# Color grade applied to every clip (ffmpeg eq). Brighter + more vivid than before
# (was brightness=-0.19 = quite dark). Tune via env VDO_EQ if needed.
VDO_EQ = os.environ.get("VDO_EQ", "brightness=-0.02:saturation=1.22:contrast=1.05")

# Default model per crew agent. Each is overridable live from the Auto Post UI
# (stored as config key vdo_model_<agent>); _agent_model() reads the override with fallback.
AGENT_MODELS = {
    "trend_scout": RESEARCH_MODEL,   # picks a fresh, real topic (web search) when none is given
    "researcher": RESEARCH_MODEL,
    "scriptwriter": SCRIPT_MODEL,
    "fact_qc": SCRIPT_MODEL,
    "retention_qc": SCRIPT_MODEL,
    "compliance": SCRIPT_MODEL,
    "director": SCRIPT_MODEL,        # plans per-beat shots (still vs stock vs AI video)
    "analyst": SCRIPT_MODEL,         # reads YouTube Analytics → advice (activates with phase ②)
}


async def _agent_model(key: str) -> str:
    """Resolve which model a crew agent uses: live config override → built-in default."""
    try:
        from app.core.database import get_config
        v = (await get_config(f"vdo_model_{key}", "")).strip()
        if v:
            return v
    except Exception:
        pass
    return AGENT_MODELS.get(key, SCRIPT_MODEL)

# Narration tone presets for the mystery script (chosen in the Auto Post UI).
TONE_GUIDE = {
    # โทน = 'สไตล์การพูด' (มุมเนื้อหาโชคลาภ/ขอพร มาจากช่อง+กฎความเชื่ออยู่แล้ว)
    "duan": "สไตล์: ดุดัน เร้าใจ กระแทก เสียงหนักแน่น เร่งเร้า ปลุกให้เชื่อและทำตามทันที เหมือนเซียนมูที่มั่นใจสุดๆ",
    "chill": "สไตล์: สบายๆ เป็นกันเอง พูดเล่นๆ เหมือนเพื่อนสายมูเล่าให้ฟังชิลๆ แต่ยังน่าเชื่อ",
    "serious": "สไตล์: จริงจัง น้ำเสียงหนักแน่นน่าเชื่อถือ เล่าเหมือนเรื่องจริงที่คุณต้องฟัง ไม่ตลก",
    "raw": "สไตล์: ดิบ ตรงๆ ภาษาบ้านๆ แรงๆ แบบสายมูดุ ใช้ กู/มึง/คำแรงได้เพื่ออารมณ์ "
           "(ห้ามเหยียด/ดูถูก/ด่าคนดู/hate) กระแทกใจ จำง่าย",
}
_DEFAULT_TONE = "duan"


def _tone_guide(tone: str) -> str:
    if (tone or "") in ("", "random"):  # สุ่มสไตล์การพูดต่อคลิป
        import random as _r
        tone = _r.choice(list(TONE_GUIDE))
    return TONE_GUIDE.get(tone, TONE_GUIDE[_DEFAULT_TONE])


# --- Retention script engine -------------------------------------------------
# Proven stop-scroll hook patterns. The writer brainstorms 3 in its head and commits
# the strongest as the very first line (the 2-second make-or-break).
HOOK_ARCHETYPES = (
    "คลังแบบฮุค (วิแรกชี้เป็นชี้ตาย) — คิดในใจ 3 แบบ แล้วเลือกอันที่ 'หยุดนิ้ว' ที่สุดมาเป็นประโยคแรก:\n"
    "1) คำถามค้างใจ — ถามสิ่งที่คนอยากรู้คำตอบทันที (เช่น คุณรู้ไหมว่า … จริงๆ แล้วคืออะไร?)\n"
    "2) พลิกความเชื่อผิด — สิ่งที่คนเข้าใจมาตลอดมัน 'ผิด' (เช่น … ไม่ใช่อย่างที่คุณคิดเลย)\n"
    "3) ข้ออ้างเจาะจงน่าทึ่ง — ปี/ตัวเลข/สถานที่จริงที่สะดุด (เช่น ปี 2520 ที่วัด… เคยเกิดเรื่องที่…)\n"
    "4) คำเตือน/ต้องห้าม — สร้างความเสี่ยง (เช่น อย่าเพิ่งเลื่อนผ่าน / ห้ามพก…ถ้ายังไม่รู้สิ่งนี้)\n"
    "5) โยนเข้ากลางฉาก — เริ่มกลางเหตุการณ์ทันที (เช่น ตอนตีสาม เสียงสวดดังขึ้นจาก…)"
)

# The viral short skeleton. Each beat maps to a rough time window of a ~30s clip.
SCRIPT_BEATS = (
    "โครงบท (เขียนให้ครบทุกช่วง เรียงตามนี้ ประโยคสั้น พูดลื่น):\n"
    "• ฮุค (วิ 0-2): 1 ประโยค หยุดนิ้วทันที — ใช้แบบฮุคที่เลือก\n"
    "• ปมค้าง (วิ 2-5): 1 ประโยค ปักคำถาม/ความลับที่ 'ยังไม่เฉลย' ให้คนอยากรู้คำตอบ\n"
    "• ปูเรื่อง (วิ 5-18): 2-4 ประโยค สร้างบรรยากาศขลัง + อ้างที่มาเจาะจง 1 จุด (ชื่อ/ปี/สถานที่/ตำนาน) ให้ดูค้นคว้ามาจริง\n"
    "• จุดพีค (วิ 18-25): 1-2 ประโยค เฉลยปมที่ค้างไว้ จุดที่คนดูอึ้ง/ขนลุก/ร้องเฮ้ย\n"
    "• ข้อคิด+ชวนคุย (วิ 25-30): 1 ประโยคข้อคิด/กรรม/ความเชื่อ + 1 ประโยคชวนคอมเมนต์ "
    "(เช่น คุณเคยเจอแบบนี้ไหม คอมเมนต์เล่าให้ฟังหน่อย)"
)

_ASS_HEADER = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "PlayResX: 1080\n"
    "PlayResY: 1920\n"
    "WrapStyle: 2\n"
    "ScaledBorderAndShadow: yes\n\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, "
    "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
    "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Garuda,74,&H00FFFFFF,&H00111111,&H64000000,-1,0,0,0,100,100,2,0,1,5,3,2,70,70,330,0\n"
    "Style: Title,Garuda,86,&H0000F0FF,&H00000000,&H64000000,-1,0,0,0,100,100,1,0,1,7,4,8,60,60,180,0\n"
    # Cover = big bold viral-style headline that stays on top the whole clip (white base,
    # keyword recolored yellow inline); thick black outline so it reads on any background.
    "Style: Cover,Garuda,134,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,112,1,0,1,11,6,8,36,36,470,0\n"
    "Style: Brand,Garuda,40,&H00FFFFFF,&H00111111,&H64000000,-1,0,0,0,100,100,1,0,1,2,2,7,40,40,60,0\n"
    # BigMark = big faded anti-theft watermark pinned the whole clip (alpha-A0 = ~63% transparent
    # white). Alignment 8 = top-center, MarginV 120 → sits near the top per the user's request.
    "Style: BigMark,Garuda,88,&HA0FFFFFF,&HB0000000,&H00000000,-1,0,0,0,100,100,3,0,1,2,0,8,40,40,120,0\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

# Brand watermark: handle (green) + website (white). Set both to "" to disable.
VDO_BRAND_HANDLE = os.environ.get("VDO_BRAND_HANDLE", "@ener")
VDO_BRAND_WEB = os.environ.get("VDO_BRAND_WEB", "my-ener.uk")
_BRAND_GREEN = "&H5EC522&"  # ASS BGR for #22c55e
_BRAND_WHITE = "&HFFFFFF&"


async def _or_chat(model: str, system: str, prompt: str, max_tokens: int = 700) -> str:
    from app.core.openrouter_client import openrouter_chat_completions
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    try:
        d = await openrouter_chat_completions(model, msgs, max_tokens=max_tokens)
        return str(((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except Exception:
        return ""


def _parse_json(raw: str) -> dict:
    if not raw or not raw.strip():
        return {}
    txt = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).replace("```", "").strip()
    s = txt.find("{")
    if s == -1:
        return {}
    # Extract the FIRST balanced {...} object. Reasoning models (MiniMax M3) sometimes emit
    # the answer twice ({obj}{obj}); a naive first-{-to-last-} span would join both into
    # invalid JSON. Scan brace depth (string-aware) to take just the first complete object.
    depth = in_str = esc = 0
    for i in range(s, len(txt)):
        c = txt[i]
        if in_str:
            if esc:
                esc = 0
            elif c == "\\":
                esc = 1
            elif c == '"':
                in_str = 0
            continue
        if c == '"':
            in_str = 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    d = _json.loads(txt[s:i + 1])
                    if isinstance(d, dict):
                        return d
                except Exception:
                    pass
                break
    # fallback: original first-to-last span (handles minor noise inside one object)
    e = txt.rfind("}")
    if e > s:
        try:
            d = _json.loads(txt[s:e + 1])
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


async def generate_script(title: str, summary: str) -> dict:
    system = (
        "คุณคือนักเขียนบทคลิปสั้นข่าวสายฮา พูดไทยกวนๆ เป็นกันเอง ทำให้คนดูอมยิ้ม "
        "เขียนบทพากย์สำหรับคลิปแนวตั้ง 30-45 วินาที ห้ามหยาบคาย ห้ามดูถูกใคร "
        "ห้ามใส่เครื่องหมายคำพูด \" \" หรือ ' ' ในบทพากย์ ตอบ JSON เท่านั้น"
    )
    prompt = (
        f"ข่าว: {title}\nรายละเอียด: {summary}\n\n"
        "เขียนบทพากย์ไทยแนวตลกเบาๆ สำหรับคลิปสั้น:\n"
        "- เปิดด้วย hook สะดุดหู 1 ประโยคใน 3 วิแรก\n"
        "- เล่าข่าวแบบกวนๆ 3-5 ประโยคสั้น (ประโยคละ 1 บรรทัด)\n"
        "- ปิดด้วยมุก/คอมเมนต์ฮาๆ 1 ประโยค\n"
        'ตอบ JSON เท่านั้น: {"lines": ["ประโยคสั้นๆ", "..."], "caption": "แคปชั่นโพสต์สั้น + #แฮชแท็ก"}'
    )
    data = _parse_json(await _or_chat(SCRIPT_MODEL, system, prompt, 4000))
    lines = [_strip_quotes(str(x)) for x in (data.get("lines") or []) if str(x).strip()][:8]
    lines = [x for x in lines if x]
    if not lines:
        lines = [title]
    caption = str(data.get("caption") or title).strip()[:300]
    return {"lines": lines, "caption": caption}


def _synth_voice(text: str, out_path: str) -> str:
    """TTS to MP3. Uses the cloned ElevenLabs voice if configured, else gTTS (fail-open)."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if key and voice:
        try:
            import httpx
            model = os.environ.get("ELEVENLABS_MODEL", "eleven_v3")
            r = httpx.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.8, "style": 0.0},
                },
                timeout=120,
            )
            if r.status_code < 300 and r.content:
                with open(out_path, "wb") as fh:
                    fh.write(r.content)
                return out_path
        except Exception:
            pass  # fall through to gTTS
    from gtts import gTTS
    gTTS(text=text, lang="th").save(out_path)
    return out_path


def _audio_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "0").strip())
    except Exception:
        return 0.0


def _ass_ts(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return (text or "").replace("\\", " ").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


# Quotes make the narration read like an AI (and look odd on screen) — strip them out.
_QUOTES = "\"“”„«»‘’‹›`'"


def _strip_quotes(s: str) -> str:
    return (s or "").translate({ord(c): None for c in _QUOTES}).strip()


# Thai has no spaces, so a naive char-cut orphans trailing vowels/tone marks on the next
# line (e.g. "เทพีบ" / "าสเทต"). These chars must never START a line; เแโใไ must never END one.
_NO_LINE_START = set("ะาำิีึืุู็่้๊๋์ํัๆๅฯๆ.,!?)]}")
_NO_LINE_END = set("เแโใไ([{")


def _cluster_cut(s: str, max_chars: int) -> int:
    """Pick a cut index <= max_chars that doesn't split a Thai vowel/tone cluster."""
    cut = max_chars
    while cut > 1 and (s[cut] in _NO_LINE_START or s[cut - 1] in _NO_LINE_END):
        cut -= 1
    return cut if cut > 1 else max_chars


def _wrap_rows(text: str, max_chars: int = 26) -> list[str]:
    """Split a line into display rows (Thai word-boundary aware), returned as a list.

    Best: pythainlp word segmentation -> break at real Thai word boundaries.
    Fallback (pythainlp absent): cluster-safe char cut so a break never orphans a Thai
    vowel/tone-mark from the consonant it attaches to.
    """
    s = (text or "").strip()
    if not s:
        return []
    if len(s) <= max_chars:
        return [s]

    words = None
    try:
        from pythainlp.tokenize import word_tokenize
        words = [w for w in word_tokenize(s, engine="newmm") if w]
    except Exception:
        words = None

    out: list[str] = []
    if words:
        # Balance rows so the last line isn't a lonely orphan: aim for even widths.
        total = sum(len(w) for w in words if w.strip())
        rows = max(1, (total + max_chars - 1) // max_chars)
        target = total / rows
        line = ""
        for w in words:
            if not w.strip():  # whitespace token — keep attached
                line += w
                continue
            if w and all(c in _NO_LINE_START for c in w):  # ๆ ฯ . , — glue to prev line
                line += w
                continue
            if len(w) > max_chars:  # single overlong word: cluster-safe split it
                if line.strip():
                    out.append(line.strip())
                    line = ""
                while len(w) > max_chars:
                    c = _cluster_cut(w, max_chars)
                    out.append(w[:c].strip())
                    w = w[c:]
                line = w
                continue
            if not line.strip():
                line = w
            elif len((line + w).strip()) > max_chars:
                out.append(line.strip())
                line = w
            elif len(line.strip()) >= target and len(out) < rows - 1:
                out.append(line.strip())  # break early to keep rows even
                line = w
            else:
                line += w
        if line.strip():
            out.append(line.strip())
        return out

    while len(s) > max_chars:
        sp = s.rfind(" ", 0, max_chars + 1)
        cut = sp if sp > max_chars // 2 else _cluster_cut(s, max_chars)
        out.append(s[:cut].strip())
        s = s[cut:].lstrip()
    if s.strip():
        out.append(s.strip())
    return out


def _wrap_thai(text: str, max_chars: int = 26) -> str:
    """Wrap a line to the video width using ASS \\N breaks (rows joined)."""
    return "\\N".join(_wrap_rows(text, max_chars))


def _concat_audio(parts: list[str], out_path: str) -> bool:
    """Concatenate mp3 segments into one mp3 (re-encoded for gapless joins)."""
    parts = [p for p in parts if p and os.path.exists(p)]
    if not parts:
        return False
    cmd = ["ffmpeg", "-y"]
    for p in parts:
        cmd += ["-i", p]
    n = len(parts)
    fc = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
    cmd += ["-filter_complex", fc, "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "4", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode == 0 and os.path.exists(out_path)
    except Exception:
        return False


def _synth_lines(lines: list[str], base: str, out_mp3: str,
                 display_lines: list[str] | None = None) -> tuple[list[tuple[str, float]], float]:
    """Synthesize each line separately → measure → concat into out_mp3.

    Per-line timing is what lets the subtitle for each line appear exactly while that line
    is spoken (one line at a time), instead of an even split that drifts off the audio.

    `lines` is what ElevenLabs SPEAKS (phonetic re-spelling so Thai สายมู terms are read
    correctly). `display_lines`, when given, is the grammatically-correct subtitle text shown
    on screen for the matching segment. The voice uses `lines`, the caption uses `display_lines`.
    Returns ([(caption_line, duration)…], total_duration).
    """
    segs: list[tuple[str, float]] = []
    parts: list[str] = []
    for i, ln in enumerate(lines):
        ln = (ln or "").strip()
        if not ln:
            continue
        p = f"{base}_seg{i}.mp3"
        _synth_voice(ln, p)
        d = _audio_duration(p)
        if d <= 0:
            try:
                os.remove(p)
            except Exception:
                pass
            continue
        parts.append(p)
        shown = ln
        if display_lines and i < len(display_lines) and (display_lines[i] or "").strip():
            shown = display_lines[i].strip()
        segs.append((shown, d))
    if not parts:
        return [], 0.0
    _concat_audio(parts, out_mp3)
    for p in parts:
        try:
            os.remove(p)
        except Exception:
            pass
    total = _audio_duration(out_mp3) or sum(d for _, d in segs)
    return segs, total


def _render_cover(bg_image: str, cover_text: str, cover_highlight: str, out_jpg: str) -> str:
    """Render a vertical YouTube COVER/thumbnail: bg image + BIG CENTERED title (white with a
    yellow keyword, thick outline). One ffmpeg frame → jpg. Returns out_jpg or '' on failure."""
    import subprocess
    tc = _ass_escape((cover_text or "").strip())
    if not tc or not bg_image or not os.path.exists(bg_image):
        return ""
    rows = _wrap_rows(tc, 10)[:3] or [tc]
    joined = "\\N".join(rows)
    hl = _ass_escape((cover_highlight or "").strip())
    if hl and hl in joined:
        joined = joined.replace(hl, "{\\c&H00F0FF&}" + hl + "{\\c&H00FFFFFF&}", 1)
    ass = out_jpg + ".ass"
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # huge, bold, dead-center, very thick black outline so it pops as a thumbnail
        "Style: CoverBig,Garuda,140,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,110,2,0,1,13,6,5,50,50,40,0\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    try:
        with open(ass, "w", encoding="utf-8") as f:
            f.write(header + f"Dialogue: 0,0:00:00.00,0:00:05.00,CoverBig,,0,0,0,,{joined}\n")
        cmd = ["ffmpeg", "-y", "-i", bg_image, "-vf",
               ("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "eq=brightness=-0.05:saturation=1.12,subtitles=" + ass.replace("\\", "/").replace(":", "\\:")
                if os.name == "nt" else
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                "eq=brightness=-0.05:saturation=1.12,subtitles=" + ass),
               "-frames:v", "1", "-q:v", "3", out_jpg]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        ok = r.returncode == 0 and os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 2000
        # YouTube rejects thumbnails > 2MB → re-encode smaller until it fits (keeps the cover usable)
        for q in ("5", "7", "10"):
            if not ok or os.path.getsize(out_jpg) <= 1_950_000:
                break
            subprocess.run(["ffmpeg", "-y", "-i", out_jpg, "-q:v", q, out_jpg + ".s.jpg"],
                           capture_output=True, timeout=30)
            if os.path.exists(out_jpg + ".s.jpg") and os.path.getsize(out_jpg + ".s.jpg") > 2000:
                os.replace(out_jpg + ".s.jpg", out_jpg)
    except Exception:
        ok = False
    finally:
        try:
            os.remove(ass)
        except Exception:
            pass
    return out_jpg if ok else ""


def _build_ass(title: str, segments: list[tuple[str, float]], ass_path: str,
               brand: tuple[str, str] | None = None, title_card: str = "",
               cover_highlight: str = "") -> None:
    """Caption track: one short row on screen at a time, voice-synced.

    Each spoken line is timed to its real audio duration, then split into single display
    rows whose on-screen time is shared across the line's segment by row length — so the
    caption advances row-by-row in step with the narration (never a multi-row block).

    `brand` = (handle, web) watermark for this channel; falls back to the global default.
    Pass ("", "") to disable the watermark (e.g. an un-branded new channel).
    """
    total = sum(d for _, d in segments) or 1.0
    events = []
    # brand logo only (no topic title) as the persistent header
    b_handle, b_web = brand if brand is not None else (VDO_BRAND_HANDLE, VDO_BRAND_WEB)
    handle, web = _ass_escape(b_handle)[:20], _ass_escape(b_web)[:30]
    brand_parts = []
    if handle:
        brand_parts.append("{\\c" + _BRAND_GREEN + "}" + handle)
    if web:
        brand_parts.append("{\\c" + _BRAND_WHITE + "}" + web)
    if brand_parts:
        events.append(
            f"Dialogue: 0,{_ass_ts(0)},{_ass_ts(total)},Brand,,0,0,0,,{'  '.join(brand_parts)}"
        )
    # 🔒 Big faded anti-theft watermark (the website, centered, the WHOLE clip) so ripped re-uploads
    # always carry the brand. Layer 0 = under the captions; alpha set in the BigMark style.
    if web:
        events.append(
            f"Dialogue: 0,{_ass_ts(0)},{_ass_ts(total)},BigMark,,0,0,0,,{web}"
        )
    # big viral-style COVER headline pinned to the top for the WHOLE clip (white base with one
    # keyword recolored yellow) — doubles as the thumbnail hook and keeps people watching.
    tc = _ass_escape(title_card or title)
    if tc:
        tc_rows = _wrap_rows(tc, 12)[:2] or [tc]
        joined = "\\N".join(tc_rows)
        hl = _ass_escape((cover_highlight or "").strip())
        if hl and hl in joined:  # recolor the keyword yellow, rest stays white
            joined = joined.replace(hl, "{\\c&H00F0FF&}" + hl + "{\\c&H00FFFFFF&}", 1)
        # show only at the START (~3s) then fade out, so the rest of the clip is clean
        cover_end = min(3.2, max(2.0, total * 0.3))
        events.append(
            f"Dialogue: 1,{_ass_ts(0)},{_ass_ts(cover_end)},Cover,,0,0,0,,{{\\fad(300,400)}}{joined}"
        )
    t = 0.0
    for ln, d in segments:
        rows = _wrap_rows(_ass_escape(ln), 19) or [_ass_escape(ln)]
        chars = sum(len(r) for r in rows) or 1
        rt = t
        for j, row in enumerate(rows):
            rd = d * (len(row) / chars)
            st = rt
            en = (t + d) if j == len(rows) - 1 else (rt + rd)  # last row absorbs rounding
            events.append(f"Dialogue: 0,{_ass_ts(st)},{_ass_ts(en)},Default,,0,0,0,,{row}")
            rt = en
        t += d
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER + "\n".join(events) + "\n")


# One consistent "Style Bible" appended to EVERY image so the whole clip looks like one set
# (this is the cheap, high-impact cohesion lever the consult AIs all recommended).
_STYLE_BIBLE = (
    "Cinematic photorealistic film still, hyper-detailed, shot on 35mm, shallow depth of field, "
    "dramatic moody volumetric lighting, unified dark teal-and-amber color grade, rich contrast, "
    "atmospheric haze — one consistent art direction across the whole set (same palette, same "
    "lens, same mood, like frames from a single film). Vertical 9:16. NO text, no words, no "
    "letters, no captions, no watermark, no border")


def _img_style(prompt: str) -> str:
    # STYLE FIRST (Flux weights earlier tokens more → the look dominates), then the scene.
    return f"{_STYLE_BIBLE}. SCENE: {prompt}"


async def _gen_bg_image_gemini(prompt: str, idx: int = 0) -> str | None:
    """Generate the image via Google's Gemini API directly (free tier, GEMINI_API_KEY).
    Fail-open → None so the caller falls back to OpenRouter."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or not str(prompt).strip():
        return None

    def _do() -> bytes | None:
        from google import genai
        client = genai.Client(api_key=key)
        model = os.environ.get("VDO_GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
        resp = client.models.generate_content(model=model, contents=_img_style(prompt))
        for cand in (getattr(resp, "candidates", None) or []):
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                data = getattr(getattr(part, "inline_data", None), "data", None)
                if data:
                    return data
        return None

    try:
        img = await asyncio.to_thread(_do)
        if not img:
            return None
        os.makedirs(VDO_DIR, exist_ok=True)
        path = os.path.join(VDO_DIR, f"bg_{int(time.time() * 1000)}_{idx}.png")
        with open(path, "wb") as f:
            f.write(img)
        return path if os.path.getsize(path) > 2000 else None
    except Exception:
        return None


async def _gen_bg_image(prompt: str, idx: int = 0, seed: int | None = None) -> str | None:
    """Generate a topical 9:16 background image. Uses the free Gemini API when the image
    provider is set to 'gemini' (falls back to OpenRouter on any failure/limit), else OpenRouter.
    A shared `seed` across the clip keeps the visual look cohesive (same Set/tone)."""
    try:
        from app.core.database import get_config
        provider = (await get_config("vdo_image_provider", "")).strip()
    except Exception:
        provider = ""
    if provider == "fal_flux":
        try:
            from app.agents import aivideo
            # 💰 Eco mode → Flux dev (~฿7/clip) instead of Flux pro (~฿11); both keep the same
            # style-first + shared-seed cohesion and strong script adherence.
            from app.core.database import get_config as _gc
            eco = (await _gc("vdo_eco", "")).strip().lower() in ("1", "true", "on", "yes")
            mdl = "fal-ai/flux/dev" if eco else ""
            f = await aivideo.generate_image(_img_style(prompt),
                                             os.path.join(VDO_DIR, f"bg_{int(time.time() * 1000)}_{idx}.png"),
                                             model=mdl, seed=seed)
            if f:
                return f  # else fall through to OpenRouter
        except Exception:
            pass
    elif provider == "gemini":
        g = await _gen_bg_image_gemini(prompt, idx)
        if g:
            return g  # else fall through to OpenRouter (free tier hit a limit / errored)
    try:
        import base64 as _b64
        import httpx
        from app.core.openrouter_client import get_openrouter_api_key, openrouter_base_url
        key = await get_openrouter_api_key()
        if not key:
            return None
        body = {
            "model": os.environ.get("VDO_IMAGE_MODEL", "google/gemini-3.1-flash-image-preview"),
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": _img_style(prompt)}],
        }
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(openrouter_base_url() + "/chat/completions",
                             headers={"Authorization": "Bearer " + key}, json=body)
        if r.status_code >= 300:
            return None
        imgs = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("images") or []
        url = (imgs[0].get("image_url") or {}).get("url") if imgs else ""
        if not url or "base64," not in url:
            return None
        os.makedirs(VDO_DIR, exist_ok=True)
        path = os.path.join(VDO_DIR, f"bg_{int(time.time() * 1000)}_{idx}.png")
        with open(path, "wb") as f:
            f.write(_b64.b64decode(url.split("base64,", 1)[1]))
        return path
    except Exception:
        return None


async def _gen_bg_images(prompts: list[str], seed: int | None = None) -> list[str]:
    """Generate several bg images in parallel; returns the paths that succeeded. One shared
    `seed` makes the whole set cohesive (same look/tone)."""
    prompts = [p for p in (prompts or []) if str(p).strip()][:9]
    if not prompts:
        return []
    results = await asyncio.gather(*[_gen_bg_image(p, i, seed) for i, p in enumerate(prompts)])
    return [p for p in results if p]


# The recurring Cat-Cast mascot — one canonical design so every clip stars the SAME cat.
_CAT_MASCOT = ("a cute chubby anthropomorphic orange-and-white tabby cat character with big round "
               "expressive eyes and a friendly face, standing upright like a person, full body, "
               "plain soft neutral background")


async def _gen_bg_images_catlock(prompts: list[str], seed: int | None = None) -> list[str]:
    """🐱 Same-cat-every-scene: make ONE clean mascot anchor, then render each scene with fal Nano
    Banana 2 edit (a SEMANTIC edit that keeps the cat's identity but builds a brand-new scene from
    the prompt — unlike Redux which clones the whole frame). Fail-open per scene → plain text2img."""
    prompts = [p for p in (prompts or []) if str(p).strip()][:9]
    if not prompts:
        return []
    from app.agents import aivideo
    anchor = await _gen_bg_image(_CAT_MASCOT, 0, seed)  # the canonical cat portrait
    if not anchor:
        return await _gen_bg_images(prompts, seed)

    async def _scene(i: int, p: str) -> str | None:
        out = os.path.join(VDO_DIR, f"bg_{int(time.time() * 1000)}_{i}.png")
        edit_prompt = (
            "Keep the EXACT same cat character from the reference image — identical face, fur "
            "colors, markings, ears and body proportions. Put that same cat into a NEW scene: "
            f"{p}. Cute cartoon, cinematic, vertical 9:16. Do NOT copy the reference background "
            "or composition; build the new scene fresh. No text.")
        r = await aivideo.generate_image_edit(edit_prompt, [anchor], out, seed=seed)
        return r or await _gen_bg_image(p, i, seed)

    res = await asyncio.gather(*[_scene(i, p) for i, p in enumerate(prompts)])
    return [x for x in res if x]


async def _catlock_on() -> bool:
    """Cat-lock runs when Cat Cast is on AND fal is available (it needs Nano Banana edits)."""
    try:
        from app.core.database import get_config
        if (await get_config("vdo_cat_mode", "")).strip().lower() not in ("1", "true", "on", "yes"):
            return False
        from app.agents import aivideo
        if not aivideo._key():
            return False
        return (await get_config("vdo_image_provider", "")).strip() in ("", "fal_flux")
    except Exception:
        return False


async def _pexels_pick(query: str, key: str) -> dict | None:
    """Search Pexels for `query` and return a RANDOM good portrait mp4 (not always the same
    top result) so clips on similar topics don't reuse identical footage. Picks a random page
    + a random candidate among the portrait videos that fit 9:16."""
    import httpx
    import random
    target = 1920 / 1080  # h/w for 9:16 ≈ 1.778
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "size": "medium",
                    "per_page": 15, "page": random.randint(1, 3)},
        )
    if r.status_code >= 300:
        return None
    candidates = []
    for v in (r.json().get("videos") or []):
        vw, vh = v.get("width") or 0, v.get("height") or 0
        if (v.get("duration") or 0) < 2 or vw <= 0 or vh < vw:
            continue
        files = [f for f in (v.get("video_files") or [])
                 if f.get("file_type") == "video/mp4" and f.get("link")
                 and (f.get("height") or 0) >= (f.get("width") or 0)]  # portrait only
        if not files:
            continue
        files.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
        candidates.append((abs((vh / vw) - target), files[0]))
    if not candidates:
        return None
    # keep the ones reasonably close to 9:16, then pick one at random for variety
    candidates.sort(key=lambda x: x[0])
    pool = candidates[:6] if len(candidates) >= 6 else candidates
    return random.choice(pool)[1]


async def _pexels_pick_url(query: str, key: str) -> str | None:
    best = await _pexels_pick(query, key)
    return best["link"] if best else None


async def _pixabay_pick(query: str, key: str) -> str | None:
    """Pick a video mp4 URL from Pixabay (free, ~1.9M assets). Prefers portrait but accepts
    landscape (the renderer crops to 9:16). Random among the top hits for variety."""
    import httpx
    import random
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get("https://pixabay.com/api/videos/",
                        params={"key": key, "q": query, "per_page": 20, "safesearch": "true"})
    if r.status_code >= 300:
        return None
    portrait, other = [], []
    for h in (r.json().get("hits") or []):
        if (h.get("duration") or 0) < 2:
            continue
        vids = h.get("videos") or {}
        pick = None
        for size in ("large", "medium", "small", "tiny"):
            f = vids.get(size) or {}
            if f.get("url"):
                pick = f
                if (f.get("width") or 0) >= 720:
                    break
        if not pick or not pick.get("url"):
            continue
        (portrait if (pick.get("height") or 0) >= (pick.get("width") or 0) else other).append(pick["url"])
    pool = portrait or other
    return random.choice(pool[:8]) if pool else None


async def _coverr_pick(query: str, key: str) -> str | None:
    """Pick a video mp4 URL from Coverr (free). Best-effort / fail-open: if the API shape
    differs we just return None and the caller falls back to another provider or an image."""
    import httpx
    import random
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get("https://api.coverr.co/videos",
                        params={"query": query, "page_size": 20, "urls": "true", "api_key": key},
                        headers={"Authorization": f"Bearer {key}"})
    if r.status_code >= 300:
        return None
    data = r.json()
    hits = data.get("hits") or data.get("videos") or []
    urls = []
    for h in hits:
        u = h.get("urls") or {}
        link = u.get("mp4_download") or u.get("mp4") or u.get("mp4_preview")
        if link:
            urls.append(link)
    return random.choice(urls[:8]) if urls else None


async def _fetch_stock_video(query: str, idx: int = 0) -> str | None:
    """Find + download a real vertical stock clip for `query`. Fail-open.

    Tries every configured FREE provider (Pexels, Pixabay, Coverr) in random order — so
    footage varies and a wider library is searched — with a Thai-stripped retry. Returns
    a local mp4 path or None (caller then uses a fresh AI still instead).
    """
    import random
    query = (query or "").strip()
    if not query:
        return None
    broadened = re.sub(r"\b(thai|thailand|bangkok)\b", "", query, flags=re.IGNORECASE).strip()
    queries = [query] + ([broadened] if broadened and broadened.lower() != query.lower() else [])
    pexkey = os.environ.get("PEXELS_API_KEY", "").strip()
    pixkey = os.environ.get("PIXABAY_API_KEY", "").strip()
    covkey = os.environ.get("COVERR_API_KEY", "").strip()
    providers = []
    if pexkey:
        providers.append(("pexels", lambda q: _pexels_pick_url(q, pexkey)))
    if pixkey:
        providers.append(("pixabay", lambda q: _pixabay_pick(q, pixkey)))
    if covkey:
        providers.append(("coverr", lambda q: _coverr_pick(q, covkey)))
    if not providers:
        return None
    random.shuffle(providers)
    url = None
    for q in queries:
        for _name, pick in providers:
            try:
                url = await pick(q)
            except Exception:
                url = None
            if url:
                break
        if url:
            break
    if not url:
        return None
    try:
        import httpx
        os.makedirs(VDO_DIR, exist_ok=True)
        path = os.path.join(VDO_DIR, f"sv_{int(time.time())}_{idx}.mp4")
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code >= 300:
                    return None
                with open(path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
        return path if os.path.exists(path) and os.path.getsize(path) > 10000 else None
    except Exception:
        return None


async def _probe_pexels(queries: list[str]) -> dict[str, int]:
    """Footage-aware scripting: check Pexels UP FRONT for how many usable portrait clips
    each query has, so the builder uses real footage where it exists and auto-falls back
    to a fresh AI still where it doesn't. Fail-open.

    Returns {query: count}; count >0 = footage exists, 0 = confirmed none (→ use image),
    -1 = unknown (API error → still worth a real try later).
    """
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    uniq: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q.lower() not in {u.lower() for u in uniq}:
            uniq.append(q)
    if not key or not uniq:
        return {}
    import httpx

    async def _one(q: str) -> tuple[str, int]:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get("https://api.pexels.com/videos/search",
                                headers={"Authorization": key},
                                params={"query": q, "orientation": "portrait", "per_page": 5})
            if r.status_code >= 300:
                return q, -1
            vids = r.json().get("videos") or []
            return q, sum(1 for v in vids if (v.get("height") or 0) >= (v.get("width") or 0))
        except Exception:
            return q, -1

    return {q: c for q, c in await asyncio.gather(*[_one(q) for q in uniq])}


async def _fetch_stock_videos(queries: list[str]) -> list[str]:
    """Fetch several stock clips in parallel; returns the local paths that succeeded."""
    queries = [str(q).strip() for q in (queries or []) if str(q).strip()][:3]
    if not queries:
        return []
    results = await asyncio.gather(*[_fetch_stock_video(q, i) for i, q in enumerate(queries)])
    return [p for p in results if p]


# Background music: quiet bed mixed under the narration. Drop a file here (or set env).
BGM_PATH = os.environ.get("VDO_BGM_PATH", "/app/data/bgm/default.wav")
BGM_VOLUME = os.environ.get("VDO_BGM_VOLUME", "0.10")


def _audio_filter(voice_idx: int, bgm_idx: int | None, duration: float) -> tuple[str, str]:
    """Build the ffmpeg audio graph: narration full + BGM quiet underneath (fade out).

    Returns (filtergraph_or_empty, audio_map). amix halves levels, so we boost x2 after
    to keep the voice at full loudness with the music sitting low under it.
    """
    if bgm_idx is None:
        return "", f"{voice_idx}:a"
    fade_st = max(0.0, duration - 1.5)
    fc = (
        f"[{bgm_idx}:a]volume={BGM_VOLUME},afade=t=out:st={fade_st:.2f}:d=1.5[bgm];"
        f"[{voice_idx}:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[mix];"
        f"[mix]volume=2.0[aout]"
    )
    return fc, "[aout]"


def _render(audio_path: str, ass_path: str, duration: float, out_path: str,
            bg_images: list[str] | None = None,
            bg_videos: list[str] | None = None,
            bg_items: list[tuple[str, str]] | None = None,
            item_durations: list[float] | None = None) -> tuple[bool, str]:
    bgm = BGM_PATH if os.path.exists(BGM_PATH) else None
    fps = 25
    # normalize backgrounds into an ordered list of (path, kind) — videos and AI images
    # can be mixed in one clip (a slot with no stock video falls back to an image).
    items = list(bg_items or [])
    if not items:
        items = ([(p, "video") for p in (bg_videos or [])] +
                 [(p, "image") for p in (bg_images or [])])
    items = [(p, k) for (p, k) in items if p and os.path.exists(p)]

    if items:
        n = len(items)
        # sync each background to its line's spoken duration (item_durations 1:1 with items);
        # else fall back to an even split across the clip.
        if item_durations and len(item_durations) == n:
            durs = [max(0.3, float(d)) for d in item_durations]
        else:
            durs = [max(1.0, duration / n)] * n
        # Snap scene boundaries onto the audio timeline: each scene's frame count is taken
        # from the running cumulative total, so they sum EXACTLY to the audio length. No
        # per-scene min-length padding or int() truncation piles up — this kills the slow
        # drift where images lag behind the voice ("ภาพช้ากว่าเสียง").
        _tot = sum(durs) or 1.0
        _total_frames = max(n, int(round(duration * fps)))
        _acc, _prev, dframes_list = 0.0, 0, []
        for _d in durs:
            _acc += _d
            _cur = int(round(_acc / _tot * _total_frames))
            dframes_list.append(max(1, _cur - _prev))
            _prev = _cur
        cmd = ["ffmpeg", "-y"]
        for i, (p, k) in enumerate(items):
            if k == "image":
                # single frame in -> zoompan generates the motion (d frames). DON'T loop the
                # input: a looped multi-frame still makes zoompan explode frames so only the
                # first image ever shows.
                cmd += ["-i", p]
            else:
                cmd += ["-stream_loop", "-1", "-t", f"{dframes_list[i] / fps:.3f}", "-i", p]
        cmd += ["-i", audio_path]
        voice_idx, bgm_idx = n, None
        if bgm:
            cmd += ["-stream_loop", "-1", "-i", bgm]
            bgm_idx = n + 1
        chains = []
        for i, (p, k) in enumerate(items):
            dframes = dframes_list[i]  # exact frame budget for this scene (sums to audio len)
            if k == "image":  # smooth slow Ken Burns zoom-IN only. Big 2x upscale + tiny step
                # makes zoompan's per-frame rounding sub-pixel → no jitter/shake.
                chains.append(
                    f"[{i}:v]scale=2160:3840:force_original_aspect_ratio=increase,crop=2160:3840,"
                    f"zoompan=z='min(zoom+0.0006,1.18)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={dframes}:s=1080x1920:fps={fps},setsar=1[v{i}]"
                )
            else:  # real video -> fill 9:16
                chains.append(
                    f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    f"fps={fps},setpts=PTS-STARTPTS,setsar=1[v{i}]"
                )
        cat = "".join(f"[v{i}]" for i in range(n))
        fc = (";".join(chains) +
              f";{cat}concat=n={n}:v=1:a=0,eq={VDO_EQ},"
              f"subtitles={ass_path}[vout]")
        fc_a, amap = _audio_filter(voice_idx, bgm_idx, duration)
        if fc_a:
            fc += ";" + fc_a
        cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", amap,
                "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
               f"color=c=0x0f172a:s=1080x1920:d={duration:.2f}", "-i", audio_path]
        voice_idx, bgm_idx = 1, None
        if bgm:
            cmd += ["-stream_loop", "-1", "-i", bgm]
            bgm_idx = 2
        fc = f"[0:v]subtitles={ass_path}[vout]"
        fc_a, amap = _audio_filter(voice_idx, bgm_idx, duration)
        if fc_a:
            fc += ";" + fc_a
        cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", amap,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out_path):
            return True, "ok"
        return False, (r.stderr or "ffmpeg failed")[-500:]
    except Exception as exc:
        return False, str(exc)[:300]


def _overlay_pip(base_mp4: str, pip_mp4: str, out_path: str) -> tuple[bool, str]:
    """Overlay the talking head keeping its natural proportions (no squish), flush to the
    very bottom-left corner with no gap, sitting below the centred subtitle. Keeps base audio."""
    fc = "[1:v]scale=330:-2,setsar=1[pip];[0:v][pip]overlay=x=0:y=H-h[v]"
    cmd = ["ffmpeg", "-y", "-i", base_mp4, "-i", pip_mp4,
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "copy", "-shortest", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out_path):
            return True, "ok"
        return False, (r.stderr or "overlay failed")[-400:]
    except Exception as exc:
        return False, str(exc)[:300]


async def make_news_short(title: str, summary: str) -> dict:
    """News -> funny Thai short MP4. Returns {ok, mp4, caption, lines, error}."""
    os.makedirs(VDO_DIR, exist_ok=True)
    stamp = int(time.time())
    base = os.path.join(VDO_DIR, f"vdo_{stamp}")
    mp3, ass, mp4 = base + ".mp3", base + ".ass", base + ".mp4"

    script = await generate_script(title, summary)
    lines = script["lines"]

    try:
        segments, duration = await asyncio.to_thread(_synth_lines, lines, base, mp3)
    except Exception as exc:
        return {"ok": False, "error": f"TTS ล้มเหลว: {str(exc)[:200]}"}
    if not segments or duration <= 0:
        return {"ok": False, "error": "อ่านความยาวเสียงไม่ได้"}

    await asyncio.to_thread(_build_ass, title, segments, ass)
    ok, err = await asyncio.to_thread(_render, mp3, ass, duration, mp4)
    if not ok:
        return {"ok": False, "error": f"render ล้มเหลว: {err}"}

    # tidy intermediates (keep the mp4)
    for p in (mp3, ass):
        try:
            os.remove(p)
        except Exception:
            pass

    return {"ok": True, "mp4": mp4, "caption": script["caption"], "lines": lines,
            "duration": round(duration, 1)}


async def _render_clip(title: str, lines: list[str], bg_images: list[str] | None = None,
                       bg_videos: list[str] | None = None, face_pip: bool = False,
                       bg_items: list[tuple[str, str]] | None = None,
                       say_lines: list[str] | None = None,
                       brand: tuple[str, str] | None = None,
                       title_card: str = "", cover_highlight: str = "") -> dict:
    """Shared render: lines -> TTS -> ASS captions -> MP4 (stock-video or image slideshow).

    `lines` is shown on screen as the subtitle; `say_lines` (optional, 1:1 with lines) is the
    phonetic re-spelling ElevenLabs actually speaks so Thai สายมู terms are read correctly.

    If face_pip and a D-ID talking head can be made, the user's lip-synced face is
    overlaid bottom-left as a PIP (the narration mp3 is served publicly for D-ID to fetch).
    """
    os.makedirs(VDO_DIR, exist_ok=True)
    stamp = int(time.time())
    base = os.path.join(VDO_DIR, f"vdo_{stamp}")
    mp3, ass, mp4 = base + ".mp3", base + ".ass", base + ".mp4"
    bg_images = [p for p in (bg_images or []) if p]
    bg_videos = [p for p in (bg_videos or []) if p]
    bg_items = [it for it in (bg_items or []) if it and it[0]]
    if not bg_items:
        bg_items = [(p, "video") for p in bg_videos] + [(p, "image") for p in bg_images]
    if not lines:
        return {"ok": False, "error": "ไม่มีบทพากย์"}
    try:
        voice_lines = say_lines if (say_lines and len(say_lines) == len(lines)) else lines
        segments, duration = await asyncio.to_thread(
            _synth_lines, voice_lines, base, mp3, lines)
    except Exception as exc:
        return {"ok": False, "error": f"TTS ล้มเหลว: {str(exc)[:200]}"}
    if not segments or duration <= 0:
        return {"ok": False, "error": "อ่านความยาวเสียงไม่ได้"}
    await asyncio.to_thread(_build_ass, title, segments, ass, brand, title_card, cover_highlight)

    # talking-head PIP (optional): generate while the mp3 is still on disk + served publicly
    pip_video = None
    if face_pip:
        try:
            from app.agents import talkinghead
            if talkinghead.enabled():
                pip_video = await talkinghead.generate_talking_head(mp3, base + "_pip.mp4")
        except Exception:
            pip_video = None

    # sync backgrounds to lines when there's one bg per line (image mode) → image follows voice
    seg_durs = [d for _, d in segments]
    item_durs = seg_durs if len(bg_items) == len(seg_durs) else None
    render_target = (base + "_bg.mp4") if pip_video else mp4
    ok, err = await asyncio.to_thread(_render, mp3, ass, duration, render_target, None, None, bg_items, item_durs)
    if not ok and bg_items:
        # the slideshow/zoom render broke → retry plain solid so the clip still ships
        ok, err = await asyncio.to_thread(_render, mp3, ass, duration, render_target, None, None, None)
    if not ok:
        return {"ok": False, "error": f"render ล้มเหลว: {err}"}

    if pip_video:
        ok2, err2 = await asyncio.to_thread(_overlay_pip, render_target, pip_video, mp4)
        if not ok2:  # overlay failed → ship the plain clip
            try:
                os.replace(render_target, mp4)
            except Exception:
                mp4 = render_target

    for p in [mp3, ass, base + "_bg.mp4", pip_video] + [it[0] for it in bg_items]:
        if p and p != mp4:
            try:
                os.remove(p)
            except Exception:
                pass
    return {"ok": True, "mp4": mp4, "duration": round(duration, 1),
            "talking_head": bool(pip_video)}


def _sweep_intermediates(max_age_sec: int = 1800) -> None:
    """Delete leftover render intermediates (stock clips, bg images, _bg/_pip/_seg, ass)
    older than max_age_sec. The happy path already cleans these; this catches the ones a
    crashed/interrupted render leaves behind so data/vdo doesn't bloat. Never touches the
    final vdo_<stamp>.mp4 clips shown in the gallery."""
    try:
        now = time.time()
        for n in os.listdir(VDO_DIR):
            if re.fullmatch(r"vdo_\d+\.mp4", n):
                continue  # keep final clips
            is_junk = (n.startswith(("sv_", "bg_", "hero_")) or n.endswith(("_bg.mp4", "_pip.mp4", ".ass"))
                       or re.search(r"_seg\d+\.mp3$", n) or n.endswith(".mp3"))
            if not is_junk:
                continue
            p = os.path.join(VDO_DIR, n)
            try:
                if now - os.path.getmtime(p) > max_age_sec:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


async def _vlog(msg: str) -> None:
    try:
        from app.core.pipeline_status import log_line
        await log_line(msg)
    except Exception:
        pass


async def _research_topic(subject: str, profile: "ChannelProfile") -> str:
    """🔎 Researcher: ground the script in REAL, sourced facts (web-search model) so the
    writer can't fabricate. Asks for a source per fact + a clear 'verified vs belief' split.
    Falls back to the writer model if the search model isn't available on the account."""
    system = (profile.research_persona +
              " ค้นจากเว็บจริง แนบชื่อแหล่งที่มาสั้นๆ ต่อข้อเท็จจริง และแยกให้ชัดว่าข้อไหน "
              "'ยืนยันได้' กับข้อไหน 'เป็นความเชื่อ/เล่าต่อ' ห้ามแต่งแหล่งอ้างอิงปลอม")
    prompt = (profile.research_question.format(subject=subject) +
              "\n\nตอบเป็น bullet พร้อม (แหล่ง) ต่อข้อ และทำเครื่องหมาย [ยืนยันได้]/[ความเชื่อ] หน้าแต่ละข้อ")
    out = (await _or_chat(await _agent_model("researcher"), system, prompt, 2500)).strip()
    if not out:  # search model unavailable → degrade to the writer model (still better than nothing)
        out = (await _or_chat(await _agent_model("scriptwriter"), system, prompt, 2500)).strip()
    return out


async def _recent_clips(channel_id: str, n: int = 20) -> list[dict]:
    """Last N clips made for this channel — fuels the Originality Guard / Angle Diversifier."""
    try:
        from app.core.database import get_config
        raw = await get_config(_CLIP_HISTORY_KEY, "")
        data = _json.loads(raw) if raw else {}
        return (data.get(channel_id) or [])[:n]
    except Exception:
        return []


async def _record_clip(channel_id: str, entry: dict) -> None:
    """Append a freshly made clip's fingerprint (title/hook_type/angle/topic) to history."""
    try:
        from app.core.database import get_config, set_config
        raw = await get_config(_CLIP_HISTORY_KEY, "")
        data = _json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {}
        lst = data.get(channel_id) or []
        lst.insert(0, entry)
        data[channel_id] = lst[:40]
        await set_config(_CLIP_HISTORY_KEY, _json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


async def _retention_qc(lines: list[str], profile: "ChannelProfile", subject: str = "") -> tuple[list[str], str]:
    """🎯 Retention-QC (runs LAST): re-sharpen the script for retention AND topical coherence —
    one topic only (no unrelated tangents), specific hook→concrete payoff, no repeated filler."""
    if not lines:
        return lines, ""
    topic_line = f"หัวข้อของคลิปนี้คือ: '{subject}'. " if subject else ""
    system = (
        "คุณคือ QC ด้าน retention + ความสอดคล้องของเนื้อหาคลิปสั้น แก้บทให้คมขึ้นตามนี้: "
        f"{topic_line}"
        "(1) อยู่กับหัวข้อเดียว — ตัด/แก้ประโยคที่ 'หลุดเรื่อง' หรือลากเรื่องอื่นที่ไม่เกี่ยวมาปน "
        "(เช่น หัวข้อ UFO ห้ามมีผีปอบ/ผีฟ้าโผล่มา เว้นแต่เกี่ยวกันจริงและเชื่อมให้เนียน) "
        "(2) ฮุคประโยคแรกต้องปักคำถาม/ปม 'เฉพาะเจาะจง 1 ข้อ' ใส่หน้าเลย "
        "ห้ามขึ้นต้นด้วยทักทาย/เกริ่น (สวัสดี/วันนี้จะมาเล่า) — ถ้ามีให้แก้ทิ้ง "
        "(3) จุดพีคต้องเฉลยปมนั้นด้วยคำตอบ 'เป็นรูปธรรม' ไม่ใช่ลอยๆ ว่า 'อาจจะ…' "
        "(4) ต้องมี 1 'ประโยคแชร์' (insight ที่อยากส่งต่อ) ก่อน CTA; CTA ต้องชวนคอมเมนต์ประสบการณ์ ไม่ใช่แค่ 'กดติดตาม' "
        "(5) ตัดสำนวนซ้ำๆ ('ตามความเชื่อ' ซ้ำทุกบรรทัด) + ตัดช่วงปูเรื่องที่ยืด ให้แต่ละบรรทัดสั้น≤14 คำ พูดลื่น "
        "(6) ภาษาต้องเข้าใจง่ายมากๆ คนทั่วไปฟังแล้วเก็ตทันที — แก้ประโยคที่นามธรรม/กำกวม/ศัพท์ยากที่คนงง "
        "(เช่น 'โลกมีชั้นซ่อน', 'พื้นที่ว่างจะไม่ว่าง') ให้พูดตรงๆ หรืออธิบายด้วยของใกล้ตัว "
        "คงจำนวนบรรทัดใกล้เดิม คงข้อเท็จจริง/แหล่งอ้างอิงเดิม ห้ามทำให้ผิดข้อมูล ตอบ JSON เท่านั้น"
    )
    prompt = ("บท:\n" + _json.dumps(lines, ensure_ascii=False) + "\n\n"
              'ตอบ JSON: {"strong": true ถ้าดี+ตรงหัวข้ออยู่แล้ว/false ถ้าต้องปรับ, '
              '"fixed_lines": [บทที่ปรับแล้ว ครบทุกบรรทัด], "note": "จุดที่ปรับสั้นๆ"}')
    qc = _parse_json(await _or_chat(await _agent_model("retention_qc"), system, prompt, 4500))
    fixed = [_strip_quotes(str(x)) for x in (qc.get("fixed_lines") or []) if str(x).strip()]
    if not qc.get("strong", True) and fixed:
        return fixed, str(qc.get("note", "")).strip()[:140]
    return lines, ""


async def _compliance_pass(lines: list[str], profile: "ChannelProfile") -> tuple[list[str], str]:
    """✅ Compliance = WORD-SWAPPER, not a claim-stripper. The creator wants MAX มู intensity; we
    only swap the few literal trigger words that platforms flag (guaranteed lottery / get-rich /
    medical-cure) for equally-strong มู-vernacular — the belief promise stays, the risky word goes.
    Never add 'ตามความเชื่อ' disclaimers, never water the script down."""
    if profile.research_mode != "belief" or not lines:
        return lines, ""
    system = (
        "คุณคือ 'ตัวสลับคำสายมู' หน้าที่เดียว: คงพลังความเชื่อให้สุดขั้ว แต่เปลี่ยน 'คำที่แพลตฟอร์ม "
        "(YouTube/Facebook) จับ' ให้เป็น 'ภาษามู' ที่ความหมายแรงเท่าเดิมแต่ปลอดภัย. "
        "อย่า 'ลดทอน/ทำให้จืด/ใส่ตามความเชื่อ' เด็ดขาด — มูต้องเต็มพลังเสมอ. "
        "วิธีสลับ (เปลี่ยนเฉพาะคำ คงประโยคให้แรงเท่าเดิม):\n"
        "• 'ถูกหวยแน่/รวยแน่/รวย 100%/การันตีรวย' → 'เปิดทรัพย์/เรียกโชคลาภก้อนใหญ่/ดวงการเงินพุ่ง/"
        "เฮงรับทรัพย์/ดวงเศรษฐีมาเยือน/เงินทองไหลมา'\n"
        "• 'รักษาโรค...หาย/หยุดยา/แทนการรักษา' → 'ปัดเป่าสิ่งไม่ดี/เสริมพลังกายใจ/ใจสงบสบายขึ้น' "
        "(ห้ามแนะให้เลิกหาหมอ/หยุดยา)\n"
        "• 'เลขเด็ด/เลขถูกแน่งวดนี้/การันตีถูก' → คงตัวเลขไว้ได้ แต่เรียกเป็น 'เลขมงคล/เลขนำโชค/"
        "เลขเสริมดวง' (กรอบความบันเทิง-ความเชื่อ ไม่ใช่การการันตีถูกรางวัล)\n"
        "อย่างอื่น (โชคลาภ/เมตตา/ขอพรสมหวัง/แคล้วคลาด/ปังเรื่องงาน/ดวงพุ่ง/ชีวิตพลิก) = ปล่อยเต็มที่ ไม่ต้องแก้. "
        "คงจำนวนบรรทัดเดิมเป๊ะ ถ้าไม่มีคำเสี่ยงเลยตอบ ok:true ตอบ JSON เท่านั้น"
    )
    prompt = ("บท:\n" + _json.dumps(lines, ensure_ascii=False) + "\n\n"
              'ตอบ JSON: {"ok": true ถ้าไม่มีคำเสี่ยง/false ถ้าสลับคำ, '
              '"fixed_lines": [บทที่สลับคำแล้ว ครบทุกบรรทัด คงพลังมูเต็ม], "note": "คำที่สลับ"}')
    qc = _parse_json(await _or_chat(await _agent_model("compliance"), system, prompt, 4500))
    fixed = [_strip_quotes(str(x)) for x in (qc.get("fixed_lines") or []) if str(x).strip()]
    if not qc.get("ok", True) and fixed:
        return fixed, str(qc.get("note", "")).strip()[:140]
    return lines, ""


async def _qc_facts(research: str, lines: list[str], profile: "ChannelProfile") -> tuple[list[str], str]:
    """QC the script lines against the research brief; fix factual errors only.
    The QC focus (beliefs vs facts/figures) comes from the channel profile."""
    if not research or not lines:
        return lines, ""
    system = profile.qc_system
    prompt = ("ข้อมูลจริง (ยึดตามนี้):\n" + research + "\n\nบทเดิม:\n"
              + _json.dumps(lines, ensure_ascii=False) + "\n\n"
              'ตรวจแล้วตอบ JSON: {"ok": true ถ้าข้อมูลถูกหมด/false ถ้าต้องแก้, '
              '"fixed_lines": [บทที่แก้ให้ถูกแล้ว ครบทุกบรรทัด คงโทนเดิม], "note": "จุดที่แก้สั้นๆ"}')
    qc = _parse_json(await _or_chat(await _agent_model("fact_qc"), system, prompt, 5000))
    fixed = [_strip_quotes(str(x)) for x in (qc.get("fixed_lines") or []) if str(x).strip()]
    if not qc.get("ok", True) and fixed:
        return fixed, str(qc.get("note", "")).strip()[:140]
    return lines, ""


async def _trend_topic(profile: "ChannelProfile", recent: list[dict]) -> str:
    """🔥 Trend Scout: when no topic is given, pick ONE fresh, real, currently-interesting
    topic for this genre (web search) that doesn't repeat recent clips. Returns "" on failure
    so the caller falls back to the profile's own topic_pick."""
    avoid = ", ".join(c.get("title", "") for c in recent[:12] if c.get("title"))
    system = ("คุณคือนักหาประเด็นคอนเทนต์ที่ 'กำลังเป็นกระแส/ทันเหตุการณ์ช่วงนี้' ค้นจากเว็บจริง "
              "ดูว่าตอนนี้คนไทยกำลังพูดถึง/ค้นหาอะไรในแนวนี้ เลือกหัวข้อที่มีข้อมูล/หลักฐานจริง "
              "อยู่ในกระแส คนอยากดู ไม่ซ้ำของเดิม")
    prompt = (f"ช่อง: {profile.name}\nแนวทางเลือกหัวข้อ: {profile.topic_pick}\n"
              + (f"ห้ามซ้ำกับที่เคยทำ: {avoid}\n" if avoid else "")
              + "เสนอหัวข้อเดียวที่ดีที่สุดตอนนี้ ตอบเป็นชื่อหัวข้อสั้นๆ บรรทัดเดียว ไม่ต้องอธิบาย")
    out = (await _or_chat(await _agent_model("trend_scout"), system, prompt, 200)).strip()
    return _strip_quotes(out.splitlines()[0] if out else "")[:80]


_YT_PERF_KEY = "vdo_yt_performance"      # list of uploaded-clip fingerprints + video_id
_ANALYST_INSIGHT_KEY = "vdo_analyst_insight"  # the learned "what works on our channel" summary


async def record_yt_clip(channel: str, video_id: str, title: str, hook_type: str,
                         angle: str, topic: str) -> None:
    """Remember an uploaded YouTube clip so the Analyst can later correlate its stats with
    the hook/angle/topic that produced it."""
    if not video_id:
        return
    try:
        from app.core.database import get_config, set_config
        raw = await get_config(_YT_PERF_KEY, "")
        data = _json.loads(raw) if raw else []
        if not isinstance(data, list):
            data = []
        if any(e.get("video_id") == video_id for e in data):
            return
        data.insert(0, {"video_id": video_id, "channel": channel, "title": title[:90],
                        "hook_type": hook_type[:60], "angle": angle[:80], "topic": topic[:90],
                        "at": int(time.time())})
        await set_config(_YT_PERF_KEY, _json.dumps(data[:200], ensure_ascii=False))
    except Exception:
        pass


async def analyze_performance() -> dict:
    """📊 Analyst: pull live YouTube stats for our uploaded clips, rank what got the most views,
    and have the Analyst model summarise 'what works on our channel'. Stores the insight so the
    Scriptwriter + Trend Scout use it. Returns {clips:[...], insight:str}."""
    from app.core.database import get_config, set_config
    from app.agents import youtube_client
    raw = await get_config(_YT_PERF_KEY, "")
    perf = _json.loads(raw) if raw else []
    if not isinstance(perf, list) or not perf:
        return {"clips": [], "insight": ""}
    stats = await youtube_client.video_stats([e["video_id"] for e in perf[:50]])
    clips = []
    for e in perf[:50]:
        s = stats.get(e["video_id"]) or {}
        clips.append({**e, "views": s.get("views", 0), "likes": s.get("likes", 0),
                      "comments": s.get("comments", 0),
                      "url": f"https://youtu.be/{e['video_id']}"})
    clips.sort(key=lambda c: c["views"], reverse=True)
    if len([c for c in clips if c["views"] > 0]) < 3:
        return {"clips": clips, "insight": "ยังมีข้อมูลน้อย — โพสต์เพิ่มอีกหน่อยแล้วค่อยวิเคราะห์ (ต้องมี ≥3 คลิปที่มีวิว)"}
    lines = [f"{c['views']} views · {c['likes']}❤ · hook={c['hook_type']} · angle={c['angle']} · {c['title']}"
             for c in clips[:25]]
    system = ("คุณคือนักวิเคราะห์คอนเทนต์ ดูสถิติคลิปจริงของช่องด้านล่าง "
              "สรุปเป็น 'กฎสั้นๆ ที่นำไปใช้เขียนคลิปต่อไปได้' 3-5 ข้อ: hook แบบไหน/หัวข้อแนวไหน/มุมไหน "
              "ที่ได้วิวสูง และแบบไหนที่ควรเลี่ยง. อิงจากข้อมูลจริงเท่านั้น ไม่เดา ตอบไทยสั้นๆ เป็น bullet")
    insight = (await _or_chat(await _agent_model("analyst"), system, "สถิติ:\n" + "\n".join(lines), 800)).strip()
    if insight:
        await set_config(_ANALYST_INSIGHT_KEY, insight[:1200])
    return {"clips": clips, "insight": insight}


async def _analyst_insight() -> str:
    """The stored 'what works on our channel' summary, injected into writer/trend prompts."""
    try:
        from app.core.database import get_config
        return (await get_config(_ANALYST_INSIGHT_KEY, "")).strip()
    except Exception:
        return ""


async def suggest_topics(profile: "ChannelProfile", n: int = 6) -> list[dict]:
    """🔥 Trend Radar: pull free trend signals (autocomplete + news + daily trending) and have
    the Trend Scout model turn them into N ranked, specific-question topics for this channel —
    bridging to the niche WITHOUT forcing/fabricating. Returns [] on failure (fail-open)."""
    from app.agents import trends as _trends
    if profile.research_mode == "belief":
        seeds = ["ดูดวง", "เครื่องราง", "สายมู", "ฮวงจุ้ย", "เลขมงคล", "ผีพราย", "พญานาค", "ของขลัง"]
        news_q = ["มูเตลู", "สายมู", "ดูดวง", "วัดดัง"]
        niche = "สายมู/ลึกลับ/ความเชื่อไทย"
    else:
        seeds = ["รู้ไหมว่า", "ทำไม", "เรื่องลับ", "อวกาศ", "ร่างกายมนุษย์", "ประวัติศาสตร์", "วิทยาศาสตร์"]
        news_q = ["วิทยาศาสตร์", "เทคโนโลยี", "อวกาศ", "ค้นพบ"]
        niche = "ความรู้ว้าวๆ (did-you-know)"
    sig = await _trends.collect_signals(seeds, news_q)
    recent = "; ".join(c.get("title", "") for c in (await _recent_clips(profile.id))[:12] if c.get("title"))
    system = (
        f"คุณคือนักวางแผนคอนเทนต์ช่อง {niche} ที่เก่งเรื่อง 'จับกระแส'. "
        "ดูสัญญาณกระแส/คำค้นด้านล่าง แล้วเสนอหัวข้อคลิปที่คนกำลังสนใจตอนนี้และมีโอกาสปัง. "
        "กฎ: หัวข้อต้องเป็น 'คำถามเฉพาะเจาะจง' (ไม่ใช่คำกว้าง), โยงเข้าแนวช่องแบบไม่ฝืน/ไม่กุข้อมูล, "
        "เลี่ยงหัวข้อที่ทำไปแล้ว, มีข้อมูลจริงให้เล่าได้. ตอบ JSON เท่านั้น"
    )
    _ins = await _analyst_insight()
    if _ins:
        system += " บทเรียนจากสถิติจริงของช่องนี้ (เลือกหัวข้อให้เข้าแนวที่เคยได้วิวสูง): " + _ins[:500]
    prompt = (
        "สัญญาณกระแสตอนนี้:\n"
        f"- คำค้น (autocomplete): {', '.join(sig.get('autocomplete', [])[:40])}\n"
        f"- ข่าว/กระแส: {' | '.join(sig.get('news', [])[:15])}\n"
        f"- เทรนด์วันนี้: {', '.join(sig.get('daily_trending', [])[:15])}\n"
        + (f"- ทำไปแล้ว (เลี่ยง): {recent}\n" if recent else "")
        + f"\nเสนอ {n} หัวข้อที่ดีที่สุด ตอบ JSON: "
        '{"topics": [{"topic": "หัวข้อแบบคำถามเฉพาะ", "why": "ทำไมตอนนี้/เกี่ยวกระแสอะไร สั้นๆ", '
        '"hook": "ประโยคฮุคแรกที่จะใช้", "score": คะแนนน่าทำ 0-100}]}'
    )
    data = _parse_json(await _or_chat(await _agent_model("trend_scout"), system, prompt, 3000))
    out = []
    for t in (data.get("topics") or []):
        topic = _strip_quotes(str(t.get("topic", ""))).strip()[:120]
        if topic:
            out.append({"topic": topic, "why": str(t.get("why", ""))[:160],
                        "hook": _strip_quotes(str(t.get("hook", "")))[:160],
                        "score": int(t.get("score", 0)) if str(t.get("score", "")).strip().isdigit() else 0})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:n]


async def _art_prompts(lines: list[str], profile: "ChannelProfile") -> list[str]:
    """🎨 Art Director: write ONE precise English image prompt PER FINAL line (after QC), in a
    single cohesive style — so each picture literally depicts what that line says and the whole
    clip looks like one set. Returns [] on failure (caller keeps the script's own prompts)."""
    if not lines:
        return []
    subj = (lines and lines[0]) or ""
    # 🐱 optional "cat cast" style — same content, but every character is the same cute cat.
    try:
        from app.core.database import get_config
        cat = (await get_config("vdo_cat_mode", "")).strip().lower() in ("on", "1", "true", "yes")
    except Exception:
        cat = False
    cat_rule = (
        " IMPORTANT CASTING RULE: depict EVERY human/person/monk/deity character as THE SAME ONE "
        "cute chubby anthropomorphic orange-and-white tabby cat (identical recurring cat character "
        "in every image), standing/acting like a person doing the scene's action, adorable and "
        "expressive. Objects/places stay normal. Keep this exact cat consistent across all frames."
        if cat else "")
    system = (
        "You are the ART DIRECTOR of a Thai amulet/spiritual short. For EACH narration line, write "
        "ONE English image description that LITERALLY shows what that line is talking about — the "
        "actual subject/person/place/object/event named (e.g. a Thai sacred amulet, a Thai monk, "
        "King Rama IX portrait, a Thai temple ceremony, a river, a specific year carved, officials "
        "making merit). Be SPECIFIC and concrete (Thai context). Do NOT use vague metaphors or "
        "generic mood shots (random candles, abstract glows, unrelated gems/stones) — only fall "
        "back to a simple symbol if the line truly has NO concrete subject. Keep ONE clear main "
        "subject + a camera shot; vary the camera (close-up/medium/wide/macro/over-the-shoulder), "
        "no repeat in a row. NO art-style words (added later). Prefer scenes with NO written text; "
        "if a sign/label is unavoidable keep it SHORT PLAIN ENGLISH only — never Thai script (the "
        "image model garbles Thai letters)." + cat_rule + " Reply JSON only."
    )
    prompt = (
        f"เรื่องของคลิป: {subj}\nบทพากย์ (เรียงตามบรรทัด):\n" + _json.dumps(lines, ensure_ascii=False) + "\n\n"
        f'ตอบ JSON: {{"prompts": ["literal English image of what line 1 shows + camera", ...]}} '
        f'— พอดี {len(lines)} พรอมต์ เรียงตรงกับบรรทัด. กฎ: วาด \'สิ่งที่บรรทัดนั้นพูดถึงจริงๆ\' แบบตรงตัว '
        "เป็นรูปธรรม เจาะจง บริบทไทย (เช่น พระเครื่องไทย/พระสงฆ์/ในหลวง ร.9/วัด/พิธี/แม่น้ำ/ปีที่สลัก) "
        "ห้ามวาดลอยๆ ไม่เกี่ยว (เทียนมั่ว/แสงนามธรรม/หินอัญมณีไม่เกี่ยว). ถ้าบรรทัดพูดถึง 'พระ/พระเครื่อง' "
        "ให้โชว์พระเครื่องไทยชัดๆ. สลับมุมกล้อง ห้ามใส่คำสไตล์/คุณภาพ"
    )
    try:
        data = _parse_json(await _or_chat(await _agent_model("director"), system, prompt, 3500))
    except Exception:
        return []
    return [str(p).strip()[:300] for p in (data.get("prompts") or []) if str(p).strip()][:len(lines)]


async def _shot_plan(lines: list[str], profile: "ChannelProfile") -> list[dict]:
    """🎬 Director / Shot Planner: choose the best medium per beat — stock (real footage),
    still (AI image + Ken Burns), or aivideo (AI hero shot, used sparingly for hook/peak).
    Returned for logging + stored in the script result; full render wiring lands in ④."""
    if not lines:
        return []
    system = ("คุณคือผู้กำกับภาพคลิปสั้น เลือก 'สื่อ' ที่เหมาะกับแต่ละประโยคของบท: "
              "stock=ฟุตเทจจริง (สถานที่/บรรยากาศจริง), still=ภาพ AI นิ่งซูม (สัญลักษณ์/นามธรรม), "
              "aivideo=วิดีโอ AI ช็อตเด็ด (ฉากที่สต็อกไม่มี เช่น พญานาค/ของขลังเรืองแสง). "
              "ใช้ aivideo ให้น้อย (เฉพาะฮุค/จุดพีค) ที่เหลือเน้น stock/still เพื่อคุมงบ ตอบ JSON เท่านั้น")
    prompt = ("บท (เรียงตามบรรทัด):\n" + _json.dumps(lines, ensure_ascii=False) + "\n\n"
              'ตอบ JSON: {"plan": [{"i": index, "medium": "stock|still|aivideo", "note": "สิ่งที่ควรเห็นสั้นๆ"}, ...]}')
    data = _parse_json(await _or_chat(await _agent_model("director"), system, prompt, 2500))
    plan = []
    for it in (data.get("plan") or []):
        med = str(it.get("medium", "")).strip().lower()
        if med not in ("stock", "still", "aivideo"):
            med = "still"
        plan.append({"i": int(it.get("i", len(plan))), "medium": med, "note": str(it.get("note", ""))[:80]})
    return plan


async def generate_mystery_script(topic: str = "", title: str = "", summary: str = "",
                                  tone: str = "lucky") -> dict:
    """Back-compat wrapper: the สายมู channel of the generic engine."""
    from app.agents.channels import MYSTERY
    return await generate_channel_script(MYSTERY, topic, title, summary, tone=tone)


async def generate_channel_script(profile: "ChannelProfile", topic: str = "", title: str = "",
                                  summary: str = "", tone: str = "") -> dict:
    """Retention-engine script for any channel. The profile supplies genre identity
    (persona, hooks, beats, research/QC focus, visuals, brand); the beat structure and
    the two-metric optimisation are shared.

    Pipeline: research (if the profile wants it) → write to beats → QC the facts → pair
    lines_say (phonetic spelling the voice speaks) 1:1 with on-screen lines.
    """
    tone = tone or profile.default_tone
    # 🛡️ Originality Guard / Angle Diversifier: pull recent clips so the writer avoids
    # repeating angles/topics (YouTube July-2025 policy demonetises templated/repetitive channels).
    recent = await _recent_clips(profile.id)
    avoid_block = ""
    if recent:
        seen = "; ".join(f"{c.get('title','')}[{c.get('angle','')}]" for c in recent[:12] if c.get("title"))
        if seen:
            avoid_block = (
                "\n\n[หลีกเลี่ยงความซ้ำ] คลิปล่าสุดของช่องนี้ทำไปแล้ว: " + seen +
                "\nห้ามซ้ำหัวข้อ/มุมเดิม เลือก 'angle' ที่ต่างออกไป "
                "(เล่าเรื่อง / พลิกความเชื่อผิด / เปรียบเทียบ / คำเตือน / เจาะที่มา-ประวัติศาสตร์) "
                "และเปลี่ยนแบบฮุคไม่ให้เหมือนคลิปก่อน")
            await _vlog("🛡️ Originality Guard: เลี่ยงซ้ำกับ " + str(len(recent)) + " คลิปล่าสุด")
    research = ""
    source_url = ""  # Wikipedia article URL for the data citation
    subject = (topic or title or "").strip()
    # 🔥 Trend Scout: no topic supplied → pick a fresh, real, non-repeating one
    if not subject and profile.research_mode != "none":
        await _vlog("🔥 Trend Scout: หาหัวข้อที่กำลังน่าสนใจ…")
        picked = await _trend_topic(profile, recent)
        if picked:
            subject = topic = picked  # feed the chosen topic into the writer body below
            await _vlog("🔥 Trend Scout: เลือกหัวข้อ — " + subject)
    if subject and profile.research_mode != "none":
        _t = time.time()
        # 📚 Wikipedia = FACTS ONLY (Thai page first, else English for international subjects).
        # The writer must REWRITE these facts in its own spoken words — never copy the phrasing.
        try:
            from app.agents import wiki_images
            art = await wiki_images.fetch_article(subject, "th") or await wiki_images.fetch_article(subject, "en")
        except Exception:
            art = None
        if art and art.get("text"):
            research = art["text"]
            source_url = art.get("source", "")
            await _vlog(f"📚 ข้อมูลจาก Wikipedia (แต่งใหม่ ไม่ลอก): {art.get('title', subject)} · {int(time.time() - _t)} วิ")
        else:
            await _vlog("📝 ไม่มีหน้าวิกิ — เขียนจากความรู้โมเดล")
    system = (
        f"{profile.persona} {profile.audience} "
        f"{_tone_guide(tone)} "
        f"{RETENTION_CORE}"
        f"{profile.hooks} "
        f"{profile.beats} "
        f"{profile.content_rules} "
        "ห้ามใส่เครื่องหมายคำพูด \" \" หรือ ' ' ในบทพากย์ "
        "สำคัญ: ต้องส่ง 2 ชุดที่จำนวนบรรทัดเท่ากันเป๊ะ เรียงตรงกัน 1:1 — "
        "lines = ซับแสดงบนจอ: เขียนไทยถูกไวยากรณ์ แต่ 'คำเฉพาะ/ชื่อสากล/แบรนด์/ตัวย่อ/ชื่อรุ่น' "
        "ให้คงเป็นภาษาอังกฤษต้นฉบับ (เช่น USS Nimitz, NASA, UAP, Project Blue Book, F-35, DNA) "
        "ยกเว้นชื่อไทย/คำไทยให้เป็นไทย, "
        "lines_say = บทเดียวกัน แต่สะกด 'ทุกคำรวมถึงคำอังกฤษ' เป็นไทยอ่านออกเสียงให้ TTS อ่านถูก "
        f"(เช่น USS Nimitz→ยูเอสเอส นิมิตซ์, NASA→นาซ่า, {profile.pronun_examples}) คำไทยปกติคงเดิม. ตอบ JSON เท่านั้น"
    )
    _ins = await _analyst_insight()
    if _ins:
        system += (" บทเรียนจากสถิติจริงของช่องนี้ (ทำตามสูตรที่เคยได้วิวสูง เลี่ยงที่เคยแป้ก): " + _ins[:500])
    if title:
        body = f"เรื่อง: {title}\nรายละเอียด: {summary}\n\nเรียบเรียงเป็นบทคลิปที่น่าติดตาม"
    elif topic:
        body = f"หัวข้อ: {topic}\n\nเขียนบทคลิปที่น่าสนใจเรื่องนี้"
    else:
        body = profile.topic_pick
    if avoid_block:
        body += avoid_block
    if research:
        body += ("\n\nข้อเท็จจริงอ้างอิง (ใช้แค่ 'ข้อมูล' เช่น ชื่อ/ปี/เหตุการณ์/ความเชื่อ ให้ถูกต้อง):\n"
                 + research[:1500] +
                 "\n\n⚠️ สำคัญมาก: ห้ามลอกประโยค/สำนวน/ลำดับคำจากข้อความนี้เด็ดขาด — มันคือ 'สารานุกรม' "
                 "อ่านแล้วเข้าใจ แล้ว 'เล่าใหม่ด้วยภาษาพูดของตัวเอง' แบบสนุก ดึงดูด เป็นกันเอง "
                 "(ห้ามขึ้นต้นแบบวิกิ เช่น 'เป็นพระเกจิอาจารย์ที่มีชื่อเสียงของจังหวัด...'). "
                 "เอาเฉพาะข้อเท็จจริงมา แต่เรียบเรียงสำนวนใหม่ทั้งหมด")
    await _vlog(f"✍️ Scriptwriter ({await _agent_model('scriptwriter')}): เขียนบทตามโครง retention… (รอ ~15-45 วิ)")
    prompt = (
        f"{body}\n\n"
        "เขียนบทตามโครงบีทข้างบนให้ครบทุกช่วง (ฮุค → ปมค้าง → ปูเรื่อง+อ้างอิงเจาะจง → จุดพีค → สรุป+ชวนติดตาม)\n"
        "สำคัญ: อยู่กับหัวข้อเดียวตลอดทั้งคลิป ห้ามลากเรื่อง/ความเชื่ออื่นที่ไม่เกี่ยวมาปน "
        "(เช่น หัวข้อ UFO ก็เล่า UFO; หัวข้อ 'มิติที่ 4' แบบฟิสิกส์ ห้ามดัดเป็นเรื่องผีพราย) "
        "ห้ามแต่งชื่องานวิจัย/สถาบัน/วารสาร/ปี ที่ไม่ได้มาจากข้อมูลอ้างอิงจริง — ถ้าไม่มีแหล่งจริงให้เล่าแบบไม่อ้างชื่อเฉพาะ "
        "จุดพีคต้องเฉลยปมที่ค้างไว้ด้วยคำตอบที่เป็นรูปธรรม ไม่ใช่ลอยๆ ว่า 'อาจจะ'\n"
        "ถ้าหัวข้อกว้าง (เช่น 'พระราหู','พญานาค') ให้แคบเป็น 'คำถามเฉพาะ' ก่อนเขียน "
        "(เช่น 'ทำไมไหว้ราหูต้องของดำ 8 อย่าง') — หัวข้อยิ่งเฉพาะคนยิ่งกดดู\n"
        "ประโยคแรกห้ามขึ้นต้นด้วยทักทาย/เกริ่น (สวัสดี/วันนี้จะมาเล่า) — โยนฮุคใส่หน้าเลย\n"
        "ต้องมี 1 'ประโยคแชร์' (insight ที่ฟังแล้วอยากส่งต่อ) ก่อนช่วง CTA เสมอ\n"
        "ใช้ภาษาบ้านๆ ที่คนทั่วไปฟังแล้ว 'เก็ตทันที' — ห้ามศัพท์ยาก/นามธรรมลอยๆ ที่คนงง "
        "(เช่น อย่าพูด 'โลกมีชั้นซ่อน', 'พื้นที่ว่างจะไม่ว่าง' แบบไม่อธิบาย) "
        "ถ้าต้องใช้คำยาก ให้อธิบายด้วยของใกล้ตัวที่เห็นภาพชัด ทุกประโยคต้องเข้าใจง่ายไม่กำกวม\n"
        "ก่อนเขียน คิดฮุค 3 แบบในใจ เลือกอันที่หยุดนิ้วที่สุดมาเป็นประโยคแรก\n"
        "1 บรรทัด = 1 ประโยคสั้นพูดลื่น (รวมทั้งคลิป 6-9 บรรทัด) ประโยคสุดท้ายคือชวนคอมเมนต์/ติดตามเสมอ\n"
        'ตอบ JSON เท่านั้น: {"title": "หัวข้อสั้น", "lines": ["ประโยคสั้นๆ (ซับสะกดถูก)", "..."], '
        '"lines_say": ["ประโยคเดียวกันแต่แปลงคำที่อ่านยากให้ TTS อ่านถูก จำนวนเท่า lines", "..."], '
        f'"caption": "แคปชั่นโพสต์ + #แฮชแท็ก เช่น {profile.caption_hint}", '
        f'"image_prompts": ["พรอมต์ภาพพื้นหลังภาษาอังกฤษ 1 พรอมต์ต่อ 1 บรรทัดของ lines (จำนวนเท่า lines เรียงตรงกัน 1:1) '
        f'แต่ละพรอมต์ต้องบรรยาย \'สิ่งที่ควรเห็นให้ตรงกับเนื้อหาบรรทัดนั้นเป๊ะ\' '
        f'(เช่น บรรทัดพูดถึงผลึก calcite ใต้กล้อง→\'calcite crystals macro under microscope\'; '
        f'บรรทัดพูดถึงพระสมเด็จ→\'extreme close-up of an old Thai Somdej amulet\') {profile.visual_style}", "...", "..."], '
        f'"video_queries": ["คำค้นวิดีโอสต็อกจริงสั้นๆ ภาษาอังกฤษ 1-3 คำ {profile.video_query_hint}", "...", "..."], '
        f'"ai_video_prompt": "พรอมต์ภาษาอังกฤษ 1 ประโยค สำหรับ AI สร้างวิดีโอ {profile.ai_video_hint}", '
        '"cover_text": "พาดหัว COVER ตัวใหญ่บนคลิป — สั้นมาก 4-9 คำ 2 บรรทัดได้ ดึงให้คนหยุดดู '
        '(เช่น \'พญาครุฑ ทำไมคนกลัว\', \'ของขลังที่ห้ามลองของ\') ห้ามยาวเกิน", '
        '"cover_highlight": "คำ/วลีเด่นใน cover_text ที่จะระบายสีเหลือง (ต้องเป็นคำที่อยู่ใน cover_text เป๊ะ '
        'เลือกคำที่สะดุดตา/เป็นจุดขาย เช่น \'ห้ามลองของ\')", '
        f'"youtube_title": "พาดหัวคลิปสำหรับ YouTube — {profile.yt_title_hint}", '
        '"youtube_description": "คำอธิบายคลิป YouTube 2-4 ประโยค เล่าว่าเรื่องเกี่ยวกับอะไรให้คนอยากดู '
        '(ดึงจากเนื้อบท ห้ามสปอยจุดพีค) แล้วขึ้นบรรทัดใหม่ใส่ #แฮชแท็ก 5-8 ตัว แล้วปิดท้ายชวนกดติดตาม", '
        '"youtube_tags": ["แท็กคีย์เวิร์ด 8-12 คำ ตรงเนื้อหา ทั้งไทยและอังกฤษ คำสั้นๆ", "..."], '
        '"angle": "มุมเล่าของคลิปนี้สั้นๆ (เล่าเรื่อง/พลิกความเชื่อ/เปรียบเทียบ/คำเตือน/เจาะที่มา)", '
        '"hook_type": "แบบฮุคที่ใช้ (คำถามค้างใจ/พลิกความเชื่อ/ข้ออ้างเจาะจง/คำเตือน/โยนกลางฉาก)"}'
    )
    # MiniMax M3 is a reasoning model: with the long beat/hook system prompt it can spend
    # its whole budget thinking and emit truncated/empty JSON. Give it room + retry once with
    # a terse "JSON only" nudge so the default (no-topic) path doesn't fall back to a 1-liner.
    data, lines = {}, []
    writer_model = await _agent_model("scriptwriter")
    # Try the chosen model twice; if it still returns an empty/too-short script (some models
    # choke on the big multi-field JSON), fall back to the reliable default so we never ship
    # a 1-line clip. A real script is ≥4 lines.
    attempts = [writer_model, writer_model]
    if writer_model != SCRIPT_MODEL:
        attempts.append(SCRIPT_MODEL)
    for idx, mdl in enumerate(attempts):
        p = prompt if idx == 0 else (prompt + "\n\nตอบ JSON ที่ครบถ้วนทันที สั้นกระชับ ไม่ต้องอธิบาย")
        _t = time.time()
        data = _parse_json(await _or_chat(mdl, system, p, 16000))
        lines = [_strip_quotes(str(x)) for x in (data.get("lines") or []) if str(x).strip()][:9]
        lines = [x for x in lines if x]
        if len(lines) >= 4:
            await _vlog(f"✅ Scriptwriter: ร่างเสร็จ {len(lines)} บรรทัด · {int(time.time() - _t)} วิ")
            break
        nxt = attempts[idx + 1] if idx + 1 < len(attempts) else None
        if nxt:
            await _vlog(f"↻ บทไม่ครบ (ได้ {len(lines)} บรรทัด ใน {int(time.time() - _t)} วิ) — ลองใหม่"
                        + (f" ด้วยโมเดลสำรอง {nxt}" if nxt != mdl else "") + "…")
    say_raw = [_strip_quotes(str(x)) for x in (data.get("lines_say") or []) if str(x).strip()]
    out_title = _strip_quotes(str(data.get("title") or title or topic or profile.fallback_title))[:60]
    if not lines:
        lines = [out_title]
    # ── QC crew: each agent can fix the script in place; any change re-pairs lines_say ──
    # 🧐 Fact/Source-QC — cut/ correct claims that don't match the sourced research
    if research and lines:
        await _vlog(f"🧐 Fact-QC ({await _agent_model('fact_qc')}): ตรวจข้อมูลตรงแหล่ง… (รอ ~10-25 วิ)")
        _t = time.time()
        fixed, note = await _qc_facts(research, lines, profile)
        if note:
            lines = [x for x in fixed if x][:9] or lines
            say_raw = []
            await _vlog(f"✏️ Fact-QC แก้ข้อมูล ({int(time.time() - _t)} วิ): " + note)
        else:
            await _vlog(f"✅ Fact-QC ผ่าน — ข้อมูลตรงแหล่ง · {int(time.time() - _t)} วิ")
    # ✅ Compliance — soften risky claims FIRST (sparingly), then…
    if lines and profile.research_mode == "belief":
        await _vlog(f"✅ Compliance ({await _agent_model('compliance')}): เช็ค claim เสี่ยง… (รอ ~10-25 วิ)")
        _t = time.time()
        fixed, note = await _compliance_pass(lines, profile)
        if note:
            lines = [x for x in fixed if x][:9] or lines
            say_raw = []
            await _vlog(f"✏️ Compliance แก้จุดเสี่ยง ({int(time.time() - _t)} วิ): " + note)
        else:
            await _vlog(f"✅ Compliance ผ่าน — ปลอดภัย · {int(time.time() - _t)} วิ")
    # 🎯 Retention-QC LAST — re-sharpen hook/open-loop/payoff after compliance, strip repetition
    if lines:
        await _vlog(f"🎯 Retention-QC ({await _agent_model('retention_qc')}): ฮุค+ปมค้าง+อยู่ในหัวข้อ… (รอ ~10-25 วิ)")
        _t = time.time()
        fixed, note = await _retention_qc(lines, profile, subject)
        if note:
            lines = [x for x in fixed if x][:9] or lines
            say_raw = []
            await _vlog(f"✏️ Retention-QC ปรับ ({int(time.time() - _t)} วิ): " + note)
        else:
            await _vlog(f"✅ Retention-QC ผ่าน — ฮุคแรง ปมค้างครบ · {int(time.time() - _t)} วิ")
    # pair lines_say 1:1 with display lines (fall back to the display line itself)
    lines_say = [(say_raw[i] if i < len(say_raw) and say_raw[i] else lines[i]) for i in range(len(lines))]
    caption = str(data.get("caption") or out_title).strip()[:300]
    promo = (getattr(profile, "promo", "") or "").strip()
    if promo:  # fixed Ener Scan promo in the caption/description (never spoken)
        caption = (caption + "\n\n" + promo).strip()
    # 🎨 Art Director: regenerate image prompts from the FINAL lines (after QC rewrote them),
    # so each picture matches what's actually said + one cohesive style. Fall back to the
    # script's own prompts if it fails.
    image_prompts = [str(x).strip()[:300] for x in (data.get("image_prompts") or []) if str(x).strip()][:9]
    if lines:
        await _vlog(f"🎨 Art Director ({await _agent_model('director')}): วาดพรอมต์ภาพให้ตรงบทสุดท้าย…")
        art = await _art_prompts(lines, profile)
        if len(art) >= max(1, (len(lines) + 1) // 2):
            image_prompts = art
            await _vlog(f"🎨 Art Director: ได้ภาพตรงเนื้อหา {len(art)} ฉาก")
    if not image_prompts:
        image_prompts = [out_title]
    video_queries = [str(x).strip()[:80] for x in (data.get("video_queries") or []) if str(x).strip()][:3]
    ai_video_prompt = str(data.get("ai_video_prompt") or "").strip()[:300]
    # YouTube metadata (catchy title + richer description + tags). Fall back to the topic
    # title / caption so a sparse model reply still uploads with something sensible.
    yt_title = _strip_quotes(str(data.get("youtube_title") or "")).strip()[:100] or out_title
    cover_text = _strip_quotes(str(data.get("cover_text") or "")).strip()[:60] or yt_title
    cover_highlight = _strip_quotes(str(data.get("cover_highlight") or "")).strip()[:30]
    yt_description = str(data.get("youtube_description") or "").strip()
    yt_description = ((yt_description + "\n\n" + promo).strip() if (yt_description and promo) else (yt_description or caption))
    yt_tags = [str(t).strip()[:60] for t in (data.get("youtube_tags") or []) if str(t).strip()][:15]
    angle = _strip_quotes(str(data.get("angle") or "")).strip()[:80]
    hook_type = _strip_quotes(str(data.get("hook_type") or "")).strip()[:60]
    # (Shot Planner removed — visuals are 100% AI images now, so the medium-per-beat plan was
    # unused dead weight that cost ~100s/clip. The Art Director above already wrote the prompts.)
    shot_plan = []
    # 🛡️ record this clip's fingerprint so next run's Originality Guard can avoid repeating it
    await _record_clip(profile.id, {"title": out_title, "angle": angle, "hook_type": hook_type,
                                    "topic": subject, "at": int(time.time())})
    if angle:
        await _vlog(f"🎬 มุมคลิปนี้: {angle}" + (f" · ฮุค: {hook_type}" if hook_type else ""))
    return {"title": out_title, "lines": lines, "lines_say": lines_say, "caption": caption,
            "image_prompts": image_prompts, "video_queries": video_queries,
            "ai_video_prompt": ai_video_prompt, "angle": angle, "hook_type": hook_type,
            "subject": subject, "shot_plan": shot_plan, "source_url": source_url,
            "cover_text": cover_text, "cover_highlight": cover_highlight,
            "youtube_title": yt_title, "youtube_description": yt_description, "youtube_tags": yt_tags}


async def _bg_item(video_query: str, image_prompt: str, idx: int) -> tuple[str, str] | None:
    """One background slot: real stock video (Thai→foreign) if found, else an AI image."""
    if video_query:
        v = await _fetch_stock_video(video_query, idx)
        if v:
            return (v, "video")
    if image_prompt:
        img = await _gen_bg_image(image_prompt)
        if img:
            return (img, "image")
    return None


async def make_mystery_short(topic: str = "", title: str = "", summary: str = "",
                             tone: str = "evidence") -> dict:
    """Back-compat wrapper: render a short for the สายมู channel."""
    from app.agents.channels import MYSTERY
    return await make_channel_short(MYSTERY, topic, title, summary, tone=tone)


async def _suggest_real_subjects(profile: "ChannelProfile", avoid: list[str], n: int = 12) -> list[str]:
    """Ask the model for specific, real, well-known subjects for this channel — each likely to
    have a Wikipedia/Commons page (so the image-first step can find a real photo). Dedup vs
    `avoid` (already-done titles)."""
    avoid_txt = "; ".join([a for a in avoid if a][:20]) or "-"
    system = (f"คุณคือผู้ช่วยเลือกหัวข้อสำหรับช่อง '{profile.name}'. "
              "เสนอ 'ชื่อจริงเจาะจง' ที่มีอยู่จริงและน่าจะมีหน้าใน Wikipedia (ไทยหรืออังกฤษ). "
              "ผสมทั้งของไทย และของต่างประเทศทั่วโลก (เครื่องราง/เทพ/สิ่งลึกลับ/ตำนาน ของชาติอื่น). "
              "ตอบ JSON เท่านั้น")
    prompt = (f"แนวช่อง: {profile.topic_pick}\n"
              f"ห้ามซ้ำกับที่ทำไปแล้ว: {avoid_txt}\n"
              f'เสนอ {n} ชื่อเฉพาะเจาะจง ที่ "มีจริงและค้นเจอใน Wikipedia" — '
              'ผสมไทย + ต่างประเทศ (เช่น ไทย: พระสมเด็จวัดระฆัง, หลวงปู่ทวด, อเมทิสต์; '
              'ต่างประเทศ: ตาปีศาจ Nazar, เครื่องราง Omamori ญี่ปุ่น, เทพอานูบิส, ด้วงสคารับอียิปต์, '
              'ค้อนธอร์ Mjolnir, เทพกวนอู, หินมูนสโตน). '
              'เรียงจากที่คนน่าสนใจที่สุด เน้นที่ดูแล้วอยากกดดู ตอบ JSON: {"subjects": ["...", "..."]}')
    try:
        data = _parse_json(await _or_chat(await _agent_model("trend_scout"), system, prompt, 1500))
    except Exception:
        data = {}
    return [str(s).strip() for s in (data.get("subjects") or []) if str(s).strip()][:n]


# Real Wikipedia categories that enumerate each mode's subjects (verified pages = data+image).
_WIKI_CATS = {
    "amulet": ["หมวดหมู่:พระเครื่อง", "หมวดหมู่:วัตถุมงคล", "หมวดหมู่:เกจิอาจารย์"],
    "stone": ["หมวดหมู่:อัญมณี", "หมวดหมู่:รัตนชาติ", "หมวดหมู่:แร่"],
    "sacred": ["หมวดหมู่:ไหว้พระ 9 วัด กรุงเทพมหานคร",
               "หมวดหมู่:พระอารามหลวงชั้นเอก ชนิดราชวรมหาวิหาร"],
}
# skip Wikipedia list/index pages that aren't real subjects
_WIKI_SKIP_PREFIX = ("รายชื่อ", "หมวดหมู่", "แม่แบบ", "สถานีย่อย")


async def library_with_images(profile: "ChannelProfile", limit: int = 24) -> list[dict]:
    """Build a browsable list of catalog subjects that ACTUALLY have a real Wikipedia image
    (for the UI 'คลังหัวข้อที่มีรูป' panel). Returns [{subject, image, source}]."""
    from app.agents import wiki_images
    import random
    cats = _WIKI_CATS.get(profile.id, [])
    pool = (await wiki_images.catalog(cats)) if cats else []
    pool = [s for s in pool if s and not s.startswith(_WIKI_SKIP_PREFIX)]
    random.shuffle(pool)
    out = []
    for s in pool[:limit * 2]:
        if len(out) >= limit:
            break
        try:
            img = await wiki_images.find_image(s)
        except Exception:
            img = None
        # only keep subjects whose image ACTUALLY downloads (no dead links in the list)
        if img and img.get("url") and await wiki_images.image_ok(img["url"]):
            out.append({"subject": s, "image": img["url"], "source": img.get("source", "")})
    return out


async def _pick_catalog_subject(profile: "ChannelProfile") -> str:
    """Auto-pick a fresh subject from the mode's Wikipedia category catalog (deduped vs recent
    clips). Falls back to LLM-suggested subjects if the categories are empty."""
    import random
    recent = await _recent_clips(profile.id)
    avoid = {c.get("title", "").strip() for c in recent if c.get("title")}
    avoid |= {c.get("subject", "").strip() for c in recent if c.get("subject")}
    cats = _WIKI_CATS.get(profile.id, [])
    wiki_pool = []
    if cats:
        try:
            from app.agents import wiki_images
            wiki_pool = await wiki_images.catalog(cats)
        except Exception:
            wiki_pool = []
    wiki_pool = [s for s in wiki_pool if s and s not in avoid and not s.startswith(_WIKI_SKIP_PREFIX)]
    # Blend Thai catalog subjects with model-suggested ones (Thai + INTERNATIONAL) so foreign
    # amulets/deities/legends enter the rotation, not just Thai-wiki pages.
    intl_pool = [s for s in await _suggest_real_subjects(profile, list(avoid)) if s not in avoid]
    random.shuffle(wiki_pool)
    random.shuffle(intl_pool)
    # ~50/50 mix: interleave so international shows up about as often as Thai catalog subjects.
    pool = [s for pair in zip(wiki_pool, intl_pool) for s in pair]
    pool += wiki_pool[len(intl_pool):] + intl_pool[len(wiki_pool):]
    if not pool:
        return ""
    return pool[0]


async def make_channel_short(profile: "ChannelProfile", topic: str = "", title: str = "",
                             summary: str = "", tone: str = "") -> dict:
    """Render a short for any channel profile: AI picks/retells a topic -> Thai short MP4.

    Each of the (up to 3) background slots prefers a real stock video, then an AI image —
    so videos and images can be mixed within one clip. The profile decides genre, visuals,
    voice, face-PIP and brand watermark.
    """
    from app.core.pipeline_status import set_status, log_line, clear_console
    tone = tone or profile.default_tone
    _sweep_intermediates()  # tidy any leftover render junk from a previous crashed run
    await clear_console()
    await log_line(f"🚀 เริ่มสร้างคลิป — {profile.name}")
    # auto mode → pick a real subject from the Wikipedia catalog (พระเครื่อง/หิน/วัด) so we get
    # verified data + a real image; manual topics skip this.
    if not (topic or title):
        try:
            picked = await _pick_catalog_subject(profile)
            if picked:
                topic = picked
                await log_line(f"🔥 AI เลือกหัวข้อ (ไทย+ต่างประเทศ): {picked}")
        except Exception:
            pass
    await set_status("script")
    await log_line(f"✍️ ทีม AI เขียนบท (Scriptwriter: {await _agent_model('scriptwriter')})…")
    script = await generate_channel_script(profile, topic, title, summary, tone=tone)
    # Per the creator: NO internet/Wikipedia photos inside the clip — generate EVERY frame with
    # AI so the whole clip is ONE cohesive Set/tone. (Wikipedia is still used to ground the
    # script's facts + the source citation, just not as a picture.)
    hero = None
    hero_path = None
    # one shared seed across the clip → consistent look/tone (cohesion lever)
    import zlib as _zlib
    clip_seed = _zlib.crc32((script.get("title") or topic or "ener").encode("utf-8")) % 2147483647
    await log_line(f"📝 หัวข้อ: {script.get('title', '')}")
    for _ln in (script.get("lines") or []):
        await log_line("· " + str(_ln))
    face_pip = False
    if profile.face_pip:  # only channels that want a talking head try D-ID
        try:
            from app.agents.talkinghead import enabled as _th_enabled
            face_pip = _th_enabled()
        except Exception:
            face_pip = False

    from app.core.pipeline_status import checkpoint
    await checkpoint()  # 🛑 abort here if the user hit Kill during scripting
    await set_status("media", title=script.get("title", ""))
    imps = script.get("image_prompts") or [script["title"]]
    vqs = script.get("video_queries") or []
    # Visuals are AI-decided per topic. For these Thai-specific channels (พระ/หิน/วัด) foreign
    # stock footage is almost always irrelevant (the infamous random-cat clip), so we always use
    # accurate AI images (Art Director, 1 per line) + the real Wikipedia hero + Ken Burns motion.
    # No manual mode selector — the system just makes the right thing.
    bg_mode = "image"
    await log_line("🎨 ภาพ: AI ตรงเนื้อหา (เลือกอัตโนมัติตามหัวข้อ)")

    if bg_mode == "image":
        # one AI image PER LINE (prompts are 1:1 with lines & content-specific) → many scenes
        # that match the narration. Cap 8 for cost/time.
        n_lines = len(script.get("lines") or [])
        n = max(3, min(9, n_lines or 5))  # 1:1 with lines so each image syncs to its line
        prompts = list(imps)
        while len(prompts) < n:
            prompts.append(imps[len(prompts) % len(imps)] if imps else script["title"])
        # ALL frames AI-generated, ONE shared seed → cohesive same-Set look, each matches its line.
        # 🐱 Cat-lock: when Cat Cast is on, anchor one mascot cat and keep it identical every scene.
        if await _catlock_on():
            await log_line(f"🐱 สร้างภาพ AI {n} ฉาก — ล็อกแมวตัวเดิมทุกฉาก (Nano Banana)…")
            imgs = await _gen_bg_images_catlock(prompts[:n], seed=clip_seed)
        else:
            await log_line(f"🎨 สร้างภาพ AI {n} ฉาก (สไตล์เดียวกันทั้งคลิป · seed คงที่)…")
            imgs = await _gen_bg_images(prompts[:n], seed=clip_seed)
        await log_line(f"✅ ได้ภาพ {len(imgs)}/{n} ฉาก")
        items = [(p, "image") for p in imgs]
    else:
        from app.agents import aivideo
        items = []
        # 🎬 AI-generated video shots (fal) for the scenes the Director flagged "aivideo".
        # Count = config override → else the Director's plan → capped for cost.
        plan = script.get("shot_plan") or []
        plan_ai_notes = [s.get("note") for s in plan if s.get("medium") == "aivideo" and s.get("note")]
        try:
            from app.core.database import get_config
            cfg_n = (await get_config("vdo_ai_video_count", "")).strip()
            fal_model = (await get_config("FAL_VIDEO_MODEL", "")).strip()
        except Exception:
            cfg_n, fal_model = "", ""
        ai_n = int(cfg_n) if cfg_n.isdigit() else (len(plan_ai_notes) or 1)
        ai_n = max(0, min(ai_n, 3))  # hard cost cap
        if aivideo.enabled() and ai_n > 0:
            prompts = (plan_ai_notes or [script.get("ai_video_prompt") or script["title"]])
            prompts = (prompts * ai_n)[:ai_n]
            await log_line(f"🎬 AI สร้างวิดีโอ {len(prompts)} ช็อต ({fal_model or aivideo.current_model()})…")
            avs = await asyncio.gather(*[
                aivideo.generate_ai_video(p, os.path.join(VDO_DIR, f"hero_{int(time.time())}_{i}.mp4"), fal_model)
                for i, p in enumerate(prompts)])
            got = [(v, "video") for v in avs if v]
            items += got
            await log_line(f"✅ ได้ AI video {len(got)}/{len(prompts)} ช็อต")
        # real stock video + AI image stills for the remaining scenes (keep clip dynamic: ~5 total)
        n_fill = max(3, 5 - len(items))
        slots = []
        for i in range(n_fill):
            vq = vqs[i] if i < len(vqs) else (vqs[0] if vqs else "")
            ip = imps[i] if i < len(imps) else (imps[0] if imps else script["title"])
            slots.append((vq, ip, i))
        fill = list(await asyncio.gather(*[_bg_item(vq, ip, i) for vq, ip, i in slots]))
        items += [it for it in fill if it]
        items = items[:6]

    items = [it for it in items if it]
    if not items:  # last-resort so the clip still ships
        imgs = await _gen_bg_images([script["title"]])
        items = [(p, "image") for p in imgs]

    # YouTube COVER/thumbnail: big centered title over the real hero (or first) image —
    # made NOW because _render_clip deletes the bg images afterwards.
    cover_bg = hero_path or (items[0][0] if items else "")
    thumb_path = ""
    if cover_bg and os.path.exists(cover_bg):
        try:
            thumb_path = await asyncio.to_thread(
                _render_cover, cover_bg,
                script.get("cover_text") or script.get("youtube_title") or script["title"],
                script.get("cover_highlight", ""),
                os.path.join(VDO_DIR, f"cover_{int(time.time())}.jpg"))
            if thumb_path:
                await log_line("🖼️ ทำรูปปก (cover) สำหรับ YouTube แล้ว")
        except Exception:
            thumb_path = ""

    # 🎬 OPTIONAL add-on (separate module, gated by vdo_animate): turn the accurate stills into
    # moving clips via Kling i2v. Off by default → still pipeline unchanged. Fail-open per item.
    try:
        from app.agents import animate
        if await animate.is_on():
            await set_status("media", title=script.get("title", ""))
            await log_line(f"🎬 ทำภาพเคลื่อนไหว (Kling i2v) {len(items)} ฉาก… อาจรอ 1-3 นาที")
            items = await animate.animate_items(items, VDO_DIR)
            nv = sum(1 for _p, k in items if k == "video")
            await log_line(f"✅ ภาพเคลื่อนไหว {nv}/{len(items)} ฉาก (ที่เหลือใช้ภาพนิ่ง)")
    except Exception:
        pass

    await checkpoint()  # 🛑 abort here if the user hit Kill during media generation
    await set_status("render", title=script.get("title", ""))
    await log_line("🎙️ พากย์ (เสียงคุณ V3)" + (" + 🗣️ หน้าพูด D-ID" if face_pip else "") + " + 🎬 ตัดต่อ…")
    r = await _render_clip(script["title"], script["lines"], bg_items=items, face_pip=face_pip,
                           say_lines=script.get("lines_say"),
                           brand=(profile.brand_handle, profile.brand_web),
                           title_card=(script.get("cover_text") or script.get("youtube_title") or script["title"]),
                           cover_highlight=script.get("cover_highlight", ""))
    if r.get("ok"):
        kinds = [k for _, k in items]
        # cite the Wikipedia data + image used (legal attribution)
        caption = script["caption"]
        yt_desc = script.get("youtube_description") or script["caption"]
        cites = []
        if script.get("source_url"):
            cites.append("📚 ข้อมูลอ้างอิง: Wikipedia — " + script["source_url"])
        if hero_path and hero:
            img_src = hero.get("source") or ""
            if img_src and img_src != script.get("source_url"):
                cites.append("📷 ภาพ: " + str(hero.get("credit", "Wikipedia")) + " — " + img_src)
            elif not script.get("source_url"):
                cites.append("📷 ภาพ: " + str(hero.get("credit", "Wikipedia")))
        if cites:
            block = "\n\n" + "\n".join(cites)
            caption = (caption + block).strip()
            yt_desc = (yt_desc + block).strip()
        await log_line(f"✅ คลิปเสร็จ {r.get('duration', '?')} วิ" + (" · มีหน้าพูด" if r.get("talking_head") else ""))
        r.update({"caption": caption, "lines": script["lines"], "title": script["title"],
                  "youtube_title": script.get("youtube_title") or script["title"],
                  "youtube_description": yt_desc,
                  "youtube_tags": script.get("youtube_tags") or [],
                  "angle": script.get("angle", ""), "hook_type": script.get("hook_type", ""),
                  "subject": script.get("subject", ""),
                  "thumbnail": thumb_path,
                  "bg_count": len(items),
                  "bg_kind": f"{kinds.count('video')}vid+{kinds.count('image')}img"})
    else:
        await log_line(f"❌ render พลาด: {str(r.get('error', ''))[:80]}")
    return r
