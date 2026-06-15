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

VDO_DIR = "/app/data/vdo"
SCRIPT_MODEL = "google/gemini-2.5-flash"  # strong Thai, low hallucination, cheap

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
    "Style: Default,Garuda,72,&H00FFFFFF,&H00111111,&H64000000,-1,0,0,0,100,100,3,0,1,4,3,2,70,70,540,0\n"
    "Style: Title,Garuda,62,&H00A5B4FC,&H00111111,&H64000000,-1,0,0,0,100,100,0,0,1,5,3,8,70,70,250,0\n"
    "Style: Brand,Garuda,52,&H00FFFFFF,&H00111111,&H64000000,-1,0,0,0,100,100,1,0,1,3,2,8,70,70,250,0\n\n"
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
    s, e = txt.find("{"), txt.rfind("}")
    if s != -1 and e > s:
        txt = txt[s:e + 1]
    try:
        d = _json.loads(txt)
        return d if isinstance(d, dict) else {}
    except Exception:
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
    data = _parse_json(await _or_chat(SCRIPT_MODEL, system, prompt, 700))
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


def _synth_lines(lines: list[str], base: str, out_mp3: str) -> tuple[list[tuple[str, float]], float]:
    """Synthesize each line separately → measure → concat into out_mp3.

    Per-line timing is what lets the subtitle for each line appear exactly while that line
    is spoken (one line at a time), instead of an even split that drifts off the audio.
    Returns ([(line, duration)…], total_duration).
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
        segs.append((ln, d))
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


def _build_ass(title: str, segments: list[tuple[str, float]], ass_path: str) -> None:
    """Caption track: one short row on screen at a time, voice-synced.

    Each spoken line is timed to its real audio duration, then split into single display
    rows whose on-screen time is shared across the line's segment by row length — so the
    caption advances row-by-row in step with the narration (never a multi-row block).
    """
    total = sum(d for _, d in segments) or 1.0
    events = []
    # brand logo only (no topic title) as the persistent header
    handle, web = _ass_escape(VDO_BRAND_HANDLE)[:20], _ass_escape(VDO_BRAND_WEB)[:30]
    brand_parts = []
    if handle:
        brand_parts.append("{\\c" + _BRAND_GREEN + "}" + handle)
    if web:
        brand_parts.append("{\\c" + _BRAND_WHITE + "}" + web)
    if brand_parts:
        events.append(
            f"Dialogue: 0,{_ass_ts(0)},{_ass_ts(total)},Brand,,0,0,0,,{'  '.join(brand_parts)}"
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


async def _gen_bg_image(prompt: str, idx: int = 0) -> str | None:
    """Generate a topical 9:16 background image via OpenRouter (Gemini image). Fail-open."""
    try:
        import base64 as _b64
        import httpx
        from app.core.openrouter_client import get_openrouter_api_key, openrouter_base_url
        key = await get_openrouter_api_key()
        if not key:
            return None
        body = {
            "model": "google/gemini-2.5-flash-image",
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": (
                f"{prompt}. Vertical 9:16 cinematic atmospheric background, dark and moody, "
                "mysterious mood, high quality. ABSOLUTELY NO text, no words, no letters, no captions."
            )}],
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


async def _gen_bg_images(prompts: list[str]) -> list[str]:
    """Generate several bg images in parallel; returns the paths that succeeded."""
    prompts = [p for p in (prompts or []) if str(p).strip()][:6]
    if not prompts:
        return []
    results = await asyncio.gather(*[_gen_bg_image(p, i) for i, p in enumerate(prompts)])
    return [p for p in results if p]


async def _pexels_pick(query: str, key: str) -> dict | None:
    """Search Pexels for `query`, return the best portrait mp4 file (closest to 9:16)."""
    import httpx
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 8},
        )
    if r.status_code >= 300:
        return None
    target = 1920 / 1080  # h/w for 9:16 ≈ 1.778
    best, best_score = None, None
    for v in (r.json().get("videos") or []):
        vw, vh = v.get("width") or 0, v.get("height") or 0
        if (v.get("duration") or 0) < 2 or vw <= 0 or vh < vw:
            continue
        files = [f for f in (v.get("video_files") or [])
                 if f.get("file_type") == "video/mp4" and f.get("link")
                 and (f.get("height") or 0) >= (f.get("width") or 0)]  # portrait only
        if not files:
            continue
        score = abs((vh / vw) - target)
        if best is None or score < best_score:
            files.sort(key=lambda f: abs((f.get("width") or 0) - 1080))
            best, best_score = files[0], score
    return best


async def _fetch_stock_video(query: str, idx: int = 0) -> str | None:
    """Find + download a real vertical Thai stock video for `query` from Pexels. Fail-open.

    Tries the (Thai-biased) query first; if nothing matches, retries once with the Thai
    words stripped so we still get a real video rather than dropping to an AI image.
    """
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    query = (query or "").strip()
    if not key or not query:
        return None
    try:
        best = await _pexels_pick(query, key)
        if not best:
            broadened = re.sub(r"\b(thai|thailand|bangkok)\b", "", query, flags=re.IGNORECASE).strip()
            if broadened and broadened.lower() != query.lower():
                best = await _pexels_pick(broadened, key)
        if not best:
            return None
        import httpx
        os.makedirs(VDO_DIR, exist_ok=True)
        path = os.path.join(VDO_DIR, f"sv_{int(time.time())}_{idx}.mp4")
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            async with c.stream("GET", best["link"]) as resp:
                if resp.status_code >= 300:
                    return None
                with open(path, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
        return path if os.path.exists(path) and os.path.getsize(path) > 10000 else None
    except Exception:
        return None


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
            bg_items: list[tuple[str, str]] | None = None) -> tuple[bool, str]:
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
        seg = max(1.0, duration / n)
        dframes = max(fps, int(seg * fps))
        cmd = ["ffmpeg", "-y"]
        for p, k in items:
            if k == "image":
                # single frame in -> zoompan generates the motion (d frames). DON'T loop the
                # input: a looped multi-frame still makes zoompan explode frames so only the
                # first image ever shows.
                cmd += ["-i", p]
            else:
                cmd += ["-stream_loop", "-1", "-t", f"{seg:.2f}", "-i", p]
        cmd += ["-i", audio_path]
        voice_idx, bgm_idx = n, None
        if bgm:
            cmd += ["-stream_loop", "-1", "-i", bgm]
            bgm_idx = n + 1
        chains = []
        for i, (p, k) in enumerate(items):
            if k == "image":  # still image -> slow Ken Burns zoom
                chains.append(
                    f"[{i}:v]scale=1620:2880:force_original_aspect_ratio=increase,crop=1620:2880,"
                    f"zoompan=z='min(zoom+0.0012,1.35)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                    f"d={dframes}:s=1080x1920:fps={fps},setsar=1[v{i}]"
                )
            else:  # real video -> fill 9:16
                chains.append(
                    f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                    f"fps={fps},setpts=PTS-STARTPTS,setsar=1[v{i}]"
                )
        cat = "".join(f"[v{i}]" for i in range(n))
        fc = (";".join(chains) +
              f";{cat}concat=n={n}:v=1:a=0,eq=brightness=-0.19:saturation=1.08,"
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
                       bg_items: list[tuple[str, str]] | None = None) -> dict:
    """Shared render: lines -> TTS -> ASS captions -> MP4 (stock-video or image slideshow).

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
        segments, duration = await asyncio.to_thread(_synth_lines, lines, base, mp3)
    except Exception as exc:
        return {"ok": False, "error": f"TTS ล้มเหลว: {str(exc)[:200]}"}
    if not segments or duration <= 0:
        return {"ok": False, "error": "อ่านความยาวเสียงไม่ได้"}
    await asyncio.to_thread(_build_ass, title, segments, ass)

    # talking-head PIP (optional): generate while the mp3 is still on disk + served publicly
    pip_video = None
    if face_pip:
        try:
            from app.agents import talkinghead
            if talkinghead.enabled():
                pip_video = await talkinghead.generate_talking_head(mp3, base + "_pip.mp4")
        except Exception:
            pip_video = None

    render_target = (base + "_bg.mp4") if pip_video else mp4
    ok, err = await asyncio.to_thread(_render, mp3, ass, duration, render_target, None, None, bg_items)
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


