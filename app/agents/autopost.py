"""Auto-post core: user-defined schedules that render a short and publish it to one or
more Postiz channels (FB / YouTube / TikTok).

Schedules and the run log live in app_config as JSON so the UI can edit them at runtime.
The APScheduler minute-tick calls run_due(), which fires any schedule whose Bangkok time
matches now (deduped per day via last_run).
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.database import get_config, set_config, get_db

_BANGKOK = ZoneInfo("Asia/Bangkok")
_SCHED_KEY = "vdo_autopost_schedules"
_LOG_KEY = "vdo_autopost_log"
_LOG_MAX = 60


async def load_schedules() -> list[dict]:
    raw = await get_config(_SCHED_KEY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
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
    return await make_mystery_short(topic)  # default: สายมู (topic optional)


async def run_job(job: dict, source: str = "schedule") -> dict:
    """Render one clip and post it to every selected platform. Returns a log entry."""
    label = job.get("label") or job.get("id") or "autopost"
    now = datetime.now(_BANGKOK).strftime("%Y-%m-%d %H:%M")
    try:
        res = await _render_for(job)
    except Exception as exc:
        entry = {"at": now, "label": label, "ok": False, "src": source,
                 "msg": f"render error: {str(exc)[:200]}"}
        await _append_log(entry)
        return entry
    if not res.get("ok"):
        entry = {"at": now, "label": label, "ok": False, "src": source,
                 "msg": f"render fail: {str(res.get('error', ''))[:200]}"}
        await _append_log(entry)
        return entry

    mp4 = res["mp4"]
    caption = res.get("caption") or res.get("title") or ""
    title = res.get("title") or ""

    results = []
    from app.agents import facebook_client
    if facebook_client.enabled():  # direct Graph API (no Postiz)
        try:
            ok, msg = await facebook_client.post_video(mp4, caption)
        except Exception as exc:
            ok, msg = False, str(exc)[:160]
        results.append({"ok": ok, "msg": "FB: " + msg})
    else:  # fall back to Postiz
        from app.agents.postiz_client import post_video, FB_INTEGRATION_ID
        platforms = job.get("platforms") or [FB_INTEGRATION_ID]
        for pid in platforms:
            try:
                ok, msg = await post_video(mp4, caption, integration_id=pid, when="now")
            except Exception as exc:
                ok, msg = False, str(exc)[:160]
            results.append({"ok": ok, "msg": msg})
    any_ok = any(r["ok"] for r in results)
    entry = {"at": now, "label": label, "ok": any_ok, "src": source, "title": title,
             "video": mp4.split("/")[-1],
             "msg": "; ".join(f"{'✅' if r['ok'] else '❌'} {r['msg']}" for r in results)}
    await _append_log(entry)
    return entry


async def run_due() -> None:
    """Fire any schedule due at the current Bangkok minute (called once per minute)."""
    schedules = await load_schedules()
    if not schedules:
        return
    now = datetime.now(_BANGKOK)
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    weekday = now.weekday()  # 0=Mon … 6=Sun

    due = []
    for job in schedules:
        if not job.get("enabled"):
            continue
        if (job.get("time") or "") != hhmm:
            continue
        days = job.get("days")
        if isinstance(days, list) and days and weekday not in days:
            continue
        if job.get("last_run") == today:
            continue
        job["last_run"] = today
        due.append(job)

    if due:
        await save_schedules(schedules)  # persist last_run BEFORE the slow render
        for job in due:
            await run_job(job, source="schedule")
