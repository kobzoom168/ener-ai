from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

BRIEFING_SYSTEM = build_system_prompt("""
งานของพี่: สรุป morning briefing ให้กบเริ่มวันได้เลย
กระชับ อ่านง่าย ใน 60 วินาที
""")

_BANGKOK = ZoneInfo("Asia/Bangkok")
_THAI_DAYS = ["จันทร์", "อังคาร", "พุธ", "พฤหัส", "ศุกร์", "เสาร์", "อาทิตย์"]


@log_agent_run("BriefingAgent", triggered_by="scheduler")
async def generate_morning_briefing() -> str:
    from app.agents.gmail_agent import fetch_unread_emails
    from app.core.memory import get_current_location

    try:
        emails = await fetch_unread_emails(max_results=5, _agent_triggered_by="scheduler")
    except Exception:
        emails = []

    # Heuristic: unread mail from real senders is important enough for morning scan.
    high_emails = [
        email for email in emails
        if str(email.get("subject", "")).strip() or str(email.get("from", "")).strip()
    ]

    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, title, priority
            FROM tasks
            WHERE status = 'open'
            ORDER BY
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                id
            LIMIT 5
            """
        )
        open_tasks = await cursor.fetchall()

        cursor = await db.execute(
            """
            SELECT title, source
            FROM news_items
            WHERE date(fetched_at, '+7 hours') = date('now', '+7 hours')
            ORDER BY id DESC
            LIMIT 3
            """
        )
        today_news = await cursor.fetchall()

    location = await get_current_location()
    now = datetime.now(_BANGKOK)
    day_th = _THAI_DAYS[now.weekday()]

    sections = [f"🌅 อรุณสวัสดิ์กบ วัน{day_th}ที่ {now.strftime('%d/%m/%Y')}", ""]

    if open_tasks:
        sections.append("📋 Task ค้างอยู่:")
        for task in open_tasks[:3]:
            emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(task["priority"], "🟡")
            sections.append(f"  {emoji} [{task['id']}] {task['title']}")

    if high_emails:
        sections.append("")
        sections.append(f"📧 Email สำคัญ {len(high_emails)} ฉบับรอกบ")
        for email in high_emails[:2]:
            sections.append(
                f"  · {str(email.get('subject', ''))[:40]} (จาก {str(email.get('from', ''))[:20]})"
            )

    if today_news:
        sections.append("")
        sections.append("📰 ข่าวเด่นวันนี้:")
        for news in today_news[:2]:
            sections.append(f"  · {str(news['title'])[:60]}")

    sections.append("")
    sections.append(f"📍 วันนี้: {location}")
    sections.append("")
    sections.append("มีอะไรให้พี่ช่วยไหมกบ?")

    raw_briefing = "\n".join(sections).strip()
    try:
        polished = await chat(
            raw_briefing,
            system=BRIEFING_SYSTEM,
            agent="briefing",
            preferred_model="haiku",
        )
        result = polished.strip() or raw_briefing
        try:
            await log_event(
                agent_name="BriefingAgent",
                event_type="task_done",
                summary="generate morning briefing",
                tags=["briefing", "morning"],
                result="success",
                learned=f"tasks={len(open_tasks)} emails={len(high_emails)} news={len(today_news)}",
            )
        except Exception:
            pass
        return result
    except Exception as exc:
        try:
            await log_event(
                agent_name="BriefingAgent",
                event_type="task_failed",
                summary="generate morning briefing fail",
                tags=["briefing", "morning", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        return raw_briefing