async def generate_mystery_script(topic: str = "", title: str = "", summary: str = "") -> dict:
    """สายมู/ลึกลับ content for the Ener Scan page: amulets (TH+world), UFO, myths, beliefs.

    If title/summary are given (a real mystery news item) it retells them; else it picks
    an intriguing topic. Tone: engaging + respectful of belief, no guarantees, not mocking.
    """
    system = (
        "คุณคือครีเอเตอร์คอนเทนต์สายมู/ลึกลับของเพจ 'Ener Scan ตรวจพลังพระ หิน เครื่องราง' "
        "เขียนบทคลิปสั้นแนวตั้งภาษาไทย แนวสายมู/พลังงาน/ความเชื่อ เล่าแบบเป็นกันเอง "
        "สุภาพแต่กวนๆ นิดหน่อย ชวนคุย (เช่น รู้ไหมว่า, บอกเลยว่า, ลองคิดดูสิ, เชื่อหรือเปล่า) "
        "ใช้สรรพนาม ผม/เรา/คุณ ห้ามใช้ กู/มึง ห้ามหยาบคาย "
        "โทนขลังๆ ลึกลับ น่าค้นหา ชวนขนลุก เน้นเล่าเรื่องความเชื่อ/พลังงาน/ตำนานให้น่าติดตาม "
        "สำคัญ: ต้องฟังดู 'มีที่มา น่าเชื่อถือ' — อ้างอิงแหล่งแบบเจาะจงเนียนๆ อย่างน้อย 1 จุด "
        "(เช่น ตามตำนานล้านนา, ในพงศาวดารเหนือ, บันทึกใบลานเก่า, ความเชื่อชาวบ้านภาคอีสาน, "
        "ตำราพราหมณ์, คนเฒ่าคนแก่เล่าสืบกันมา, ตำนานของชนเผ่า...) ใส่รายละเอียดเฉพาะ (ชื่อ/ยุค/สถานที่) "
        "ให้ฟังดูเหมือนค้นคว้ามาจริง แต่ห้ามกุข้อมูลเท็จที่ตรวจสอบได้ ห้ามอ้างวิทยาศาสตร์/งานวิจัยปลอม "
        "ไม่ต้องหักมุมพลิกตอนจบ — เล่าตรงๆ ตามโทนความเชื่อ ปิดด้วยประโยคชวนขนลุก/ชวนเชื่อ/ชวนคิด "
        "เคารพความเชื่อ ไม่ลบหลู่สิ่งศักดิ์สิทธิ์ ไม่การันตีโชคลาภ/รักษาโรค "
        "ห้ามใส่เครื่องหมายคำพูด \" \" หรือ ' ' ในบทพากย์ ตอบ JSON เท่านั้น"
    )
    if title:
        body = f"ข่าว/เรื่อง: {title}\nรายละเอียด: {summary}\n\nเรียบเรียงเป็นบทคลิปสายมูที่น่าติดตาม"
    elif topic:
        body = f"หัวข้อ: {topic}\n\nเขียนบทคลิปสายมูที่น่าสนใจเรื่องนี้"
    else:
        body = (
            "เลือกหัวข้อสายมู/ลึกลับที่น่าสนใจ 1 เรื่อง (หมุนเวียนแนว: พระเครื่อง/เครื่องรางไทย, "
            "เครื่องรางต่างประเทศ เช่น Omamori ญี่ปุ่น/Nazar ตุรกี/Hamsa/Maneki-neko, UFO/UAP, "
            "ตำนานลึกลับ, ความเชื่อ/ของขลัง, สถานที่ศักดิ์สิทธิ์) แล้วเขียนบทคลิป"
        )
    prompt = (
        f"{body}\n\n"
        "รูปแบบบท (สไตล์เล่าให้เพื่อนฟัง โทนความเชื่อขลังๆ):\n"
        "- เปิดด้วย hook สะดุดใจ ชวนขนลุก 1 ประโยคใน 3 วิแรก (เช่น เชื่อไหมว่า.../รู้ไหมว่า...)\n"
        "- เล่า 3-4 ประโยคสั้น ค่อยๆ ปูเรื่อง สร้างบรรยากาศลึกลับ โดย**อ้างที่มาแบบเจาะจง 1 จุด** "
        "(ตำนาน/บันทึก/ความเชื่อท้องถิ่น + ชื่อ/ยุค/สถานที่) ให้ฟังดูค้นคว้ามา น่าเชื่อถือ\n"
        "- ปิดด้วยประโยคชวนขนลุก/ชวนเชื่อ/ชวนคิด ตามโทนความเชื่อ (ไม่การันตีผล ไม่ต้องหักมุม)\n"
        'ตอบ JSON เท่านั้น: {"title": "หัวข้อสั้น", "lines": ["ประโยคสั้นๆ", "..."], '
        '"caption": "แคปชั่นโพสต์ + #แฮชแท็ก เช่น #สายมู #เครื่องราง #ความเชื่อ #ลึกลับ", '
        '"image_prompts": ["ภาพพื้นหลัง 5 ฉากเป็นภาษาอังกฤษ ไล่ตามเนื้อหาทีละช่วง บรรยากาศขลังๆ ไทย/เอเชีย (ไม่มีตัวหนังสือในภาพ)", "...", "...", "...", "..."], '
        '"video_queries": ["คำค้นวิดีโอสต็อกจริงสั้นๆ ภาษาอังกฤษ 1-3 คำ เน้นบรรยากาศไทย/เอเชีย ใส่คำว่า Thai หรือ Thailand เมื่อเข้ากับเรื่อง เช่น Thai temple, Thai monk, Thailand misty forest, incense smoke shrine, Thai river mist", "...", "..."], '
        '"ai_video_prompt": "พรอมต์ภาษาอังกฤษ 1 ประโยค สำหรับ AI สร้างวิดีโอ \\"ฉากเด็ด\\" ที่สต็อกไม่มี (เช่น พญานาค/ของขลังเรืองแสง/ควันวนรอบพระ) cinematic ขลังๆ"}'
    )
    data = _parse_json(await _or_chat(SCRIPT_MODEL, system, prompt, 1000))
    lines = [_strip_quotes(str(x)) for x in (data.get("lines") or []) if str(x).strip()][:8]
    lines = [x for x in lines if x]
    out_title = _strip_quotes(str(data.get("title") or title or topic or "เรื่องลึกลับ"))[:60]
    if not lines:
        lines = [out_title]
    caption = str(data.get("caption") or out_title).strip()[:300]
    image_prompts = [str(x).strip()[:300] for x in (data.get("image_prompts") or []) if str(x).strip()][:6]
    if not image_prompts:
        image_prompts = [out_title]
    video_queries = [str(x).strip()[:80] for x in (data.get("video_queries") or []) if str(x).strip()][:3]
    ai_video_prompt = str(data.get("ai_video_prompt") or "").strip()[:300]
    return {"title": out_title, "lines": lines, "caption": caption,
            "image_prompts": image_prompts, "video_queries": video_queries,
            "ai_video_prompt": ai_video_prompt}


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


