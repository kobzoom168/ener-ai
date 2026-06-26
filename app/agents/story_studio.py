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
_SIZE_9x16 = {"width": 768, "height": 1344}


def _aspect_size(aspect: str) -> dict:
    """fal image_size for the chosen format. 9:16 = Shorts/TikTok/Reels (default), 16:9 = ปกติ."""
    return _SIZE_9x16 if str(aspect) == "9:16" else _SIZE_16x9


def _aspect_dims(aspect: str) -> tuple[int, int]:
    """Final mp4 dimensions (w, h)."""
    return (1080, 1920) if str(aspect) == "9:16" else (1920, 1080)


async def fal_balance() -> float | None:
    """Current fal.ai credit balance in USD (so the UI can warn before it runs dry). None on error."""
    import httpx
    key = os.environ.get("FAL_KEY", "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://rest.alpha.fal.ai/billing/user_balance",
                            headers={"Authorization": f"Key {key}"})
            if r.status_code < 300:
                return round(float(r.text.strip()), 2)
    except Exception:
        return None
    return None
_REAL_STYLE = ("cinematic photorealistic film still, real authentic Thai people and Thai setting, "
               "natural realistic lighting, shot on a 35mm cinema camera, shallow depth of field, "
               "fine skin texture and pores, natural imperfections, no plastic skin, "
               "consistent cinematic color grading, filmic, true-to-life photograph. "
               "ABSOLUTELY NO text, no letters, no words, no readable signs or signage, no Thai script, "
               "no captions, no watermark — any sign must be completely blank and unmarked. "
               "No illustration, no 3d render, no cartoon, no anime")


def _story_style(prompt: str) -> str:
    return f"{prompt}. {_REAL_STYLE}"


# live state for the /admin/story storyboard UI (single uvicorn worker → a module global is enough)
STORY_STATE: dict = {"running": False, "log": [], "board": None, "mp4": "", "title": "", "err": ""}

_BOARD_FILE = os.path.join(_STORY_DIR, "_board.json")


def _save_board() -> None:
    """Persist the board so a deploy/restart doesn't wipe the user's storyboard (images stay on disk)."""
    import json
    try:
        os.makedirs(_STORY_DIR, exist_ok=True)
        with open(_BOARD_FILE, "w", encoding="utf-8") as f:
            json.dump({"board": STORY_STATE.get("board"), "title": STORY_STATE.get("title", ""),
                       "mp4": STORY_STATE.get("mp4", "")}, f, ensure_ascii=False)
    except Exception:
        pass


