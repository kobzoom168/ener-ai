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
SCRIPT_MODEL = "deepseek/deepseek-v4-flash"  # cheap, decent Thai for v1

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
    "Style: Default,Loma,60,&H00FFFFFF,&H00111111,&H64000000,-1,0,0,0,100,100,0,0,1,5,3,2,70,70,320,0\n"
    "Style: Title,Loma,46,&H00A5B4FC,&H00111111,&H64000000,-1,0,0,0,100,100,0,0,1,4,2,8,70,70,90,0\n\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


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
        "เขียนบทพากย์สำหรับคลิปแนวตั้ง 30-45 วินาที ห้ามหยาบคาย ห้ามดูถูกใคร ตอบ JSON เท่านั้น"
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
    lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()][:8]
    if not lines:
        lines = [title]
    caption = str(data.get("caption") or title).strip()[:300]
    return {"lines": lines, "caption": caption}


def _synth_voice(text: str, out_path: str) -> str:
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


def _wrap_thai(text: str, max_chars: int = 24) -> str:
    """Hard-wrap a line to fit the video width using ASS \\N breaks.

    Thai has no spaces so libass can't auto-wrap — we wrap manually: break at spaces
    when possible, otherwise hard-split long runs at max_chars.
    """
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    out: list[str] = []
    line = ""
    for tok in re.findall(r"\S+|\s+", text):
        if tok.isspace():
            if line:
                line += tok
            continue
        while len(tok) > max_chars:
            if line.strip():
                out.append(line.strip())
            out.append(tok[:max_chars])
            tok = tok[max_chars:]
            line = ""
        if len(line) + len(tok) > max_chars:
            if line.strip():
                out.append(line.strip())
            line = tok
        else:
            line += tok
    if line.strip():
        out.append(line.strip())
    return "\\N".join(out)


def _build_ass(title: str, lines: list[str], duration: float, ass_path: str) -> None:
    n = max(1, len(lines))
    per = max(1.0, duration / n)
    events = []
    # persistent small title at top for the whole clip
    events.append(
        f"Dialogue: 0,{_ass_ts(0)},{_ass_ts(duration)},Title,,0,0,0,,{_wrap_thai(_ass_escape(title)[:80], 30)}"
    )
    for i, ln in enumerate(lines):
        st = i * per
        en = min(duration, (i + 1) * per)
        events.append(f"Dialogue: 0,{_ass_ts(st)},{_ass_ts(en)},Default,,0,0,0,,{_wrap_thai(_ass_escape(ln), 24)}")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(_ASS_HEADER + "\n".join(events) + "\n")


def _render(audio_path: str, ass_path: str, duration: float, out_path: str) -> tuple[bool, str]:
    bg = f"color=c=0x0f172a:s=1080x1920:d={duration:.2f}"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", bg,
        "-i", audio_path,
        "-vf", f"subtitles={ass_path}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", out_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if r.returncode == 0 and os.path.exists(out_path):
            return True, "ok"
        return False, (r.stderr or "ffmpeg failed")[-500:]
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
    narration = " ".join(lines)

    try:
        await asyncio.to_thread(_synth_voice, narration, mp3)
    except Exception as exc:
        return {"ok": False, "error": f"TTS ล้มเหลว: {str(exc)[:200]}"}

    duration = await asyncio.to_thread(_audio_duration, mp3)
    if duration <= 0:
        return {"ok": False, "error": "อ่านความยาวเสียงไม่ได้"}

    await asyncio.to_thread(_build_ass, title, lines, duration, ass)
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


async def _render_clip(title: str, lines: list[str]) -> dict:
    """Shared render: lines -> gTTS -> ASS captions -> MP4. Returns {ok, mp4, duration, error}."""
    os.makedirs(VDO_DIR, exist_ok=True)
    stamp = int(time.time())
    base = os.path.join(VDO_DIR, f"vdo_{stamp}")
    mp3, ass, mp4 = base + ".mp3", base + ".ass", base + ".mp4"
    if not lines:
        return {"ok": False, "error": "ไม่มีบทพากย์"}
    try:
        await asyncio.to_thread(_synth_voice, " ".join(lines), mp3)
    except Exception as exc:
        return {"ok": False, "error": f"TTS ล้มเหลว: {str(exc)[:200]}"}
    duration = await asyncio.to_thread(_audio_duration, mp3)
    if duration <= 0:
        return {"ok": False, "error": "อ่านความยาวเสียงไม่ได้"}
    await asyncio.to_thread(_build_ass, title, lines, duration, ass)
    ok, err = await asyncio.to_thread(_render, mp3, ass, duration, mp4)
    if not ok:
        return {"ok": False, "error": f"render ล้มเหลว: {err}"}
    for p in (mp3, ass):
        try:
            os.remove(p)
        except Exception:
            pass
    return {"ok": True, "mp4": mp4, "duration": round(duration, 1)}


async def generate_mystery_script(topic: str = "", title: str = "", summary: str = "") -> dict:
    """สายมู/ลึกลับ content for the Ener Scan page: amulets (TH+world), UFO, myths, beliefs.

    If title/summary are given (a real mystery news item) it retells them; else it picks
    an intriguing topic. Tone: engaging + respectful of belief, no guarantees, not mocking.
    """
    system = (
        "คุณคือครีเอเตอร์คอนเทนต์สายมู/ลึกลับของเพจ 'Ener Scan ตรวจพลังพระ หิน เครื่องราง' "
        "เขียนบทคลิปสั้นแนวตั้งภาษาไทย น่าสนใจ ชวนติดตาม เล่าเรื่องสนุกแต่ให้ความรู้ "
        "เคารพความเชื่อ ไม่ลบหลู่ ไม่การันตีโชคลาภ/รักษาโรค ไม่ชวนเชื่องมงายเกินจริง ตอบ JSON เท่านั้น"
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
        "รูปแบบบท:\n"
        "- เปิดด้วย hook สะดุดใจ 1 ประโยคใน 3 วิแรก\n"
        "- เล่า 3-5 ประโยคสั้น (ที่มา/ตำนาน/ความเชื่อ/เกร็ดน่ารู้)\n"
        "- ปิดด้วยประโยคชวนคิด/ชวนติดตาม (ไม่การันตีผล)\n"
        'ตอบ JSON เท่านั้น: {"title": "หัวข้อสั้น", "lines": ["ประโยคสั้นๆ", "..."], '
        '"caption": "แคปชั่นโพสต์ + #แฮชแท็ก เช่น #สายมู #เครื่องราง #ความเชื่อ #ลึกลับ"}'
    )
    data = _parse_json(await _or_chat(SCRIPT_MODEL, system, prompt, 800))
    lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()][:8]
    out_title = str(data.get("title") or title or topic or "เรื่องลึกลับ").strip()[:60]
    if not lines:
        lines = [out_title]
    caption = str(data.get("caption") or out_title).strip()[:300]
    return {"title": out_title, "lines": lines, "caption": caption}


async def make_mystery_short(topic: str = "", title: str = "", summary: str = "") -> dict:
    """สายมู short: AI picks/retells a mystery topic -> Thai short MP4."""
    script = await generate_mystery_script(topic, title, summary)
    r = await _render_clip(script["title"], script["lines"])
    if r.get("ok"):
        r.update({"caption": script["caption"], "lines": script["lines"], "title": script["title"]})
    return r
