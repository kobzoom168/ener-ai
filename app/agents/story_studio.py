"""🎬 Story Studio — a SEPARATE engine for realistic Thai narrative videos (นิทาน/พุทธประวัติ/
นิยาย). Independent from Auto Post: it does NOT import channels.py / autopost.py and has its own
pipeline. Stage 1 here = the story brain: topic → a shot-by-shot script with consistent characters,
Thai narration, and English cinematic image prompts (Thai-authentic) ready for image generation.

Pipeline (built in stages):
  1) generate_story()      → title, characters, shots   ← this file (stage 1)
  2) character sheets      → multi-view Nano Banana refs per character
  3) per-shot images       → Nano Banana (character + location refs) = same faces every shot
  4) motion + narration    → Kling i2v + Thai voiceover
  5) assemble              → ffmpeg → mp4
"""
from __future__ import annotations

import json
import re

from app.core.ai import chat_json

# Default LLM for the story brain. xAI (Grok) is strong at vivid narrative; override per call.
_STORY_MODEL = "grok"

_STORY_SYSTEM = """คุณคือ "นักเขียนบทหนังสั้น AI" ที่เชี่ยวชาญเรื่องเล่าไทย (นิทาน/พุทธประวัติ/นิยาย/ตำนาน)
เขียนบทให้ละเอียดระดับสตอรี่บอร์ด แบ่งเป็น "ช็อต" พร้อมพรอมต์ภาพ + บทบรรยายไทย

หลักการ:
- ตัวละครต้องคงที่ทั้งเรื่อง: บรรยายหน้าตา/ชุด/ลักษณะเด่นแต่ละตัวให้ชัด (เพื่อใช้สร้างชีตตัวละครอ้างอิง)
- ภาพ "สมจริงสุด + บริบทไทยแท้": คนไทย ผิว/ผม/ชุดไทย, สถาปัตยกรรมไทย (วัด ช่อฟ้า ใบระกา เรือนไทย)
- พรอมต์ภาพเป็นภาษาอังกฤษ cinematic photorealistic ระบุ: ใคร/ทำอะไร/ที่ไหน/มุมกล้อง/แสง
- ห้ามใส่ตัวหนังสือในภาพ
- บทบรรยาย (narration) เป็นภาษาไทยพูดลื่น เล่าเรื่องต่อเนื่อง 1 ช็อต = 1-2 ประโยค
- ถ้าตัวละครพูด ใส่ใน dialogue (ไทย) แยกจาก narration

ตอบ JSON เท่านั้น:
{
  "title": "ชื่อเรื่องสั้นๆ",
  "logline": "เรื่องย่อ 1 ประโยค",
  "characters": [
    {"name": "ชื่อ", "ref_prompt": "English: a Thai ... detailed look/clothing for a character reference sheet"}
  ],
  "shots": [
    {
      "idx": 1,
      "image_prompt": "English cinematic photorealistic prompt, Thai-authentic, with camera + lighting, no text",
      "characters": ["ชื่อตัวละครในช็อตนี้"],
      "narration": "บทบรรยายไทยของช็อตนี้",
      "dialogue": [{"speaker": "ชื่อ", "line": "บทพูดไทย"}],
      "motion": "คำสั่งการเคลื่อนไหวสั้นๆ อังกฤษ เช่น slow push-in, gentle pan"
    }
  ]
}"""


async def generate_story(topic: str, n_shots: int = 8, characters: int = 2,
                         style: str = "สมจริง photorealistic", model: str = "") -> dict:
    """Topic → a shot-by-shot Thai story script. n_shots controls length (≈8s/shot)."""
    n = max(3, min(40, int(n_shots or 8)))
    prompt = (
        f"หัวข้อเรื่อง: {topic}\n"
        f"จำนวนช็อต: {n} ช็อต (ช็อตละ ~8 วินาที)\n"
        f"จำนวนตัวละครหลัก: ~{max(1, int(characters or 1))} ตัว\n"
        f"สไตล์ภาพ: {style} — เน้นไทยแท้สมจริงที่สุด\n\n"
        f"เขียนบทเล่าเรื่องให้ครบ {n} ช็อต เรียงต่อเนื่องมีต้น-กลาง-จบ ตอบ JSON ตามรูปแบบ"
    )
    try:
        data = await chat_json(prompt, system=_STORY_SYSTEM, agent="storyteller",
                               preferred_model=(model or _STORY_MODEL))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    title = str(data.get("title") or topic or "เรื่องเล่า").strip()[:120]
    chars = []
    for c in (data.get("characters") or [])[:6]:
        nm = str(c.get("name") or "").strip()[:40]
        rp = str(c.get("ref_prompt") or "").strip()[:400]
        if nm and rp:
            chars.append({"name": nm, "ref_prompt": rp})
    shots = []
    for i, s in enumerate(data.get("shots") or [], start=1):
        ip = str(s.get("image_prompt") or "").strip()[:600]
        if not ip:
            continue
        dlg = [{"speaker": str(d.get("speaker") or "").strip()[:40],
                "line": str(d.get("line") or "").strip()[:300]}
               for d in (s.get("dialogue") or []) if str(d.get("line") or "").strip()][:4]
        shots.append({
            "idx": len(shots) + 1,
            "image_prompt": ip,
            "characters": [str(x).strip()[:40] for x in (s.get("characters") or []) if str(x).strip()][:5],
            "narration": str(s.get("narration") or "").strip()[:400],
            "dialogue": dlg,
            "motion": str(s.get("motion") or "slow cinematic push-in").strip()[:120],
        })
    if not shots:
        return {"ok": False, "error": "โมเดลไม่คืนช็อต — ลองใหม่/เปลี่ยนหัวข้อ"}
    return {"ok": True, "title": title, "logline": str(data.get("logline") or "").strip()[:200],
            "characters": chars, "shots": shots[:n]}
