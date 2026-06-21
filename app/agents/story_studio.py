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

import asyncio
import os
import time

from app.core.ai import chat_json

_STORY_DIR = "/app/data/story"
_SIZE_16x9 = {"width": 1344, "height": 768}
_REAL_STYLE = ("cinematic photorealistic film still, real authentic Thai people and Thai setting, "
               "natural realistic lighting, shot on a cinema camera, shallow depth of field, "
               "rich fine detail, true-to-life. No text, no watermark, no caption")


def _story_style(prompt: str) -> str:
    return f"{prompt}. {_REAL_STYLE}"

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


# ── stage 2: character reference sheets (one clean anchor per character) ──────
async def gen_character_sheets(characters: list[dict], seed: int | None = None) -> dict:
    """One clean full-body+face reference per character → the anchor that locks the face/outfit
    across every shot (fed to Nano Banana). Returns {name: image_path}."""
    from app.agents import aivideo
    os.makedirs(_STORY_DIR, exist_ok=True)

    async def _one(i: int, c: dict) -> tuple[str, str | None]:
        prompt = (c.get("ref_prompt", "") +
                  ", full body and clear face, neutral plain studio background, character reference "
                  "sheet, " + _REAL_STYLE)
        out = os.path.join(_STORY_DIR, f"char_{int(time.time()*1000)}_{i}.png")
        path = await aivideo.generate_image(prompt, out, seed=seed, size=_SIZE_16x9)
        return c.get("name", ""), path

    res = await asyncio.gather(*[_one(i, c) for i, c in enumerate(characters or [])])
    return {n: p for n, p in res if n and p}


# ── stage 3: one image per shot, with the SAME characters via Nano Banana ─────
async def gen_shot_images(shots: list[dict], sheets: dict, seed: int | None = None) -> list[str | None]:
    """For each shot: if characters appear → Nano Banana edit referencing their sheets (same faces);
    else → plain Flux pro. 16:9. Returns paths 1:1 with shots (None where a shot failed)."""
    from app.agents import aivideo
    os.makedirs(_STORY_DIR, exist_ok=True)

    async def _one(shot: dict) -> str | None:
        idx = shot.get("idx", 0)
        out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_{idx}.png")
        refs = [sheets[n] for n in shot.get("characters", []) if n in sheets and sheets[n]]
        if refs:
            edit = ("Keep the EXACT same character(s) from the reference image(s) — identical face, "
                    "body, hair and clothing. Put them into a NEW scene: " + shot.get("image_prompt", "")
                    + ". " + _REAL_STYLE + ". Do not copy the reference background.")
            p = await aivideo.generate_image_edit(edit, refs, out, seed=seed, aspect="16:9")
            if p:
                return p
        # no characters, or the edit failed → plain photorealistic scene
        return await aivideo.generate_image(_story_style(shot.get("image_prompt", "")),
                                            out, seed=seed, size=_SIZE_16x9)

    return list(await asyncio.gather(*[_one(s) for s in (shots or [])]))