async def make_mystery_short(topic: str = "", title: str = "", summary: str = "") -> dict:
    """สายมู short: AI picks/retells a mystery topic -> Thai short MP4.

    Each of the (up to 3) background slots prefers a real Thai stock video, then a foreign
    one, then an AI image — so videos and images can be mixed within one clip.
    """
    script = await generate_mystery_script(topic, title, summary)
    try:
        from app.agents.talkinghead import enabled as _th_enabled
        face_pip = _th_enabled()
    except Exception:
        face_pip = False

    imps = script.get("image_prompts") or [script["title"]]
    vqs = script.get("video_queries") or []
    bg_mode = os.environ.get("VDO_BG_MODE", "image")  # image (free, all AI images) | video | mixed

    if bg_mode == "image":
        # all AI images via OpenRouter (no new bill) — several scenes per clip
        n = max(1, min(6, int(os.environ.get("VDO_BG_IMAGE_COUNT", "5") or 5)))
        prompts = list(imps)
        while len(prompts) < n:
            prompts.append(imps[len(prompts) % len(imps)])
        imgs = await _gen_bg_images(prompts[:n])
        items = [(p, "image") for p in imgs]
    else:
        slots = []
        for i in range(3):
            vq = vqs[i] if i < len(vqs) else (vqs[0] if vqs else "")
            ip = imps[i] if i < len(imps) else (imps[0] if imps else script["title"])
            slots.append((vq, ip, i))
        items = list(await asyncio.gather(*[_bg_item(vq, ip, i) for vq, ip, i in slots]))
        try:  # one "hero" AI-video scene when fal.ai is configured (video/mixed mode only)
            from app.agents import aivideo
            hero = script.get("ai_video_prompt") or ""
            if aivideo.enabled() and hero:
                hv = await aivideo.generate_ai_video(hero, os.path.join(VDO_DIR, f"hero_{int(time.time())}.mp4"))
                if hv:
                    items.insert(1 if len(items) >= 2 else 0, (hv, "video"))
                    items = items[:3]
        except Exception:
            pass

    items = [it for it in items if it]
    if not items:  # last-resort so the clip still ships
        imgs = await _gen_bg_images([script["title"]])
        items = [(p, "image") for p in imgs]

    r = await _render_clip(script["title"], script["lines"], bg_items=items, face_pip=face_pip)
    if r.get("ok"):
        kinds = [k for _, k in items]
        r.update({"caption": script["caption"], "lines": script["lines"], "title": script["title"],
                  "bg_count": len(items),
                  "bg_kind": f"{kinds.count('video')}vid+{kinds.count('image')}img"})
    return r