def _load_board() -> None:
    import json
    try:
        with open(_BOARD_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("board"):
            STORY_STATE["board"] = d["board"]
            STORY_STATE["title"] = d.get("title", "")
            STORY_STATE["mp4"] = d.get("mp4", "")
    except Exception:
        pass


_load_board()  # restore last storyboard on import (survives restarts)


# ── ตัวละครเอก (HERO): a persistent signature character reused as the lead in EVERY clip ──
_HERO_DIR = os.path.join(_STORY_DIR, "hero")
_HERO_FILE = os.path.join(_STORY_DIR, "_hero.json")


def get_hero() -> dict | None:
    """The saved signature character {name, desc, images:[paths], enabled}. None if unset/no images."""
    import json
    try:
        with open(_HERO_FILE, encoding="utf-8") as f:
            h = json.load(f)
        h["images"] = [p for p in h.get("images", []) if p and os.path.exists(p)]
        return h if h["images"] else None
    except Exception:
        return None


def save_hero(h: dict) -> None:
    import json
    try:
        os.makedirs(_HERO_DIR, exist_ok=True)
        with open(_HERO_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False)
    except Exception:
        pass


def add_hero_image(path: str) -> None:
    h = get_hero() or {"name": "ตัวละครเอก", "desc": "", "images": [], "enabled": True}
    h["images"] = ([path] + [p for p in h.get("images", []) if p != path])[:4]  # newest first, keep ≤4
    h["enabled"] = True
    save_hero(h)


def set_hero_enabled(on: bool) -> None:
    h = get_hero()
    if h:
        h["enabled"] = bool(on)
        save_hero(h)


def clear_hero() -> None:
    try:
        os.remove(_HERO_FILE)
    except Exception:
        pass


async def gen_hero(desc: str) -> str | None:
    """Generate a brand-new signature character from a text description, then save it as the hero."""
    from app.agents import aivideo
    os.makedirs(_HERO_DIR, exist_ok=True)
    out = os.path.join(_HERO_DIR, f"hero_{int(time.time()*1000)}.png")
    prompt = (desc.strip() + ", full body and clear face, character reference sheet, clean neutral "
              "studio background, " + _REAL_STYLE)
    p = await aivideo.generate_image(prompt, out, size=_SIZE_16x9)
    if p:
        save_hero({"name": "ตัวละครเอก", "desc": desc.strip()[:300], "images": [p], "enabled": True})
    return p


def _hero_refs() -> list[str] | None:
    """Hero image paths if a hero is set AND enabled — used as the lead-character anchor for all shots."""
    h = get_hero()
    if h and h.get("enabled") and h.get("images"):
        return h["images"]
    return None


_HERO_PREVIEW = os.path.join(_HERO_DIR, "_preview.png")


async def preview_hero(scene: str = "") -> str | None:
    """Render ONE sample shot of the saved hero (e.g. the user's real face) as a cinematic character,
    so they can see how it looks before committing. Works even if the hero isn't enabled yet."""
    h = get_hero()
    if not h or not h.get("images"):
        return None
    from app.agents import aivideo
    os.makedirs(_HERO_DIR, exist_ok=True)
    scene = (scene or "a close-up frontal portrait, head and shoulders, looking straight at the camera, "
                      "neutral calm expression, natural soft lighting").strip()
    edit = ("This is the SAME REAL PERSON shown in the reference image(s). Reproduce their face EXACTLY — "
            "identical facial structure, eyes, nose, mouth, eyebrows, jawline, skin tone, wrinkles and "
            "hair. Do NOT beautify, do NOT change age, do NOT alter or average the features — it must "
            "look unmistakably like the same individual. A photorealistic real photograph of this exact "
            "person: " + scene + ". " + _REAL_STYLE)
    return await aivideo.generate_image_edit(edit, h["images"], _HERO_PREVIEW, aspect="9:16", resolution="2K")


# ── ค้นรูปบุคคลจริง (keyless DuckDuckGo image search) → reference for a true-to-life HERO face ──
_SEARCH_DIR = os.path.join(_HERO_DIR, "search")
HERO_SEARCH: dict = {"items": []}
_DDG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def _ddg_headers(referer: str | None = None, accept: str = "*/*") -> dict:
    h = {"User-Agent": _DDG_UA, "Accept": accept, "Accept-Language": "th,en-US;q=0.9,en;q=0.8",
         "X-Requested-With": "XMLHttpRequest", "Sec-Fetch-Dest": "empty",
         "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
    if referer:
        h["Referer"] = referer
    return h


async def search_person_images(query: str, n: int = 8) -> list[dict]:
    """Find real photos of a person via Bing image search (keyless, works from datacenter IPs — DDG's
    i.js 403s from Hetzner). Downloads locally so the UI can show them. Returns [{title, path, source}].
    Biases toward face photos for the real-person HERO use case."""
    import hashlib
    import html as _html
    import json as _json
    import re
    import urllib.parse
    import httpx
    HERO_SEARCH["items"] = []
    q = query.strip()
    if not q:
        return []
    if not any(k in q.lower() for k in ("รูป", "ภาพ", "portrait", "photo", "หน้า")):
        q = q + " รูปถ่าย portrait"
    os.makedirs(_SEARCH_DIR, exist_ok=True)
    for f in os.listdir(_SEARCH_DIR):
        try:
            os.remove(os.path.join(_SEARCH_DIR, f))
        except Exception:
            pass
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    burl = "https://www.bing.com/images/search?q=" + urllib.parse.quote(q) + "&form=HDRSC2&first=1"
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            page = (await c.get(burl, headers={"User-Agent": ua,
                                               "Accept-Language": "th,en-US;q=0.9,en;q=0.8"})).text
            metas = re.findall(r'\sm="([^"]+)"', page)  # iusc anchors carry HTML-escaped JSON
            slug = hashlib.md5(q.encode()).hexdigest()[:6]
            seen: set = set()
            items: list[dict] = []
            for meta in metas:
                if len(items) >= n:
                    break
                try:
                    d = _json.loads(_html.unescape(meta))
                except Exception:
                    continue
                murl = d.get("murl") or ""
                turl = d.get("turl") or ""
                if not (murl or turl) or (murl or turl) in seen:
                    continue
                seen.add(murl or turl)
                title = (d.get("t") or "รูป").replace("\n", " ").strip()[:70]
                cands = []
                if murl:
                    cands.append((murl, "https://" + urllib.parse.urlparse(murl).netloc + "/"))
                if turl:
                    cands.append((turl, "https://www.bing.com/"))  # Bing CDN — reliable fallback
                got = None
                for cand, ref in cands:
                    try:
                        dd = (await c.get(cand, headers={"User-Agent": ua, "Referer": ref,
                                                         "Accept": "image/*,*/*"})).content
                        if dd and len(dd) > 1500:
                            got = dd
                            break
                    except Exception:
                        pass
                if not got:
                    continue
                ext = ".jpg"
                if got[:4] == b"\x89PNG":
                    ext = ".png"
                elif got[:4] == b"RIFF" and got[8:12] == b"WEBP":
                    ext = ".webp"
                p = os.path.join(_SEARCH_DIR, f"{slug}_{len(items)+1}{ext}")
                with open(p, "wb") as fh:
                    fh.write(got)
                items.append({"title": title, "path": p, "source": d.get("purl") or ""})
            HERO_SEARCH["items"] = items
            return items
    except Exception:
        return []


def use_search_image(i: int) -> bool:
    """Add a searched photo (by index) to the hero reference set."""
    import shutil
    items = HERO_SEARCH.get("items", [])
    if not (0 <= i < len(items)):
        return False
    src = items[i].get("path", "")
    if not src or not os.path.exists(src):
        return False
    os.makedirs(_HERO_DIR, exist_ok=True)
    dst = os.path.join(_HERO_DIR, f"hero_{int(time.time()*1000)}_{i}.png")
    shutil.copy(src, dst)
    add_hero_image(dst)
    return True


async def _render_shots_with_refs(shots: list[dict], ref_imgs: list[str], seed: int | None,
                                  aspect: str = "9:16", protagonist: str = "") -> list[str | None]:
    """Render shots with the hero as the lead. The hero is injected ONLY into shots where the
    protagonist actually appears (from the shot's `characters`); scene/other-character shots are
    rendered plainly from the prompt so the picture matches the script. If `protagonist` is empty
    (import / no named lead) the hero goes into every shot. Fail-open → plain shot."""
    from app.agents import aivideo
    os.makedirs(_STORY_DIR, exist_ok=True)
    size = _aspect_size(aspect)
    pl = (protagonist or "").strip().lower()

    async def _img(s):
        out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_{s.get('idx', 0)}.png")
        chars = [str(c).strip().lower() for c in (s.get("characters") or [])]
        hero_here = bool(ref_imgs) and ((not pl) or any(pl == c or pl in c for c in chars))
        if hero_here:
            edit = ("Keep the SAME main character as the reference image(s) — identical face, hair, "
                    "outfit and overall design. Place this character into a NEW scene: "
                    + s.get("image_prompt", "") + ". " + _REAL_STYLE + ". Do not copy the reference background.")
            p = await aivideo.generate_image_edit(edit, ref_imgs, out, seed=seed, aspect=aspect)
            if p:
                return p
        return await aivideo.generate_image(_story_style(s.get("image_prompt", "")), out,
                                            seed=seed, size=size)

    return list(await asyncio.gather(*[_img(s) for s in shots]))


def _log_state(m):
    STORY_STATE["log"].append(str(m))


async def run_board_bg(topic: str, n_shots: int, characters: int, model: str,
                       aspect: str = "9:16", genre: str = "", location: str = "") -> None:
    """Stage 1-3 → a STORYBOARD (shots with image + editable script + narration). Per-shot upload /
    regenerate / assemble happen via separate endpoints afterward."""
    STORY_STATE.update(running=True, log=["🚀 สร้างสตอรี่บอร์ด…"], board=None, mp4="", title="", err="")
    try:
        from app.agents import aivideo
        _log_state("✍️ เขียนบท…")
        story = await generate_story(topic, n_shots=n_shots, characters=characters, model=model,
                                     genre=genre, location=location)
        if not story.get("ok"):
            STORY_STATE["err"] = story.get("error", "")
            _log_state("❌ " + story.get("error", "")); return
        hero = _hero_refs()
        if hero:  # ตัวละครเอก = ตัวเอก (ใส่เฉพาะช็อตที่ตัวเอกอยู่ ช็อตฉาก/คนอื่นสร้างตามบท)
            protagonist = (story["characters"][0].get("name", "") if story.get("characters") else "")
            _log_state(f"🎭 ใส่ตัวละครเอก ({protagonist or 'ทุกช็อต'}) เฉพาะช็อตที่ตัวเอกอยู่…")
            images = await _render_shots_with_refs(story["shots"], hero, seed=_seed(topic),
                                                   aspect=aspect, protagonist=protagonist)
        else:
            _log_state(f"🎭 สร้างชีตตัวละคร {len(story['characters'])} ตัว…")
            sheets = await gen_character_sheets(story["characters"], seed=_seed(topic), aspect=aspect)
            _log_state(f"🖼️ สร้างภาพ {len(story['shots'])} ช็อต (ตัวละครคงที่)…")
            images = await gen_shot_images(story["shots"], sheets, seed=_seed(topic), aspect=aspect)
        shots = []
        for s, img in zip(story["shots"], images):
            shots.append({**s, "image": img or "", "video": ""})
        STORY_STATE["board"] = {"title": story["title"], "logline": story.get("logline", ""),
                                "aspect": aspect,
                                "characters": story["characters"], "shots": shots}
        STORY_STATE["title"] = story["title"]
        _save_board()
        _log_state(f"✅ สตอรี่บอร์ดเสร็จ {len(shots)} ช็อต — แก้/อัปวิดีโอ แล้วกดตัดต่อ")
    except Exception as exc:
        STORY_STATE["err"] = str(exc)[:200]
        _log_state("❌ " + str(exc)[:200])
    finally:
        STORY_STATE["running"] = False


import zlib as _zlib


def _seed(topic: str) -> int:
    return _zlib.crc32((topic or "story").encode()) % 2147483647


def _mk_shot(idx: int, prompt: str, narr: str) -> dict:
    return {"idx": idx, "image_prompt": (prompt or narr).strip()[:600],
            "narration": (narr or "").strip()[:400], "dialogue": [],
            "motion": "slow cinematic push-in", "characters": []}


_HDR_RE = __import__("re").compile(r"prompt|visual scene|voiceover|ช็อตที่|ข้อความบรรยาย", __import__("re").I)
_QUOTE_RE = __import__("re").compile(r"[\"'“”„«»]{1,3}(.+?)[\"'“”„«»]{1,3}", __import__("re").S)
_TRAIL_EN_RE = __import__("re").compile(r"([A-Za-z][A-Za-z0-9 ,\.\-:;()/]{14,})\s*$")


def _parse_line_heuristic(line: str) -> dict | None:
    """One pasted row → (image_prompt, narration) without relying on column splitting. The English
    cinematic prompt is the trailing Latin block; the Thai narration is the quoted segment."""
    import re
    s = (line or "").strip().strip(",").strip()
    if not s:
        return None
    s = re.sub(r"^\s*\d+\s*[\.\)\,\t|]*\s*", "", s)  # drop leading shot number
    narr, prompt = "", ""
    m = _QUOTE_RE.search(s)
    if m:  # narration is the quoted Thai; prompt is whatever Latin trails it
        narr = m.group(1).strip()
        tail = s[m.end():].strip().strip(",").strip()
        prompt = tail or s[:m.start()].strip()
    else:  # no quotes → split Thai (front) vs trailing English prompt
        em = _TRAIL_EN_RE.search(s)
        if em:
            prompt = em.group(1).strip()
            narr = s[:em.start()].strip().strip(",").strip()
        else:
            prompt = narr = s
    if not (prompt or narr):
        return None
    return _mk_shot(0, prompt, narr)


def parse_script_table(text: str) -> list[dict]:
    """Parse a pasted shot table into shots. Tries clean CSV/TSV columns first (Prompt→image_prompt,
    บรรยาย/voiceover→narration); if the columns collapse (space-aligned paste, merged header), falls
    back to a per-line heuristic that uses quotes + Thai/English to separate narration vs prompt."""
    import csv
    import io
    text = (text or "").strip()
    if not text:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]

    # ── attempt 1: real delimited table (comma or tab), only trusted if columns are distinct ──
    for delim in ("\t", ","):
        if sum(ln.count(delim) for ln in lines) < len(lines):
            continue
        rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim) if any(str(c).strip() for c in r)]
        if not rows or max(len(r) for r in rows) < 3:
            continue
        header = [str(h).strip().lower() for h in rows[0]]

        def _find(keys, hdr=header):
            for i, h in enumerate(hdr):
                if any(k in h for k in keys):
                    return i
            return -1
        pi = _find(["prompt"])
        ni = _find(["บรรยาย", "voiceover", "narration", "script", "เสียง"])
        if pi >= 0 and ni >= 0 and pi != ni:  # distinct columns → trustworthy
            shots = []
            for r in rows[1:]:
                cells = [str(c).strip().strip('"').strip("'").strip() for c in r]
                pr = (cells[pi] if pi < len(cells) else "").strip()
                nr = (cells[ni] if ni < len(cells) else "").strip()
                if pr or nr:
                    shots.append(_mk_shot(len(shots) + 1, pr or nr, nr))
            if shots:
                return shots
        break

    # ── attempt 2: heuristic per-line (handles Excel/space paste & merged columns) ──
    _hdr_markers = ("visual scene", "voiceover", "ช็อตที่", "prompt สำหรับ", "ข้อความบรรยาย")
    shots = []
    for ln in lines:
        low = ln.lower()
        if sum(1 for mk in _hdr_markers if mk in low) >= 2:
            continue  # column-title header row
        s = _parse_line_heuristic(ln)
        if s:
            s["idx"] = len(shots) + 1
            shots.append(s)
    return shots


