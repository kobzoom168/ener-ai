import shutil
import psutil
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from app.agents import gmail_agent, log_keeper, monitor_agent, news, news_discovery, session_agent, summary
from app.core.agents import log_agent_run
from app.core.config import settings
from app.core.database import get_db
from app.core.event_log import prune_old_events

_BANGKOK = ZoneInfo("Asia/Bangkok")
_APP_DATA_DIR = Path("/app/data")
_APP_BACKUP_DIR = Path("/app/backups")


def _database_file() -> Path:
    configured = Path(settings.database_path)
    if configured.is_absolute():
        return configured
    if _APP_DATA_DIR.exists():
        return _APP_DATA_DIR / configured.name
    return configured


def _backup_dir() -> Path:
    if Path("/app").exists():
        return _APP_BACKUP_DIR
    return Path("backups")


def _metrics_data_dir() -> Path:
    if _APP_DATA_DIR.exists():
        return _APP_DATA_DIR
    return _database_file().parent


async def _log_audit(action: str, details: str):
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            (action, details),
        )
        await db.commit()


async def _send_scheduled_message(bot: Bot, text: str, action: str):
    await bot.send_message(chat_id=settings.telegram_chat_id, text=text, parse_mode=None)
    await _log_audit(action, f"chat_id={settings.telegram_chat_id}")


async def _send_alert(bot: Bot, text: str, action: str):
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=None,
    )
    await _log_audit(action, text)


async def _send_warning(bot: Bot, details: str, action: str):
    await _send_alert(bot, f"⚠️ Health Warning: {details}", action)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=_BANGKOK)

    async def send_daily_news():
        message = await news.fetch_and_summarize(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, message, "scheduled_news_sent")

    async def send_daily_email_summary():
        message = await gmail_agent.summarize_emails(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, message, "scheduled_email_summary_sent")

    async def send_daily_summary():
        message = await summary.generate_daily_summary(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, message, "scheduled_daily_summary_sent")
        session_message = await session_agent.generate_session_log(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, session_message, "scheduled_session_log_sent")

    async def send_weekly_review():
        message = await summary.generate_weekly_summary(_agent_triggered_by="scheduler")
        today = datetime.now(_BANGKOK).date()
        start_date = today - timedelta(days=6)
        async with get_db() as db:
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

    async def send_agent_health_report():
        message = await log_keeper.analyze_agent_health(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, message, "scheduled_agent_health_sent")

    async def send_news_discovery():
        message = await news_discovery.discover_new_sources(_agent_triggered_by="scheduler")
        await _send_scheduled_message(bot, message, "scheduled_news_discovery_sent")

    @log_agent_run("MonitorAgent", triggered_by="scheduler")
    async def monitor_check():
        await monitor_agent.check_and_alert(bot, _agent_triggered_by="scheduler")

    @log_agent_run("LogPruneAgent", triggered_by="scheduler")
    async def prune_agent_events():
        deleted = await prune_old_events(30)
        await _log_audit("agent_events_pruned", f"deleted={deleted}")

    @log_agent_run("BackupAgent", triggered_by="scheduler")
    async def run_daily_backup():
        db_file = _database_file()
        backup_dir = _backup_dir()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_name = f"ener-{datetime.now(_BANGKOK).strftime('%Y%m%d')}.db"
        backup_path = backup_dir / backup_name
        try:
            shutil.copy2(db_file, backup_path)
            cutoff = datetime.now(_BANGKOK).date() - timedelta(days=7)
            for old_file in backup_dir.glob("ener-*.db"):
                stamp = old_file.stem.replace("ener-", "")
                try:
                    file_date = datetime.strptime(stamp, "%Y%m%d").date()
                except ValueError:
                    continue
                if file_date < cutoff:
                    old_file.unlink(missing_ok=True)
            await _log_audit("daily_backup_completed", f"path={backup_path}")
        except Exception as exc:
            await _send_alert(bot, f"📌 backup ล้มเหลว: {exc}", "daily_backup_failed")

    @log_agent_run("HealthAgent", triggered_by="scheduler")
    async def run_health_check():
        issues = []
        try:
            async with get_db() as db:
                marker = f"health-check-{datetime.now(_BANGKOK).isoformat()}"
                cursor = await db.execute(
                    "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                    ("health_check_probe", marker),
                )
                probe_id = cursor.lastrowid
                cursor = await db.execute(
                    "SELECT details FROM audit_logs WHERE id = ?",
                    (probe_id,),
                )
                row = await cursor.fetchone()
                await db.commit()
                if not row or row["details"] != marker:
                    issues.append("SQLite อ่าน/เขียนไม่ได้")
        except Exception as exc:
            issues.append(f"SQLite ใช้งานไม่ได้: {exc}")

        if not settings.anthropic_api_key:
            issues.append("Anthropic API key ว่างอยู่")

        try:
            data_dir = _APP_DATA_DIR if _APP_DATA_DIR.exists() else _database_file().parent
            usage = shutil.disk_usage(data_dir)
            used_ratio = usage.used / usage.total
            if used_ratio > 0.8:
                issues.append(f"disk usage ของ {data_dir} เกิน 80% แล้ว")
        except Exception as exc:
            issues.append(f"เช็ก disk usage ไม่ได้: {exc}")

        if issues:
            await _send_warning(bot, " | ".join(issues), "health_warning_sent")

    @log_agent_run("MetricsAgent", triggered_by="scheduler")
    async def record_server_metrics():
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(_metrics_data_dir())
        network = psutil.net_io_counters()
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO server_metrics (
                    cpu_percent, ram_percent, ram_used_mb, ram_total_mb, disk_percent, net_in_bytes, net_out_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    float(psutil.cpu_percent()),
                    float(memory.percent),
                    int(memory.used / 1024 / 1024),
                    int(memory.total / 1024 / 1024),
                    float(disk.percent),
                    int(network.bytes_recv),
                    int(network.bytes_sent),
                ),
            )
            await db.execute(
                "DELETE FROM server_metrics WHERE recorded_at < datetime('now', '-24 hours')",
            )
            await db.commit()

    scheduler.add_job(
        send_daily_email_summary,
        CronTrigger(hour=8, minute=0, timezone=_BANGKOK),
        id="daily_email_summary",
        replace_existing=True,
    )
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
    scheduler.add_job(
        send_agent_health_report,
        CronTrigger(hour=22, minute=0, timezone=_BANGKOK),
        id="agent_health_report",
        replace_existing=True,
    )
    scheduler.add_job(
        monitor_check,
        CronTrigger(minute="*/30", timezone=_BANGKOK),
        id="monitor_check",
        replace_existing=True,
    )
    scheduler.add_job(
        run_daily_backup,
        CronTrigger(hour=2, minute=30, timezone=_BANGKOK),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.add_job(
        run_health_check,
        CronTrigger(minute="*/30", timezone=_BANGKOK),
        id="health_check",
        replace_existing=True,
    )
    scheduler.add_job(
        record_server_metrics,
        CronTrigger(minute="*/10", timezone=_BANGKOK),
        id="server_metrics",
        replace_existing=True,
    )
    scheduler.add_job(
        prune_agent_events,
        CronTrigger(day_of_week="sun", hour=3, minute=15, timezone=_BANGKOK),
        id="agent_events_prune",
        replace_existing=True,
    )
    scheduler.add_job(
        send_news_discovery,
        CronTrigger(day_of_week="mon", hour=10, minute=0, timezone=_BANGKOK),
        id="news_discovery",
        replace_existing=True,
    )
    return scheduler
