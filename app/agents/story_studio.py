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


# live state for the /admin/story UI (single uvicorn worker → a module global is enough)
STORY_STATE: dict = {"running": False, "log": [], "mp4": "", "title": "", "err": ""}


async def run_story_bg(topic: str, n_shots: int, characters: int, motion: str) -> None:
    """Background runner the UI triggers; mirrors progress into STORY_STATE for polling."""
    STORY_STATE.update(running=True, log=["🚀 เริ่มสร้างเรื่อง…"], mp4="", title="", err="")

    async def _log(m):
        STORY_STATE["log"].append(str(m))

    try:
        import os as _os
        _os.environ.setdefault("FAL_KEY", "")  # ensured loaded at app startup
        r = await make_story(topic, n_shots=n_shots, characters=characters, motion=motion, log=_log)
        if r.get("ok"):
            STORY_STATE.update(mp4=r.get("mp4", ""), title=r.get("title", ""))
        else:
            STORY_STATE["err"] = r.get("error", "")
            STORY_STATE["log"].append("❌ " + r.get("error", ""))
    except Exception as exc:
        STORY_STATE["err"] = str(exc)[:200]
        STORY_STATE["log"].append("❌ " + str(exc)[:200])
    finally:
        STORY_STATE["running"] = False

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


# ── stage 4: narration (single narrator V3 reads each shot) ──────────────────
def _shot_text(shot: dict) -> str:
    t = (shot.get("narration") or "").strip()
    for d in shot.get("dialogue", []):
        line = (d.get("line") or "").strip()
        if line:
            t += (" " + line)
    return t.strip() or "..."


async def narrate_shots(shots: list[dict]) -> tuple[list[str], list[float]]:
    """TTS each shot's narration (reuses the V3/gTTS voice) → (mp3 paths, durations)."""
    from app.agents.vdo_agent import _synth_voice, _audio_duration
    os.makedirs(_STORY_DIR, exist_ok=True)

    def _one(i: int, shot: dict) -> tuple[str, float]:
        out = os.path.join(_STORY_DIR, f"narr_{int(time.time()*1000)}_{i}.mp3")
        try:
            _synth_voice(_shot_text(shot), out)
            return out, max(1.2, _audio_duration(out))
        except Exception:
            return "", 0.0

    res = await asyncio.gather(*[asyncio.to_thread(_one, i, s) for i, s in enumerate(shots or [])])
    paths = [p for p, _ in res]
    durs = [d for _, d in res]
    return paths, durs


# ── stage 5: assemble → mp4 (16:9, Ken Burns on stills or Kling videos) ───────
def assemble_story(visuals: list[tuple[str, str]], narr_paths: list[str],
                   durs: list[float], out_mp4: str, fps: int = 30) -> str:
    """Build the final 16:9 video: each shot's visual (image→Ken Burns zoom, or video) timed to its
    narration, concatenated, with the narration as the audio track. Fail-open → '' on error."""
    import subprocess
    from app.agents.vdo_agent import _concat_audio
    visuals = [(p, k) for (p, k) in visuals if p and os.path.exists(p)]
    if not visuals:
        return ""
    narration = out_mp4 + ".narr.mp3"
    valid_narr = [p for p in narr_paths if p and os.path.exists(p)]
    if valid_narr:
        _concat_audio(valid_narr, narration)
    cmd = ["ffmpeg", "-y"]
    for (path, kind), d in zip(visuals, durs):
        if kind == "image":
            cmd += ["-i", path]
        else:
            cmd += ["-stream_loop", "-1", "-t", f"{max(1.0, d):.3f}", "-i", path]
    has_audio = os.path.exists(narration)
    if has_audio:
        cmd += ["-i", narration]
    n = len(visuals)
    chains = []
    for i, ((path, kind), d) in enumerate(zip(visuals, durs)):
        frames = max(1, int(max(1.0, d) * fps))
        if kind == "image":  # slow cinematic Ken Burns zoom-in at 1080p
            chains.append(
                f"[{i}:v]scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,"
                f"zoompan=z='min(zoom+0.0004,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s=1920x1080:fps={fps},setsar=1[v{i}]")
        else:
            chains.append(
                f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
                f"fps={fps},setpts=PTS-STARTPTS,setsar=1[v{i}]")
    cat = "".join(f"[v{i}]" for i in range(n))
    fc = ";".join(chains) + f";{cat}concat=n={n}:v=1:a=0[vout]"
    cmd += ["-filter_complex", fc, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-b:a", "160k"]
    cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-shortest", out_mp4]
    try:
        subprocess.run(cmd, capture_output=True, timeout=900)
    except Exception:
        return ""
    return out_mp4 if (os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 10000) else ""


# ── orchestrator: topic → finished story mp4 ─────────────────────────────────
async def make_story(topic: str, n_shots: int = 8, characters: int = 2,
                     motion: str = "kenburns", seed: int | None = None, log=None) -> dict:
    """End-to-end: story → character sheets → shot images → narration → assemble → mp4.
    motion: 'kenburns' (free zoom) | 'kling' (animate every shot, pricey)."""
    async def _say(msg):
        if log:
            try:
                await log(msg)
            except Exception:
                pass

    await _say("✍️ เขียนบท…")
    story = await generate_story(topic, n_shots=n_shots, characters=characters)
    if not story.get("ok"):
        return story
    await _say(f"🎭 สร้างชีตตัวละคร {len(story['characters'])} ตัว…")
    sheets = await gen_character_sheets(story["characters"], seed=seed)
    await _say(f"🖼️ สร้างภาพ {len(story['shots'])} ช็อต (ตัวละครคงที่)…")
    images = await gen_shot_images(story["shots"], sheets, seed=seed)
    pairs = list(zip(story["shots"], images))
    pairs = [(s, p) for s, p in pairs if p]
    if not pairs:
        return {"ok": False, "error": "สร้างภาพไม่สำเร็จ"}

    if motion == "kling":
        from app.agents import animate
        await _say("🎬 ทำภาพเคลื่อนไหว (Kling) ทุกช็อต… อาจนานหลายนาที")

        async def _anim(i, p):
            out = os.path.join(_STORY_DIR, f"mv_{int(time.time()*1000)}_{i}.mp4")
            v = await animate.animate_image(p, out)
            return (v, "video") if v else (p, "image")
        visuals = list(await asyncio.gather(*[_anim(i, p) for i, (_s, p) in enumerate(pairs)]))
    else:
        visuals = [(p, "image") for _s, p in pairs]

    await _say("🎙️ พากย์เสียง…")
    narr_paths, durs = await narrate_shots([s for s, _ in pairs])
    await _say("🎬 ตัดต่อ → mp4…")
    out = os.path.join(_STORY_DIR, f"story_{int(time.time())}.mp4")
    mp4 = await asyncio.to_thread(assemble_story, visuals, narr_paths, durs, out)
    if not mp4:
        return {"ok": False, "error": "ตัดต่อไม่สำเร็จ"}
    await _say("✅ เสร็จ!")
    return {"ok": True, "mp4": mp4, "title": story["title"], "shots": len(pairs),
            "logline": story.get("logline", "")}
