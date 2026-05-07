from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from app.agents import news, summary
from app.core.config import settings
from app.core.database import get_db

_BANGKOK = ZoneInfo("Asia/Bangkok")


async def _send_scheduled_message(bot: Bot, text: str, action: str):
    await bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode=None)
    async with await get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            (action, f"chat_id={settings.telegram_chat_id}"),
        )
        await db.commit()


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=_BANGKOK)

    async def send_daily_news():
        message = await news.fetch_and_summarize()
        await _send_scheduled_message(bot, message, "scheduled_news_sent")

    async def send_daily_summary():
        message = await summary.generate_daily_summary()
        await _send_scheduled_message(bot, message, "scheduled_daily_summary_sent")

    async def send_weekly_review():
        message = await summary.generate_weekly_summary()
        today = datetime.now(_BANGKOK).date()
        start_date = today - timedelta(days=6)
        async with await get_db() as db:
            cursor = await db.execute(
                """
                SELECT mistake, lesson
                FROM lessons_learned
                WHERE date(created_at) BETWEEN ? AND ?
                ORDER BY created_at DESC
                """,
                (start_date.isoformat(), today.isoformat()),
            )
            rows = await cursor.fetchall()
        lesson_lines = ["", "🔁 สิ่งที่พลาดสัปดาห์นี้:"]
        if rows:
            for row in rows:
                lesson_lines.append(f"· {row['mistake']} -> {row['lesson']}")
        else:
            lesson_lines.append("· ไม่มี")
        message = message + "\n" + "\n".join(lesson_lines)
        await _send_scheduled_message(bot, message, "scheduled_weekly_review_sent")

    scheduler.add_job(
        send_daily_news,
        CronTrigger(hour=8, minute=0, timezone=_BANGKOK),
        id="daily_news",
        replace_existing=True,
    )
    scheduler.add_job(
        send_daily_summary,
        CronTrigger(hour=21, minute=0, timezone=_BANGKOK),
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.add_job(
        send_weekly_review,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=_BANGKOK),
        id="weekly_review",
        replace_existing=True,
    )
    return scheduler
