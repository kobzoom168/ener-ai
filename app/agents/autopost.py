"""Auto-post core: schedules that render a short and publish it to Facebook / YouTube /
TikTok — each platform with its own posting time. Posting goes directly through our own
clients (facebook_client); Postiz is no longer required.

A schedule:
  {id, label, content_type, topic, days:[0..6], enabled,
   platforms: [{name:'facebook', time:'18:00', enabled:true}, ...],
   _state: {gen_date, mp4, caption, title, last_run:{facebook:'YYYY-MM-DD', ...}}}

run_due() (minute tick) fires each enabled platform when its Bangkok time matches now; the
clip is rendered once per schedule per day and reused across the staggered platform times.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.database import get_config, set_config, get_db
from app.core.pipeline_status import set_status

_BANGKOK = ZoneInfo("Asia/Bangkok")
_SCHED_KEY = "vdo_autopost_schedules"
_LOG_KEY = "vdo_autopost_log"
_LOG_MAX = 60

PLATFORMS = ["facebook", "youtube", "tiktok"]
PLATFORM_LABEL = {"facebook": "📘 Facebook", "youtube": "▶️ YouTube", "tiktok": "🎵 TikTok"}


def platform_status() -> dict:
    """Which platforms are actually connected/postable right now."""
    try:
        from app.agents import facebook_client
        fb = facebook_client.enabled()
    except Exception:
        fb = False
    return {"facebook": fb, "youtube": False, "tiktok": False}


def _migrate(s: dict) -> dict:
    """Upgrade an old {time, platforms:[ids]} schedule to the per-platform shape."""
    if isinstance(s.get("platforms"), list) and s["platforms"] and isinstance(s["platforms"][0], dict):
        return s  # already new shape
    old_time = s.get("time") or "18:00"
    s["platforms"] = [
        {"name": "facebook", "time": old_time, "enabled": True},
        {"name": "youtube", "time": "19:00", "enabled": False},
        {"name": "tiktok", "time": "20:00", "enabled": False},
    ]
    s.pop("time", None)
    s.pop("last_run", None)
    return s


async def load_schedules() -> list[dict]:
    raw = await get_config(_SCHED_KEY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return [_migrate(s) for s in data] if isinstance(data, list) else []
    except Exception:
        return []


async def save_schedules(schedules: list[dict]) -> None:
    await set_config(_SCHED_KEY, json.dumps(schedules, ensure_ascii=False))


async def get_log() -> list[dict]:
    raw = await get_config(_LOG_KEY, "")
    try:
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


async def _append_log(entry: dict) -> None:
    log = await get_log()
    log.insert(0, entry)
    await set_config(_LOG_KEY, json.dumps(log[:_LOG_MAX], ensure_ascii=False))


async def _render_for(job: dict) -> dict:
    topic = (job.get("topic") or "").strip()
    ctype = job.get("content_type") or "mystery"
    tone = job.get("tone") or "evidence"
    from app.agents.vdo_agent import make_mystery_short, make_news_short
    if ctype == "news":
        if topic:
            return await make_news_short(topic, "")
        async with get_db() as db:  # no fixed topic -> latest fetched news item
            cur = await db.execute(
                "SELECT title, summary FROM news_items ORDER BY id DESC LIMIT 1")
            row = await cur.fetchone()
        if not row:
            return {"ok": False, "error": "ไม่มีข่าวให้ทำคลิป"}
        return await make_news_short(row["title"], row["summary"] or "")
    return await make_mystery_short(topic, tone=tone)  # default: สายมู (topic optional)


async def _post_platform(name: str, mp4: str, caption: str) -> tuple[bool, str]:
    if name == "facebook":
        from app.agents import facebook_client
        if facebook_client.enabled():
            return await facebook_client.post_video(mp4, caption)
        return False, "Facebook ยังไม่ได้ตั้ง token"
    if name == "youtube":
        return False, "YouTube ยังไม่เชื่อม"
    if name == "tiktok":
        return False, "TikTok ยังไม่เชื่อม"
    return False, f"ไม่รู้จักช่องทาง {name}"


async def _send_telegram(mp4: str, caption: str) -> None:
    """Always push the freshly made clip to Telegram so the user sees every one."""
    try:
        from app.core.config import settings
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        from telegram import Bot
        with open(mp4, "rb") as fh:
            await Bot(settings.telegram_bot_token).send_video(
                chat_id=settings.telegram_chat_id, video=fh, caption=(caption or "")[:1000])
    except Exception:
        pass


async def _ensure_clip(job: dict, today: str) -> tuple[str, str, str]:
    """Render the clip once per schedule per day; reuse across staggered platform times."""
    st = job.setdefault("_state", {})
    mp4 = st.get("mp4") or ""
    if st.get("gen_date") == today and mp4 and os.path.exists(mp4):
        return mp4, st.get("caption", ""), st.get("title", "")
    res = await _render_for(job)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("error", "render failed"))[:200])
    st.update(gen_date=today, mp4=res["mp4"],
              caption=res.get("caption") or res.get("title") or "",
              title=res.get("title") or "")
    await _send_telegram(st["mp4"], f"{st['title']}\n\n{st['caption']}".strip())
    return st["mp4"], st["caption"], st["title"]


async def run_job(job: dict, source: str = "manual") -> dict:
    """Render one clip now and post to every ENABLED platform (used by ▶ ทดสอบเลย)."""
    label = job.get("label") or "autopost"
    now = datetime.now(_BANGKOK).strftime("%Y-%m-%d %H:%M")
    today = now.split(" ")[0]
    try:
        mp4, caption, title = await _ensure_clip(job, today)
    except Exception as exc:
        await set_status("error", str(exc)[:120])
        entry = {"at": now, "label": label, "ok": False, "src": source, "msg": str(exc)[:200]}
        await _append_log(entry)
        return entry

    plats = [p for p in (job.get("platforms") or []) if p.get("enabled")] or [{"name": "facebook"}]
    results = []
    for p in plats:
        await set_status("posting", f"{PLATFORM_LABEL.get(p['name'], p['name'])}", title)
        try:
            ok, msg = await _post_platform(p["name"], mp4, caption)
        except Exception as exc:
            ok, msg = False, str(exc)[:160]
        results.append(f"{'✅' if ok else '❌'} {PLATFORM_LABEL.get(p['name'], p['name'])}: {msg}")
    await set_status("done", title=title)
    entry = {"at": now, "label": label, "ok": True, "src": source, "title": title,
             "video": mp4.split("/")[-1], "msg": " | ".join(results)}
    await _append_log(entry)
    return entry


async def run_due() -> None:
    """Fire each enabled platform whose Bangkok time matches now (deduped per day)."""
    schedules = await load_schedules()
    if not schedules:
        return
    now = datetime.now(_BANGKOK)
    hhmm, today, weekday = now.strftime("%H:%M"), now.strftime("%Y-%m-%d"), now.weekday()

    changed = False
    for job in schedules:
        if not job.get("enabled"):
            continue
        days = job.get("days")
        if isinstance(days, list) and days and weekday not in days:
            continue
        st = job.setdefault("_state", {})
        last_run = st.setdefault("last_run", {})
        due = [p for p in (job.get("platforms") or [])
               if p.get("enabled") and (p.get("time") or "") == hhmm
               and last_run.get(p["name"]) != today]
        if not due:
            continue
        try:
            mp4, caption, title = await _ensure_clip(job, today)
        except Exception as exc:
            await set_status("error", str(exc)[:120])
            await _append_log({"at": now.strftime("%Y-%m-%d %H:%M"), "label": job.get("label"),
                               "ok": False, "src": "schedule", "msg": str(exc)[:200]})
            for p in due:  # don't retry every minute on a hard failure
                last_run[p["name"]] = today
            changed = True
            continue
        for p in due:
            await set_status("posting", PLATFORM_LABEL.get(p["name"], p["name"]), title)
            try:
                ok, msg = await _post_platform(p["name"], mp4, caption)
            except Exception as exc:
                ok, msg = False, str(exc)[:160]
            last_run[p["name"]] = today
            await _append_log({"at": now.strftime("%Y-%m-%d %H:%M"), "label": job.get("label"),
                               "ok": ok, "src": "schedule", "title": title,
                               "video": mp4.split("/")[-1],
                               "msg": f"{PLATFORM_LABEL.get(p['name'], p['name'])}: {msg}"})
        await set_status("done", title=title)
        changed = True

    if changed:
        await save_schedules(schedules)