async def run_import_bg(text: str, aspect: str = "9:16") -> None:
    """Import a user's own shot table → generate one image per shot → storyboard (no AI scripting)."""
    STORY_STATE.update(running=True, log=["📥 นำเข้าสคริปต์…"], board=None, mp4="", title="", err="")
    try:
        shots = parse_script_table(text)
        if not shots:
            STORY_STATE["err"] = "แยกตารางไม่ได้ — ตรวจรูปแบบ (CSV: ช็อต,เนื้อหา,บรรยาย,Prompt)"
            _log_state("❌ " + STORY_STATE["err"]); return
        from app.agents import aivideo
        os.makedirs(_STORY_DIR, exist_ok=True)
        seed = _seed(text[:60])
        size = _aspect_size(aspect)
        hero = _hero_refs()

        if hero:  # ── ตัวละครเอก: every shot is THIS character (across all clips) ──
            _log_state(f"🎭 ใช้ตัวละครเอกของคุณ — ตัวหลักทุกช็อต ({len(shots)} ช็อต)…")
            imgs = await _render_shots_with_refs(shots, hero, seed, aspect=aspect)
        else:  # ── no hero → shot 1 establishes the character, rest lock to it ──
            _log_state("🖼️ ช็อต 1 — ตั้งตัวละคร/โทน (ตัวจำ)…")
            a_out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_1.png")
            anchor = await aivideo.generate_image(_story_style(shots[0]["image_prompt"]), a_out,
                                                  seed=seed, size=size)

            async def _img(s):
                out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_{s['idx']}.png")
                if anchor:
                    edit = ("Keep the SAME main character(s), wardrobe, art style, color grade and world "
                            "as the reference image — same identity. Now show a NEW connected scene: "
                            + s["image_prompt"] + ". " + _REAL_STYLE + ". Do not copy the reference background.")
                    p = await aivideo.generate_image_edit(edit, [anchor], out, seed=seed, aspect=aspect)
                    if p:
                        return p
                return await aivideo.generate_image(_story_style(s["image_prompt"]), out, seed=seed, size=size)

            if len(shots) > 1:
                _log_state(f"🔗 ล็อกตัวละครให้ต่อเนื่องอีก {len(shots)-1} ช็อต…")
            rest = await asyncio.gather(*[_img(s) for s in shots[1:]])
            imgs = [anchor, *rest]
        board_shots = [{**s, "image": img or "", "video": ""} for s, img in zip(shots, imgs)]
        STORY_STATE["board"] = {"title": "สคริปต์นำเข้า", "logline": "", "aspect": aspect,
                                "characters": [], "shots": board_shots}
        STORY_STATE["title"] = "สคริปต์นำเข้า"
        _save_board()
        _log_state(f"✅ นำเข้า {len(board_shots)} ช็อต — แก้/รีเจน/อัปวิดีโอ แล้วกดตัดต่อ")
    except Exception as exc:
        STORY_STATE["err"] = str(exc)[:200]; _log_state("❌ " + str(exc)[:200])
    finally:
        STORY_STATE["running"] = False


def update_shot(idx: int, image_prompt: str | None = None, narration: str | None = None) -> bool:
    """Edit a shot's image prompt and/or narration (user override). Regenerate after to apply the
    new prompt to the picture; narration is used at assemble time."""
    b = STORY_STATE.get("board")
    if not b:
        return False
    for s in b["shots"]:
        if s.get("idx") == idx:
            if image_prompt is not None:
                s["image_prompt"] = str(image_prompt).strip()[:600]
            if narration is not None:
                s["narration"] = str(narration).strip()[:400]
            _save_board()
            return True
    return False


def set_shot_video(idx: int, path: str) -> bool:
    """Attach an uploaded mp4 to a shot (overrides AI image for that shot)."""
    b = STORY_STATE.get("board")
    if not b:
        return False
    for s in b["shots"]:
        if s.get("idx") == idx:
            s["video"] = path
            _save_board()
            return True
    return False


async def regen_shot(idx: int) -> bool:
    """Re-generate one shot's image from its (possibly edited) prompt, keeping character continuity:
    AI boards use the character sheets; imported boards anchor on shot 1's image (Nano Banana)."""
    b = STORY_STATE.get("board")
    if not b:
        return False
    shot = next((s for s in b["shots"] if s.get("idx") == idx), None)
    if not shot:
        return False
    new = None
    if b.get("characters"):  # AI-generated board → re-use the character reference sheets
        sheets = await gen_character_sheets(b["characters"], seed=_seed(b["title"]))
        imgs = await gen_shot_images([shot], sheets, seed=_seed(str(idx)))
        new = imgs[0] if imgs else None
    else:  # imported board → lock to shot 1 so the regenerated shot still matches the others
        from app.agents import aivideo
        out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_{idx}.png")
        anchor = b["shots"][0].get("image") if b.get("shots") else ""
        if anchor and os.path.exists(anchor) and anchor != shot.get("image"):
            edit = ("Keep the SAME main character(s), wardrobe, style and world as the reference "
                    "image. New connected scene: " + shot.get("image_prompt", "") + ". " + _REAL_STYLE
                    + ". Do not copy the reference background.")
            new = await aivideo.generate_image_edit(edit, [anchor], out, seed=_seed(str(idx)), aspect="16:9")
        if not new:
            new = await aivideo.generate_image(_story_style(shot.get("image_prompt", "")), out,
                                               seed=_seed(str(idx)), size=_SIZE_16x9)
    if new:
        shot["image"] = new
        shot["video"] = ""
        _save_board()
        return True
    return False


async def assemble_board_bg(motion: str) -> None:
    """Stage 4-5 on the current board: narrate + assemble (uploaded video > Kling > Ken Burns)."""
    b = STORY_STATE.get("board")
    if not b:
        STORY_STATE["err"] = "ยังไม่มีสตอรี่บอร์ด"; return
    STORY_STATE.update(running=True, mp4="", err="")
    STORY_STATE["log"] = STORY_STATE.get("log", []) + ["🎬 เริ่มตัดต่อ…"]
    try:
        shots = [s for s in b["shots"] if s.get("image") or s.get("video")]
        if motion == "talk":  # ── ละครพูด: dialogue → OmniHuman lip-sync ──
            aspect = b.get("aspect", "16:9")
            _log_state("🗣️ ละครพูด — ลิปซิงค์ช็อตที่มีบทพูด (OmniHuman) อาจนานหลายนาที…")

            async def _alog(m):
                _log_state(m)
            out = os.path.join(_STORY_DIR, f"story_{int(time.time())}.mp4")
            mp4 = await assemble_talking(shots, b.get("characters", []), out, aspect, log=_alog)
            if mp4:
                STORY_STATE["mp4"] = mp4
                _save_board()
                _log_state("✅ คลิปละครพูดเสร็จ!")
            else:
                STORY_STATE["err"] = "ตัดต่อไม่สำเร็จ"
                _log_state("❌ ตัดต่อไม่สำเร็จ")
            return
        if motion == "kling":
            from app.agents import animate
            _log_state("🎬 Kling ทำภาพเคลื่อนไหว (ตัวละครขยับจริง)… อาจนานหลายนาที")

            async def _vis(i, s):
                if s.get("video") and os.path.exists(s["video"]):
                    return (s["video"], "video")
                out = os.path.join(_STORY_DIR, f"mv_{int(time.time()*1000)}_{i}.mp4")
                hint = str(s.get("motion") or "").strip()
                mprompt = ("cinematic character animation: the character moves and acts naturally — "
                           "subtle head/eye/hand motion, breathing, clothing and hair sway, living "
                           "expression; gentle camera move; "
                           + (hint + "; " if hint else "")
                           + "smooth realistic motion, consistent identity, no morphing")
                v = await animate.animate_image(s["image"], out, prompt=mprompt)
                return (v, "video") if v else (s["image"], "image")
            visuals = list(await asyncio.gather(*[_vis(i, s) for i, s in enumerate(shots)]))
        else:
            visuals = [((s["video"], "video") if s.get("video") and os.path.exists(s["video"])
                        else (s["image"], "image")) for s in shots]
        _log_state("🎙️ พากย์เสียง…")
        narr_paths, durs = await narrate_shots(shots)
        aspect = b.get("aspect", "16:9")  # boards made before this feature were 16:9
        _log_state(f"🎬 ตัดต่อ → mp4 ({aspect})…")
        out = os.path.join(_STORY_DIR, f"story_{int(time.time())}.mp4")
        mp4 = await asyncio.to_thread(assemble_story, visuals, narr_paths, durs, out, 30, aspect)
        if mp4:
            STORY_STATE["mp4"] = mp4
            _save_board()
            _log_state("✅ คลิปเสร็จ!")
        else:
            STORY_STATE["err"] = "ตัดต่อไม่สำเร็จ"; _log_state("❌ ตัดต่อไม่สำเร็จ")
    except Exception as exc:
        STORY_STATE["err"] = str(exc)[:200]; _log_state("❌ " + str(exc)[:200])
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
- ห้ามให้ image_prompt มีป้าย/ตัวหนังสือที่อ่านได้ (โมเดลภาพเขียนอักษรไทยไม่ได้ จะเพี้ยน) — เลี่ยงฉากที่ต้องมีป้ายชื่อร้าน/ป้ายบอกทาง/ตัวอักษร หรือถ้าจำเป็นให้ระบุว่าป้ายเปล่าไม่มีตัวอักษร (blank unmarked sign)
- บทบรรยาย (narration) เป็นภาษาไทยพูดลื่น เล่าเรื่องต่อเนื่อง 1 ช็อต = 1-2 ประโยค
- ถ้าตัวละครพูด ใส่ใน dialogue (ไทย) แยกจาก narration

ตอบ JSON เท่านั้น:
{
  "title": "ชื่อเรื่องสั้นๆ",
  "logline": "เรื่องย่อ 1 ประโยค",
  "characters": [
    {"name": "ชื่อ", "gender": "ชาย หรือ หญิง", "ref_prompt": "English: a Thai ... detailed look/clothing for a character reference sheet"}
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


_IDEA_SYSTEM = """คุณคือนักเขียนบทไทยครีเอทีฟ ช่วยคิด "ไอเดียเรื่องสั้นแนวละคร" สำหรับคลิปวิดีโอแนวตั้ง ~2 นาที
- แนวหลากหลาย: นิทานพื้นบ้าน/ตำนานไทย/พุทธประวัติ/ดราม่าชีวิต/ลึกลับสยอง/ความเชื่อสายมู/รักโศก
- แต่ละเรื่องมี hook แรงน่ากดดู เล่าจบใน 2 นาทีได้ ฉากไทยสมจริง
ตอบ JSON เท่านั้น: {"ideas":[{"title":"ชื่อเรื่องสั้นกระชับ","hook":"เรื่องย่อ/มุกเด็ด 1 ประโยคสะดุดใจ"}]}"""


async def suggest_ideas(genre: str = "", n: int = 4, model: str = "") -> list[dict]:
    """Brainstorm a few Thai short-drama story ideas (title + hook) for the 'ให้ AI คิดเรื่อง' button."""
    n = max(2, min(6, int(n or 4)))
    tone = _GENRE_TONE.get(str(genre).strip().lower(), "")
    prompt = (f"คิดไอเดียเรื่องสั้นไทยแนวละคร {n} เรื่องที่ต่างกัน"
              + (f" — ทุกเรื่องเป็น{tone}" if tone else " คละแนว")
              + " สำหรับคลิปแนวตั้ง ~2 นาที — ตอบ JSON ตามรูปแบบ")
    try:
        data = await chat_json(prompt, system=_IDEA_SYSTEM, agent="storyteller",
                               preferred_model=(model or _STORY_MODEL))
    except Exception:
        return []
    out = []
    for it in (data.get("ideas") or [])[:n]:
        if not isinstance(it, dict):
            continue
        t = str(it.get("title") or "").strip()
        h = str(it.get("hook") or "").strip()
        if t:
            out.append({"title": t[:90], "hook": h[:160]})
    return out


_GENRE_TONE = {
    "horror": "แนวสยองขวัญ/ลึกลับ — บรรยากาศหลอน ตึงเครียด มืด เงา หมอก จังหวะค่อยๆ บีบหัวใจ มีจุดผวา/หักมุมน่ากลัว",
    "drama": "แนวดราม่าชีวิต — อารมณ์ลึก ซึ้ง กินใจ ความสัมพันธ์/บทเรียนชีวิต จบแบบสะเทือนใจหรือให้ข้อคิด",
    "comedy": "แนวตลก/ฮา — เบาสมอง มุกตลก สถานการณ์พลิกขำ จังหวะสนุก ตัวละครกวนๆ",
}


async def generate_story(topic: str, n_shots: int = 8, characters: int = 2,
                         style: str = "สมจริง photorealistic", model: str = "",
                         genre: str = "", location: str = "") -> dict:
    """Topic → a shot-by-shot Thai story script. n_shots controls length (≈8s/shot)."""
    n = max(3, min(50, int(n_shots or 8)))
    tone = _GENRE_TONE.get(str(genre).strip().lower(), "")
    loc = (location or "").strip()
    prompt = (
        f"หัวข้อเรื่อง: {topic}\n"
        f"จำนวนช็อต: {n} ช็อต (ช็อตละ ~8 วินาที)\n"
        f"จำนวนตัวละครหลัก: ~{max(1, int(characters or 1))} ตัว\n"
        + (f"แนวเรื่อง: {tone}\n" if tone else "")
        + (f"สถานที่/ฉากหลัก: {loc} — ทุก image_prompt ต้องอยู่ที่สถานที่นี้ ให้ฉาก/โทนสี/บรรยากาศเดียวกันทั้งเรื่อง\n" if loc else "")
        + f"สไตล์ภาพ: {style} — เน้นไทยแท้สมจริงที่สุด\n\n"
        f"เขียนบทเล่าเรื่องให้ครบ {n} ช็อต เรียงต่อเนื่องมีต้น-กลาง-จบ"
        + (" คุมโทน/อารมณ์ตามแนวเรื่องตลอดทั้งเรื่อง" if tone else "")
        + " ตอบ JSON ตามรูปแบบ"
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
            chars.append({"name": nm, "gender": str(c.get("gender") or "").strip()[:20], "ref_prompt": rp})
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
async def gen_character_sheets(characters: list[dict], seed: int | None = None,
                               aspect: str = "9:16") -> dict:
    """One clean full-body+face reference per character → the anchor that locks the face/outfit
    across every shot (fed to Nano Banana). Returns {name: image_path}."""
    from app.agents import aivideo
    os.makedirs(_STORY_DIR, exist_ok=True)
    size = _aspect_size(aspect)

    async def _one(i: int, c: dict) -> tuple[str, str | None]:
        prompt = (c.get("ref_prompt", "") +
                  ", full body and clear face, neutral plain studio background, character reference "
                  "sheet, " + _REAL_STYLE)
        out = os.path.join(_STORY_DIR, f"char_{int(time.time()*1000)}_{i}.png")
        path = await aivideo.generate_image(prompt, out, seed=seed, size=size)
        return c.get("name", ""), path

    res = await asyncio.gather(*[_one(i, c) for i, c in enumerate(characters or [])])
    return {n: p for n, p in res if n and p}


# ── stage 3: one image per shot, with the SAME characters via Nano Banana ─────
async def gen_shot_images(shots: list[dict], sheets: dict, seed: int | None = None,
                          aspect: str = "9:16") -> list[str | None]:
    """For each shot: if characters appear → Nano Banana edit referencing their sheets (same faces);
    else → plain Flux pro. Returns paths 1:1 with shots (None where a shot failed)."""
    from app.agents import aivideo
    os.makedirs(_STORY_DIR, exist_ok=True)
    size = _aspect_size(aspect)

    async def _one(shot: dict) -> str | None:
        idx = shot.get("idx", 0)
        out = os.path.join(_STORY_DIR, f"shot_{int(time.time()*1000)}_{idx}.png")
        refs = [sheets[n] for n in shot.get("characters", []) if n in sheets and sheets[n]]
        if refs:
            edit = ("Keep the EXACT same character(s) from the reference image(s) — identical face, "
                    "body, hair and clothing. Put them into a NEW scene: " + shot.get("image_prompt", "")
                    + ". " + _REAL_STYLE + ". Do not copy the reference background.")
            p = await aivideo.generate_image_edit(edit, refs, out, seed=seed, aspect=aspect)
            if p:
                return p
        # no characters, or the edit failed → plain photorealistic scene
        return await aivideo.generate_image(_story_style(shot.get("image_prompt", "")),
                                            out, seed=seed, size=size)

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


# ── stage 5: assemble → mp4 (9:16 Shorts/TikTok by default, or 16:9) ──────────
def assemble_story(visuals: list[tuple[str, str]], narr_paths: list[str],
                   durs: list[float], out_mp4: str, fps: int = 30, aspect: str = "9:16") -> str:
    """Build the final video at the chosen aspect. Renders EACH shot to a small temp clip then concats
    them (the whole thing in one filter_complex OOM-killed ffmpeg at ~5GB on the 7.5GB box). Each shot
    = image→Ken Burns zoom or looped video, timed to its narration; audio = the narration track.
    Checks ffmpeg returncode at every step + faststart. Fail-open → ''."""
    import subprocess
    from app.agents.vdo_agent import _concat_audio
    W, H = _aspect_dims(aspect)
    BW, BH = int(W * 1.25), int(H * 1.25)  # modest oversample for the zoom headroom (low memory)
    visuals = [(p, k) for (p, k) in visuals if p and os.path.exists(p)]
    if not visuals:
        return ""
    workdir = out_mp4 + "_parts"
    os.makedirs(workdir, exist_ok=True)

    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=300).returncode == 0
        except Exception:
            return False

    parts = []
    for i, ((path, kind), d) in enumerate(zip(visuals, durs)):
        d = max(1.0, float(d))
        clip = os.path.join(workdir, f"p{i:03d}.mp4")
        if kind == "image":  # slow cinematic Ken Burns zoom-in — one shot at a time = little RAM
            # NOTE: -t must be an OUTPUT option here. As an input option before -loop it makes ffmpeg
            # feed d*fps image frames and zoompan (d frames/input-frame) then explodes the duration.
            vf = (f"scale={BW}:{BH}:force_original_aspect_ratio=increase,crop={BW}:{BH},"
                  f"zoompan=z='min(zoom+0.0004,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                  f"d={max(1, int(d*fps))}:s={W}x{H}:fps={fps},setsar=1")
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", path, "-vf", vf, "-t", f"{d:.3f}"]
        else:
            cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-t", f"{d:.3f}", "-i", path,
                   "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1"]
        cmd += ["-an", "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-threads", "2", clip]
        if _run(cmd) and os.path.exists(clip) and os.path.getsize(clip) > 3000:
            parts.append(clip)

    if not parts:
        return ""
    listf = os.path.join(workdir, "list.txt")
    with open(listf, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p)

    narration = out_mp4 + ".narr.mp3"
    valid_narr = [p for p in narr_paths if p and os.path.exists(p)]
    if valid_narr:
        _concat_audio(valid_narr, narration)
    has_audio = os.path.exists(narration)

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf]
    if has_audio:
        cmd += ["-i", narration]
    cmd += ["-c:v", "copy"]  # parts already encoded identically → fast, low-memory concat
    if has_audio:
        cmd += ["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    cmd += ["-movflags", "+faststart", out_mp4]
    ok = _run(cmd)

    try:  # cleanup temp parts
        for p in parts:
            os.remove(p)
        os.remove(listf)
        os.rmdir(workdir)
    except Exception:
        pass
    return out_mp4 if (ok and os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 10000) else ""


# ── ละครพูด (talking drama): dialogue shots → OmniHuman lip-sync ──────────────
_VOICE_FEMALE = "Z3R5wn05IrDiVCyEkUrK"  # ElevenLabs female (กบ supplied)
_VOICE_MALE = "UmQN7jS1Ee8B1czsUtQh"    # ElevenLabs male


def _voice_for(speaker: str, characters: list[dict]) -> str:
    """Pick an ElevenLabs voice for a dialogue speaker by the character's gender. '' = cloned default."""
    sp = (speaker or "").strip().lower()
    for c in characters or []:
        if str(c.get("name", "")).strip().lower() == sp:
            g = str(c.get("gender", "")).strip().lower()
            if "หญิง" in g or "female" in g or "woman" in g:
                return _VOICE_FEMALE
            if "ชาย" in g or "male" in g or "man" in g:
                return _VOICE_MALE
    return ""


async def assemble_talking(shots: list[dict], characters: list[dict], out_mp4: str,
                           aspect: str = "9:16", fps: int = 30, log=None) -> str:
    """Build a talking-drama: shots WITH dialogue → OmniHuman lip-sync (per-speaker voice); other shots
    → Ken Burns + narration voice. Every part is re-encoded uniform (WxH/fps/H264/AAC) so concat-copy
    works. OmniHuman clip carries its own dialogue audio. Fail-open → ''."""
    import subprocess
    from app.agents import aivideo
    from app.agents.vdo_agent import _audio_duration, _synth_voice

    async def _say(m):
        if log:
            try:
                await log(m)
            except Exception:
                pass

    W, H = _aspect_dims(aspect)
    BW, BH = int(W * 1.25), int(H * 1.25)
    workdir = out_mp4 + "_talk"
    os.makedirs(workdir, exist_ok=True)

    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, timeout=400).returncode == 0
        except Exception:
            return False

    parts = []
    shots = [s for s in shots if s.get("image") and os.path.exists(s["image"])]
    for i, s in enumerate(shots):
        img = s["image"]
        clip = os.path.join(workdir, f"t{i:03d}.mp4")
        dlg = s.get("dialogue") or []
        spoken = " ".join(str(d.get("line", "")).strip() for d in dlg
                          if str(d.get("line", "")).strip()).strip()
        done = False
        if spoken:  # ── talking shot → OmniHuman lip-sync ──
            await _say(f"🗣️ ช็อต {s.get('idx', i + 1)}: ลิปซิงค์…")
            voice = _voice_for(dlg[0].get("speaker") if dlg else "", characters)
            aud = os.path.join(workdir, f"a{i:03d}.mp3")
            try:
                _synth_voice(spoken[:600], aud, voice)
            except Exception:
                aud = ""
            omni = os.path.join(workdir, f"o{i:03d}.mp4")
            ov = await aivideo.omnihuman(img, aud, omni) if (aud and os.path.exists(aud)) else None
            if ov and _run([
                    "ffmpeg", "-y", "-i", ov,
                    "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},setsar=1",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", "-threads", "2", clip]):
                if os.path.exists(clip) and os.path.getsize(clip) > 5000:
                    parts.append(clip)
                    done = True
        if not done:  # ── narration / fallback → Ken Burns + voice ──
            narr = (s.get("narration") or spoken or "").strip()
            aud = os.path.join(workdir, f"n{i:03d}.mp3")
            d = 3.0
            if narr:
                try:
                    _synth_voice(narr[:400], aud)
                    d = max(1.5, _audio_duration(aud))
                except Exception:
                    aud = ""
            else:
                aud = ""
            vf = (f"scale={BW}:{BH}:force_original_aspect_ratio=increase,crop={BW}:{BH},"
                  f"zoompan=z='min(zoom+0.0004,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                  f"d={max(1, int(d * fps))}:s={W}x{H}:fps={fps},setsar=1")
            if aud and os.path.exists(aud):
                cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img, "-i", aud, "-vf", vf, "-t", f"{d:.3f}",
                       "-map", "0:v", "-map", "1:a"]
            else:
                cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img, "-f", "lavfi", "-i",
                       "anullsrc=channel_layout=stereo:sample_rate=44100", "-vf", vf, "-t", f"{d:.3f}",
                       "-map", "0:v", "-map", "1:a"]
            cmd += ["-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "160k", "-shortest",
                    "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-threads", "2", clip]
            if _run(cmd) and os.path.exists(clip) and os.path.getsize(clip) > 5000:
                parts.append(clip)

    if not parts:
        return ""
    listf = os.path.join(workdir, "list.txt")
    with open(listf, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p)
    ok = _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listf,
               "-c", "copy", "-movflags", "+faststart", out_mp4])
    try:
        for p in os.listdir(workdir):
            os.remove(os.path.join(workdir, p))
        os.rmdir(workdir)
    except Exception:
        pass
    return out_mp4 if (ok and os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 10000) else ""


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
