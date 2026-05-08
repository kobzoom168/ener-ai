import asyncio
import base64
import json
import re
import secrets
import shutil
import subprocess
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
import psutil
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from telegram import Update

from app.bot.router import build_application
from app.core.ai import get_active_model, get_model_availability, get_model_label
from app.core.config import settings
from app.core.database import get_db, init_db
from app.scheduler import build_scheduler

telegram_app = build_application()
scheduler = None
_BANGKOK = ZoneInfo("Asia/Bangkok")
_APP_STARTED_AT = datetime.now(_BANGKOK)
_LOG_DIR = Path("/var/log/ener-ai")
_DOCKER_CONTAINER_NAME = "ener-ai-ener-ai-1"
_RANGE_OPTIONS = ["1h", "3h", "10h", "24h", "7d"]
_ADMIN_SIDEBAR_ITEMS = [
    ("Overview", "/admin", "overview", True),
    ("Conversations", "#timeline-panel", "conversations", False),
    ("Notes", "#workspace-panel", "notes", False),
    ("Tasks", "#workspace-panel", "tasks", False),
    ("Memory", "#brain-panel", "memory", False),
    ("Daily Digest", "#brain-panel", "daily-digest", False),
    ("Agents", "#usage-panel", "agents", False),
    ("AI Models", "#models-panel", "ai-models", False),
    ("Scheduler", "#scheduler-panel", "scheduler", False),
    ("Metrics", "/admin/metrics", "metrics", True),
    ("Logs", "/admin/logs", "logs", True),
    ("Settings", "#workspace-panel", "settings", False),
]
_MODEL_PANEL_ROWS = [
    {
        "key": "haiku",
        "name": "Claude Haiku",
        "cost": "Paid",
        "speed": "Smart / fast",
        "role": "General reasoning",
        "routing": "Chat mode",
    },
    {
        "key": "groq",
        "name": "Groq",
        "cost": "Free",
        "speed": "Very fast",
        "role": "Fast chat / low cost",
        "routing": "Default chat fallback",
    },
    {
        "key": "gemini",
        "name": "Gemini Flash",
        "cost": "Free",
        "speed": "Fast",
        "role": "Search-capable summarization",
        "routing": "/news",
    },
    {
        "key": "qwen3b",
        "name": "Qwen 3B",
        "cost": "Free",
        "speed": "Slow",
        "role": "Local fallback",
        "routing": "Local fallback",
    },
    {
        "key": "qwen7b",
        "name": "Qwen 7B",
        "cost": "Free",
        "speed": "Very slow",
        "role": "Heavier local fallback",
        "routing": "Manual switch",
    },
]
_SCHEDULER_JOB_META = [
    {
        "id": "daily_news",
        "name": "08:00 Daily News",
        "schedule": "Daily 08:00",
        "success_actions": ["scheduled_news_sent"],
        "failure_actions": [],
    },
    {
        "id": "daily_summary",
        "name": "21:00 Daily Summary",
        "schedule": "Daily 21:00",
        "success_actions": ["scheduled_daily_summary_sent", "daily_summary_generated"],
        "failure_actions": [],
    },
    {
        "id": "weekly_review",
        "name": "09:00 Weekly Review",
        "schedule": "Monday 09:00",
        "success_actions": ["scheduled_weekly_review_sent", "weekly_summary_generated"],
        "failure_actions": [],
    },
    {
        "id": "daily_backup",
        "name": "02:30 SQLite Backup",
        "schedule": "Daily 02:30",
        "success_actions": ["daily_backup_completed"],
        "failure_actions": ["daily_backup_failed"],
    },
    {
        "id": "server_metrics",
        "name": "10m Server Metrics",
        "schedule": "Every 10 min",
        "success_actions": [],
        "failure_actions": [],
    },
    {
        "id": "health_check",
        "name": "30m Health Check",
        "schedule": "Every 30 min",
        "success_actions": ["health_check_probe"],
        "failure_actions": ["health_warning_sent"],
    },
]


def _data_dir() -> Path:
    app_data = Path("/app/data")
    if app_data.exists():
        return app_data
    configured = Path(settings.database_path)
    return configured.parent if configured.parent != Path("") else Path(".")


def _truncate_text(text: str, limit: int = 100) -> str:
    clean = str(text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _sanitize_admin_text(text: str) -> str:
    sanitized = str(text or "")
    sanitized = re.sub(r"chat_id=(\d{4})\d+(\d{4})", r"chat_id=\1****\2", sanitized)
    sanitized = re.sub(r"chat_id=\d+", "chat_id=masked", sanitized)
    sanitized = re.sub(r"(\b\d{4})\d{4,}(\d{4}\b)", r"\1****\2", sanitized)
    sanitized = re.sub(r"(bot)\d{6,}:[A-Za-z0-9_-]+", r"\1***:masked", sanitized, flags=re.IGNORECASE)
    return sanitized


def _format_short_time(raw: str) -> str:
    return str(raw)[11:16] if raw else "--:--"


def _format_full_time(raw: str) -> str:
    return str(raw)[11:19] if raw else "--:--:--"


def _normalize_range_key(range_key: str) -> str:
    return range_key if range_key in _RANGE_OPTIONS else "10h"


def _range_delta(range_key: str) -> timedelta:
    normalized = _normalize_range_key(range_key)
    if normalized == "1h":
        return timedelta(hours=1)
    if normalized == "3h":
        return timedelta(hours=3)
    if normalized == "24h":
        return timedelta(hours=24)
    if normalized == "7d":
        return timedelta(days=7)
    return timedelta(hours=10)


def _stats_for(values: list[float]) -> dict[str, float]:
    if not values:
        return {"last": 0.0, "min": 0.0, "max": 0.0, "mean": 0.0}
    return {
        "last": float(values[-1]),
        "min": float(min(values)),
        "max": float(max(values)),
        "mean": float(sum(values) / len(values)),
    }


def _classify_log_level(text: str) -> str:
    lowered = text.lower()
    if any(keyword in lowered for keyword in ["error", "traceback", "exception", "critical", "failed"]):
        return "ERROR"
    if any(keyword in lowered for keyword in ["warn", "warning"]):
        return "WARNING"
    return "INFO"


def _extract_log_time(text: str) -> str:
    match = re.search(r"(\d{2}:\d{2}:\d{2})", text)
    if match:
        return match.group(1)
    return datetime.now(_BANGKOK).strftime("%H:%M:%S")


def _build_conversation_pairs(rows) -> list[dict]:
    ordered_messages = list(reversed(rows))
    recent_conversations = []
    current_pair: dict[str, str] | None = None
    for row in ordered_messages:
        role = row["role"]
        if role == "user":
            if current_pair and (current_pair.get("user") or current_pair.get("assistant")):
                recent_conversations.append(current_pair)
            current_pair = {
                "time": _format_short_time(row["local_created_at"]),
                "model": row["model"] or "haiku",
                "model_label": get_model_label(row["model"] or "haiku"),
                "user": _truncate_text(row["content"]),
                "assistant": "",
            }
        elif role == "assistant":
            if current_pair is None:
                current_pair = {
                    "time": _format_short_time(row["local_created_at"]),
                    "model": row["model"] or "haiku",
                    "model_label": get_model_label(row["model"] or "haiku"),
                    "user": "",
                    "assistant": _truncate_text(row["content"]),
                }
            elif not current_pair.get("assistant"):
                current_pair["assistant"] = _truncate_text(row["content"])
                if row["model"]:
                    current_pair["model"] = row["model"]
                    current_pair["model_label"] = get_model_label(row["model"])
            else:
                recent_conversations.append(current_pair)
                current_pair = {
                    "time": _format_short_time(row["local_created_at"]),
                    "model": row["model"] or "haiku",
                    "model_label": get_model_label(row["model"] or "haiku"),
                    "user": "",
                    "assistant": _truncate_text(row["content"]),
                }
    if current_pair and (current_pair.get("user") or current_pair.get("assistant")):
        recent_conversations.append(current_pair)
    return list(reversed(recent_conversations[-10:]))


def _realtime_metrics() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(_data_dir())
    network = psutil.net_io_counters()
    return {
        "cpu_percent": float(psutil.cpu_percent()),
        "ram_percent": float(memory.percent),
        "ram_used_mb": int(memory.used / 1024 / 1024),
        "ram_total_mb": int(memory.total / 1024 / 1024),
        "disk_percent": float(disk.percent),
        "network_in_bytes": int(network.bytes_recv),
        "network_out_bytes": int(network.bytes_sent),
    }


async def _require_admin(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    if not (
        secrets.compare_digest(username, "admin")
        and secrets.compare_digest(password, settings.admin_password)
    ):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


async def _check_webhook_status() -> str:
    try:
        info = await telegram_app.bot.get_webhook_info()
        return "OK" if info.url else "FAIL"
    except Exception:
        return "FAIL"


async def _check_ollama_status() -> str:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            return "OK" if response.status_code == 200 else "FAIL"
    except Exception:
        return "FAIL"


async def _load_admin_status() -> dict:
    active_model = await get_active_model() or "haiku"
    availability = get_model_availability()
    today = datetime.now(_BANGKOK).date().isoformat()
    month_key = datetime.now(_BANGKOK).strftime("%Y-%m")
    webhook_status, ollama_status = await asyncio.gather(
        _check_webhook_status(),
        _check_ollama_status(),
    )

    async with get_db() as db:
        today_cost_cursor = await db.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total, COUNT(*) AS calls
            FROM ai_runs
            WHERE date(created_at, '+7 hours') = ?
            """,
            (today,),
        )
        today_cost = await today_cost_cursor.fetchone()
        month_cost_cursor = await db.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE strftime('%Y-%m', datetime(created_at, '+7 hours')) = ?
            """,
            (month_key,),
        )
        month_cost = await month_cost_cursor.fetchone()
        conversation_cursor = await db.execute(
            """
            SELECT
              datetime(m.created_at, '+7 hours') AS local_created_at,
              m.role,
              m.content,
              r.model
            FROM messages m
            LEFT JOIN ai_runs r ON (
              r.agent = 'chat' AND
              ABS(strftime('%s', r.created_at) - strftime('%s', m.created_at)) < 5
            )
            WHERE m.chat_id = ?
            ORDER BY m.created_at DESC
            LIMIT 20
            """,
            (settings.telegram_chat_id,),
        )
        conversation_rows = await conversation_cursor.fetchall()
        backup_cursor = await db.execute(
            """
            SELECT datetime(created_at, '+7 hours') AS local_created_at
            FROM audit_logs
            WHERE action = 'daily_backup_completed'
            ORDER BY id DESC
            LIMIT 1
            """,
        )
        backup_row = await backup_cursor.fetchone()
        sqlite_ok = True
        try:
            check_cursor = await db.execute("SELECT 1 AS ok")
            check_row = await check_cursor.fetchone()
            sqlite_ok = bool(check_row and check_row["ok"] == 1)
        except Exception:
            sqlite_ok = False

    disk_percent = round(shutil.disk_usage(_data_dir()).used / shutil.disk_usage(_data_dir()).total * 100)
    using_local_model = active_model in {"qwen3b", "qwen7b"}
    api_ok = availability.get(active_model, False) if not using_local_model else ollama_status == "OK"
    disk_ok = disk_percent < 80
    health_ok_count = sum([1 if sqlite_ok else 0, 1 if api_ok else 0, 1 if disk_ok else 0])
    uptime_delta = datetime.now(_BANGKOK) - _APP_STARTED_AT
    uptime_minutes = int(uptime_delta.total_seconds() // 60)
    uptime_text = f"{uptime_minutes // 60}h {uptime_minutes % 60}m"
    local_model_label = "Ollama" if using_local_model else "Local model"
    local_model_status = ollama_status if using_local_model else ("Ready" if ollama_status == "OK" else "Offline")

    return {
        "active_model": active_model,
        "active_model_label": get_model_label(active_model),
        "model_availability": availability,
        "today_cost_thb": float(today_cost["total"]),
        "today_calls": int(today_cost["calls"]),
        "month_cost_thb": float(month_cost["total"]),
        "health": {
            "summary": f"{health_ok_count}/3 OK",
            "sqlite": "OK" if sqlite_ok else "FAIL",
            "api": "OK" if api_ok else "FAIL",
            "disk": "OK" if disk_ok else "FAIL",
            "webhook": webhook_status,
            "ollama": local_model_status,
            "local_model_label": local_model_label,
            "local_model": local_model_status,
            "uptime": uptime_text,
        },
        "last_backup_time": _format_full_time(backup_row["local_created_at"]) if backup_row else "ยังไม่มี",
        "recent_conversations": _build_conversation_pairs(conversation_rows),
    }


async def _load_admin_metrics() -> dict:
    return await _load_metrics_payload("10h")


async def _load_metrics_payload(range_key: str) -> dict:
    now = datetime.now(_BANGKOK)
    normalized_range = _normalize_range_key(range_key)
    today = now.date().isoformat()
    seven_days = [(now.date() - timedelta(days=offset)) for offset in range(6, -1, -1)]
    history_cutoff = (now - _range_delta(normalized_range)).strftime("%Y-%m-%d %H:%M:%S")
    realtime = _realtime_metrics()

    async with get_db() as db:
        history_cursor = await db.execute(
            """
            SELECT
                datetime(recorded_at, '+7 hours') AS local_recorded_at,
                cpu_percent,
                ram_percent,
                disk_percent
            FROM server_metrics
            WHERE datetime(recorded_at, '+7 hours') >= ?
            ORDER BY recorded_at
            """,
            (history_cutoff,),
        )
        history_rows = await history_cursor.fetchall()

        today_calls_cursor = await db.execute(
            """
            SELECT model, COUNT(*) AS calls, COALESCE(SUM(estimated_cost_thb), 0) AS cost
            FROM ai_runs
            WHERE datetime(created_at, '+7 hours') >= ?
            GROUP BY model
            ORDER BY calls DESC, model
            """,
            (history_cutoff,),
        )
        today_calls_rows = await today_calls_cursor.fetchall()

        avg_response_cursor = await db.execute(
            """
            SELECT COALESCE(AVG(response_time_ms), 0) AS avg_response_ms
            FROM ai_runs
            WHERE datetime(created_at, '+7 hours') >= ? AND success = 1
            """,
            (history_cutoff,),
        )
        avg_response_row = await avg_response_cursor.fetchone()

        hourly_calls_cursor = await db.execute(
            """
            SELECT
                strftime('%Y-%m-%d %H:00', datetime(created_at, '+7 hours')) AS hour_bucket,
                model,
                COUNT(*) AS calls
            FROM ai_runs
            WHERE datetime(created_at, '+7 hours') >= ?
            GROUP BY hour_bucket, model
            ORDER BY hour_bucket
            """,
            (history_cutoff,),
        )
        hourly_calls_rows = await hourly_calls_cursor.fetchall()

        cost_7d_cursor = await db.execute(
            """
            SELECT date(created_at, '+7 hours') AS local_day, COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE date(created_at, '+7 hours') >= ?
            GROUP BY local_day
            ORDER BY local_day
            """,
            ((now.date() - timedelta(days=6)).isoformat(),),
        )
        cost_7d_rows = await cost_7d_cursor.fetchall()

        network_cursor = await db.execute(
            """
            SELECT net_in_bytes, net_out_bytes
            FROM server_metrics
            WHERE date(recorded_at, '+7 hours') = ?
            ORDER BY recorded_at
            """,
            (today,),
        )
        network_rows = await network_cursor.fetchall()

    history_labels = [_format_short_time(row["local_recorded_at"]) for row in history_rows]
    history_cpu = [float(row["cpu_percent"] or 0) for row in history_rows]
    history_ram = [float(row["ram_percent"] or 0) for row in history_rows]
    history_disk = [float(row["disk_percent"] or 0) for row in history_rows]
    if not history_labels:
        fallback_label = now.strftime("%H:%M")
        history_labels = [fallback_label]
        history_cpu = [realtime["cpu_percent"]]
        history_ram = [realtime["ram_percent"]]
        history_disk = [realtime["disk_percent"]]

    usage_labels = [get_model_label(row["model"]) for row in today_calls_rows]
    usage_counts = [int(row["calls"]) for row in today_calls_rows]
    total_calls = sum(usage_counts)
    total_cost = sum(float(row["cost"]) for row in today_calls_rows)
    top_model_label = usage_labels[0] if usage_labels else "-"
    ai_calls_hourly: dict[str, int] = {}
    ai_calls_by_model: dict[str, dict[str, int]] = {}
    for row in hourly_calls_rows:
        hour_label = str(row["hour_bucket"])[11:16]
        ai_calls_hourly[hour_label] = ai_calls_hourly.get(hour_label, 0) + int(row["calls"])
        model_key = row["model"]
        if model_key not in ai_calls_by_model:
            ai_calls_by_model[model_key] = {}
        ai_calls_by_model[model_key][hour_label] = int(row["calls"])

    cost_by_day = {row["local_day"]: float(row["total"]) for row in cost_7d_rows}
    cost_7d_labels = [day.strftime("%d/%m") for day in seven_days]
    cost_7d_values = [cost_by_day.get(day.isoformat(), 0.0) for day in seven_days]

    network_in_mb = 0.0
    network_out_mb = 0.0
    if len(network_rows) >= 2:
        network_in_mb = max(0.0, (network_rows[-1]["net_in_bytes"] - network_rows[0]["net_in_bytes"]) / 1024 / 1024)
        network_out_mb = max(0.0, (network_rows[-1]["net_out_bytes"] - network_rows[0]["net_out_bytes"]) / 1024 / 1024)

    return {
        "range": normalized_range,
        "realtime": {
            "cpu_percent": realtime["cpu_percent"],
            "ram_percent": realtime["ram_percent"],
            "ram_used_mb": realtime["ram_used_mb"],
            "ram_total_mb": realtime["ram_total_mb"],
            "disk_percent": realtime["disk_percent"],
            "network_in_mb": round(network_in_mb, 2),
            "network_out_mb": round(network_out_mb, 2),
        },
        "history": {
            "labels": history_labels,
            "cpu": history_cpu,
            "ram": history_ram,
            "disk": history_disk,
        },
        "labels": history_labels,
        "cpu": history_cpu,
        "ram": history_ram,
        "disk": history_disk,
        "ai_calls_hourly": ai_calls_hourly,
        "ai_calls_by_model": ai_calls_by_model,
        "cost_daily": {label: value for label, value in zip(cost_7d_labels, cost_7d_values)},
        "stats": {
            "cpu": _stats_for(history_cpu),
            "ram": _stats_for(history_ram),
            "disk": _stats_for(history_disk),
            "calls": _stats_for([float(value) for value in ai_calls_hourly.values()]),
            "cost": _stats_for(cost_7d_values),
        },
        "ai_usage": {
            "labels": usage_labels,
            "counts": usage_counts,
            "total_calls": total_calls,
            "total_cost_thb": round(total_cost, 2),
            "avg_response_ms": round(float(avg_response_row["avg_response_ms"]), 1),
            "top_model_label": top_model_label,
            "cost_7d_labels": cost_7d_labels,
            "cost_7d_values": cost_7d_values,
        },
    }


def _read_file_logs(lines: int) -> list[dict]:
    if not _LOG_DIR.exists():
        return []
    files = sorted(
        [path for path in _LOG_DIR.iterdir() if path.is_file()],
        key=lambda path: path.stat().st_mtime,
    )
    collected = []
    for path in files[-3:]:
        try:
            collected.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except Exception:
            continue
    entries = []
    for line in collected[-lines:]:
        entries.append(
            {
                "time": _extract_log_time(line),
                "level": _classify_log_level(line),
                "message": _sanitize_admin_text(line.strip()),
            }
        )
    return entries


async def _read_docker_logs(lines: int) -> list[dict]:
    def _run_logs():
        return subprocess.run(
            ["docker", "logs", _DOCKER_CONTAINER_NAME, "--tail", str(lines)],
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        result = await asyncio.to_thread(_run_logs)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines()[-lines:]:
        entries.append(
            {
                "time": _extract_log_time(line),
                "level": _classify_log_level(line),
                "message": _sanitize_admin_text(line.strip()),
            }
        )
    return entries


async def _read_audit_logs(lines: int) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT datetime(created_at, '+7 hours') AS local_created_at, action, details
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (lines,),
        )
        rows = await cursor.fetchall()
    entries = []
    for row in reversed(rows):
        text = _sanitize_admin_text(f"{row['action']} {row['details'] or ''}".strip())
        entries.append(
            {
                "time": _format_full_time(row["local_created_at"]),
                "level": _classify_log_level(text),
                "message": text,
            }
        )
    return entries


async def _load_log_entries(filter_value: str, lines: int) -> list[dict]:
    safe_lines = max(50, min(lines, 500))
    entries = _read_file_logs(safe_lines)
    if not entries:
        entries = await _read_docker_logs(safe_lines)
    if not entries:
        entries = await _read_audit_logs(safe_lines)
    if filter_value != "ALL":
        entries = [entry for entry in entries if entry["level"] == filter_value]
    return entries[-safe_lines:]


def _status_tone(status: str) -> str:
    lowered = str(status).strip().lower()
    if lowered in {"ok", "healthy"}:
        return "ok"
    if lowered in {"warning", "warn", "stale"}:
        return "warning"
    if lowered in {"error", "danger", "fail"}:
        return "danger"
    if lowered in {"empty"}:
        return "empty"
    return "unknown"


def _dashboard_timestamp(raw: str | None) -> str:
    if not raw:
        return "No data"
    text = str(raw)
    if len(text) >= 16:
        return f"{text[:10]} {text[11:16]}"
    return text


def _scheduler_next_run(job_id: str) -> str:
    if scheduler is None:
        return "Unknown"
    try:
        job = scheduler.get_job(job_id)
        if job is None or job.next_run_time is None:
            return "Unknown"
        return job.next_run_time.astimezone(_BANGKOK).strftime("%d/%m %H:%M")
    except Exception:
        return "Unknown"


def _latest_time(rows: list[dict], actions: list[str]) -> str | None:
    latest = None
    for row in rows:
        if row["action"] not in actions:
            continue
        current = row["local_created_at"]
        if latest is None or current > latest:
            latest = current
    return latest


def _summarize_audit_event(action: str, details: str) -> dict | None:
    mapping = {
        "scheduled_news_sent": ("cron", "Daily News Sent", "ok"),
        "scheduled_daily_summary_sent": ("cron", "Daily Summary Sent", "ok"),
        "scheduled_weekly_review_sent": ("cron", "Weekly Review Sent", "ok"),
        "daily_backup_completed": ("cron", "SQLite Backup Completed", "ok"),
        "daily_backup_failed": ("error", "SQLite Backup Failed", "danger"),
        "health_warning_sent": ("error", "Health Warning", "warning"),
        "note_saved": ("note", "Note Created", "ok"),
        "task_created": ("task", "Task Added", "ok"),
        "task_done": ("task", "Task Completed", "ok"),
        "memory_searched": ("memory", "Memory Search", "ok"),
        "idea_parked": ("memory", "Idea Parked", "ok"),
        "long_term_memory_saved": ("memory", "Long-term Memory Saved", "ok"),
        "long_term_memory_deleted": ("memory", "Long-term Memory Deleted", "warning"),
        "long_term_memory_auto_saved": ("memory", "Memory Auto-captured", "ok"),
        "lesson_recorded": ("memory", "Lesson Recorded", "ok"),
        "brainstorm_completed": ("chat", "Brainstorm Completed", "ok"),
        "news_fetch_completed": ("cron", "News Fetch Completed", "ok"),
        "daily_summary_generated": ("cron", "Daily Digest Generated", "ok"),
        "weekly_summary_generated": ("cron", "Weekly Digest Generated", "ok"),
        "voice_mode_updated": ("memory", "Voice Setting Changed", "ok"),
        "admin_model_switched": ("chat", "Active Model Switched", "ok"),
        "clarification_requested": ("chat", "Clarification Requested", "warning"),
        "clarification_resolved": ("chat", "Clarification Resolved", "ok"),
        "clarification_skipped": ("chat", "Clarification Skipped", "warning"),
        "clarification_cleared": ("chat", "Clarification Cleared", "ok"),
        "chat_tasks_created": ("task", "Tasks Created From Chat", "ok"),
    }
    if action not in mapping:
        return None
    timeline_type, title, tone = mapping[action]
    snippet = _truncate_text(_sanitize_admin_text(details or title), 90)
    return {
        "type": timeline_type,
        "title": title,
        "tone": tone,
        "message": snippet if snippet else title,
    }


def _extract_command_label(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "chat mode"
    if not stripped.startswith("/"):
        return "chat mode"
    command = stripped.split()[0].lower()
    alias_map = {
        "/mistake": "/learn",
        "/brainstorm": "/think",
        "/start": "/help",
    }
    return alias_map.get(command, command)


async def _load_admin_overview() -> dict:
    now = datetime.now(_BANGKOK)
    refreshed_at = now.strftime("%d/%m/%Y %H:%M")
    today = now.date().isoformat()
    status: dict
    metrics: dict

    try:
        status = await _load_admin_status()
    except Exception:
        status = {
            "active_model": "unknown",
            "active_model_label": "Unknown",
            "model_availability": {},
            "today_cost_thb": 0.0,
            "today_calls": 0,
            "month_cost_thb": 0.0,
            "health": {
                "summary": "Unknown",
                "sqlite": "Unknown",
                "api": "Unknown",
                "disk": "Unknown",
                "webhook": "Unknown",
                "ollama": "Unknown",
                "uptime": "Unknown",
            },
            "last_backup_time": "Unknown",
            "recent_conversations": [],
        }

    try:
        metrics = await _load_metrics_payload("10h")
    except Exception:
        metrics = {
            "realtime": {
                "cpu_percent": 0.0,
                "ram_percent": 0.0,
                "ram_used_mb": 0,
                "ram_total_mb": 0,
                "disk_percent": 0.0,
                "network_in_mb": 0.0,
                "network_out_mb": 0.0,
            },
            "ai_usage": {
                "total_calls": 0,
                "total_cost_thb": 0.0,
                "avg_response_ms": 0.0,
                "top_model_label": "Unknown",
            },
        }

    overview = {
        "topbar": {
            "title": "Ener-AI Admin",
            "environment": "Production",
            "server": "Hetzner CPX22",
            "timezone": "Bangkok Time",
            "active_model": status.get("active_model_label", "Unknown"),
            "voice": "Unknown",
            "health": status.get("health", {}).get("summary", "Unknown"),
            "health_tone": _status_tone(status.get("health", {}).get("sqlite", "unknown")),
            "cost_today": f"฿{float(status.get('today_cost_thb', 0.0)):.2f}",
            "last_refresh": refreshed_at,
        },
        "kpis": [],
        "brain_status": [],
        "timeline": [],
        "model_panel": {},
        "agent_usage": [],
        "scheduler_health": [],
        "server_health": {},
        "recent_logs": [],
    }

    async with get_db() as db:
        try:
            voice_row = await (
                await db.execute(
                    "SELECT value FROM memories WHERE key = ? LIMIT 1",
                    (f"voice_mode_{settings.telegram_chat_id}",),
                )
            ).fetchone()
            voice_enabled = bool(voice_row and voice_row["value"] == "on")
            overview["topbar"]["voice"] = "Voice ON" if voice_enabled else "Voice OFF"
        except Exception:
            overview["topbar"]["voice"] = "Voice Unknown"

        try:
            message_count_row = await (
                await db.execute(
                    "SELECT COUNT(*) AS total FROM messages WHERE date(created_at, '+7 hours') = ?",
                    (today,),
                )
            ).fetchone()
            task_count_row = await (
                await db.execute(
                    "SELECT COUNT(*) AS total FROM tasks WHERE COALESCE(status, 'open') NOT IN ('done', 'closed')"
                )
            ).fetchone()
            note_count_row = await (
                await db.execute(
                    "SELECT COUNT(*) AS total FROM notes WHERE date(created_at, '+7 hours') = ?",
                    (today,),
                )
            ).fetchone()
            memory_count_row = await (
                await db.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM long_term_memories) + (SELECT COUNT(*) FROM beliefs) AS total
                    """
                )
            ).fetchone()
            cron_health_tone = "unknown"
            server_health_tone = "unknown"
            open_tasks = int(task_count_row["total"]) if task_count_row else 0
            memories_active = int(memory_count_row["total"]) if memory_count_row else 0
            overview["kpis"] = [
                {
                    "label": "AI Calls Today",
                    "value": str(int(status.get("today_calls", 0))),
                    "meta": "Rows in ai_runs (Bangkok today)",
                    "tone": "ok" if int(status.get("today_calls", 0)) > 0 else "unknown",
                },
                {
                    "label": "Cost Today",
                    "value": f"฿{float(status.get('today_cost_thb', 0.0)):.2f}",
                    "meta": f"Month ฿{float(status.get('month_cost_thb', 0.0)):.2f}",
                    "tone": "ok",
                },
                {
                    "label": "Messages Today",
                    "value": str(int(message_count_row["total"]) if message_count_row else 0),
                    "meta": "messages today",
                    "tone": "ok" if int(message_count_row["total"]) > 0 else "empty",
                },
                {
                    "label": "Open Tasks",
                    "value": str(open_tasks),
                    "meta": "status not done/closed",
                    "tone": "warning" if open_tasks > 0 else "ok",
                },
                {
                    "label": "Notes Today",
                    "value": str(int(note_count_row["total"]) if note_count_row else 0),
                    "meta": "notes created today",
                    "tone": "ok" if int(note_count_row["total"]) > 0 else "empty",
                },
                {
                    "label": "Memories Active",
                    "value": str(memories_active),
                    "meta": "long-term memories + beliefs",
                    "tone": "ok" if memories_active > 0 else "empty",
                },
                {
                    "label": "Cron Health",
                    "value": "Unknown",
                    "meta": "scheduler status inferred",
                    "tone": cron_health_tone,
                },
                {
                    "label": "Server Health",
                    "value": "Unknown",
                    "meta": "latest server metrics",
                    "tone": server_health_tone,
                },
            ]
        except Exception:
            overview["kpis"] = [
                {"label": "AI Calls Today", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Cost Today", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Messages Today", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Open Tasks", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Notes Today", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Memories Active", "value": "No data", "meta": "Query failed", "tone": "unknown"},
                {"label": "Cron Health", "value": "Unknown", "meta": "Query failed", "tone": "unknown"},
                {"label": "Server Health", "value": "Unknown", "meta": "Query failed", "tone": "unknown"},
            ]

        try:
            brain_queries = {
                "ltm": await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS total, MAX(datetime(created_at, '+7 hours')) AS latest
                        FROM long_term_memories
                        """
                    )
                ).fetchone(),
                "beliefs": await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS total, MAX(datetime(created_at, '+7 hours')) AS latest
                        FROM beliefs
                        """
                    )
                ).fetchone(),
                "digests": await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS total, MAX(period_start) AS latest
                        FROM digests
                        WHERE digest_type = 'daily'
                        """
                    )
                ).fetchone(),
                "messages": await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS total, MAX(datetime(created_at, '+7 hours')) AS latest
                        FROM messages
                        """
                    )
                ).fetchone(),
                "lessons": await (
                    await db.execute(
                        """
                        SELECT COUNT(*) AS total, MAX(datetime(created_at, '+7 hours')) AS latest
                        FROM lessons_learned
                        """
                    )
                ).fetchone(),
            }
            latest_digest = brain_queries["digests"]["latest"] if brain_queries["digests"] else None
            digest_tone = "empty"
            if latest_digest:
                latest_digest_date = datetime.fromisoformat(str(latest_digest))
                digest_tone = "ok" if (now.date() - latest_digest_date.date()).days <= 1 else "warning"
            overview["brain_status"] = [
                {
                    "name": "Long-term Memory",
                    "count": f"{int(brain_queries['ltm']['total'])} items",
                    "status": "OK" if int(brain_queries["ltm"]["total"]) > 0 else "Empty",
                    "tone": "ok" if int(brain_queries["ltm"]["total"]) > 0 else "empty",
                    "note": "Used in every AI context",
                    "latest": _dashboard_timestamp(brain_queries["ltm"]["latest"]),
                    "href": "#workspace-panel",
                    "action": "View section",
                },
                {
                    "name": "Beliefs",
                    "count": f"{int(brain_queries['beliefs']['total'])} items",
                    "status": "OK" if int(brain_queries["beliefs"]["total"]) > 0 else "Empty",
                    "tone": "ok" if int(brain_queries["beliefs"]["total"]) > 0 else "empty",
                    "note": "Preferences and identity layer",
                    "latest": _dashboard_timestamp(brain_queries["beliefs"]["latest"]),
                    "href": "#workspace-panel",
                    "action": "View section",
                },
                {
                    "name": "Daily Summary",
                    "count": _dashboard_timestamp(latest_digest),
                    "status": "OK" if digest_tone == "ok" else ("Warning" if digest_tone == "warning" else "Empty"),
                    "tone": digest_tone,
                    "note": "7-day digest source",
                    "latest": _dashboard_timestamp(latest_digest),
                    "href": "#brain-panel",
                    "action": "Review digest",
                },
                {
                    "name": "Recent Messages",
                    "count": f"{min(int(brain_queries['messages']['total']), 20)} visible",
                    "status": "OK" if int(brain_queries["messages"]["total"]) > 0 else "Empty",
                    "tone": "ok" if int(brain_queries["messages"]["total"]) > 0 else "empty",
                    "note": "Immediate chat context",
                    "latest": _dashboard_timestamp(brain_queries["messages"]["latest"]),
                    "href": "#timeline-panel",
                    "action": "See timeline",
                },
                {
                    "name": "Lessons Learned",
                    "count": f"{int(brain_queries['lessons']['total'])} items",
                    "status": "OK" if int(brain_queries["lessons"]["total"]) > 0 else "Empty",
                    "tone": "ok" if int(brain_queries["lessons"]["total"]) > 0 else "empty",
                    "note": "Mistake memory",
                    "latest": _dashboard_timestamp(brain_queries["lessons"]["latest"]),
                    "href": "#workspace-panel",
                    "action": "View section",
                },
            ]
        except Exception:
            overview["brain_status"] = [
                {
                    "name": "Brain Status",
                    "count": "Unknown",
                    "status": "Unknown",
                    "tone": "unknown",
                    "note": "Panel query failed",
                    "latest": "No data",
                    "href": "#workspace-panel",
                    "action": "View section",
                }
            ]

        try:
            timeline_events = []
            audit_rows = await (
                await db.execute(
                    """
                    SELECT datetime(created_at, '+7 hours') AS local_created_at, action, details
                    FROM audit_logs
                    WHERE date(created_at, '+7 hours') = ?
                    ORDER BY id DESC
                    LIMIT 60
                    """,
                    (today,),
                )
            ).fetchall()
            for row in audit_rows:
                summarized = _summarize_audit_event(row["action"], row["details"] or "")
                if not summarized:
                    continue
                timeline_events.append(
                    {
                        "sort_key": row["local_created_at"],
                        "time": _format_short_time(row["local_created_at"]),
                        "type": summarized["type"],
                        "title": summarized["title"],
                        "message": summarized["message"],
                        "tone": summarized["tone"],
                        "meta": row["action"],
                    }
                )

            ai_run_rows = await (
                await db.execute(
                    """
                    SELECT datetime(created_at, '+7 hours') AS local_created_at, model, estimated_cost_thb, success
                    FROM ai_runs
                    WHERE date(created_at, '+7 hours') = ?
                    ORDER BY id DESC
                    LIMIT 30
                    """,
                    (today,),
                )
            ).fetchall()
            for row in ai_run_rows:
                timeline_events.append(
                    {
                        "sort_key": row["local_created_at"],
                        "time": _format_short_time(row["local_created_at"]),
                        "type": "chat" if row["success"] else "error",
                        "title": f"AI Run: {get_model_label(row['model'])}",
                        "message": f"{'Success' if row['success'] else 'Failed'} · ฿{float(row['estimated_cost_thb'] or 0):.2f}",
                        "tone": "ok" if row["success"] else "danger",
                        "meta": row["model"],
                    }
                )

            message_rows = await (
                await db.execute(
                    """
                    SELECT datetime(created_at, '+7 hours') AS local_created_at, content
                    FROM messages
                    WHERE date(created_at, '+7 hours') = ? AND role = 'user'
                    ORDER BY id DESC
                    LIMIT 20
                    """,
                    (today,),
                )
            ).fetchall()
            for row in message_rows:
                timeline_events.append(
                    {
                        "sort_key": row["local_created_at"],
                        "time": _format_short_time(row["local_created_at"]),
                        "type": "chat",
                        "title": "Chat Message",
                        "message": _truncate_text(_sanitize_admin_text(row["content"]), 80),
                        "tone": "ok",
                        "meta": "user",
                    }
                )

            digest_rows = await (
                await db.execute(
                    """
                    SELECT datetime(created_at, '+7 hours') AS local_created_at, digest_type, content
                    FROM digests
                    WHERE date(created_at, '+7 hours') = ?
                    ORDER BY id DESC
                    LIMIT 10
                    """,
                    (today,),
                )
            ).fetchall()
            for row in digest_rows:
                timeline_events.append(
                    {
                        "sort_key": row["local_created_at"],
                        "time": _format_short_time(row["local_created_at"]),
                        "type": "cron",
                        "title": f"Digest Created: {str(row['digest_type']).title()}",
                        "message": _truncate_text(_sanitize_admin_text(row["content"]), 80),
                        "tone": "ok",
                        "meta": row["digest_type"],
                    }
                )

            timeline_events.sort(key=lambda item: item["sort_key"], reverse=True)
            overview["timeline"] = timeline_events[:40]
        except Exception:
            overview["timeline"] = []

        try:
            usage_counts = {
                "chat mode": 0,
                "/note": 0,
                "/task": 0,
                "/tasks": 0,
                "/done": 0,
                "/today": 0,
                "/week": 0,
                "/news": 0,
                "/think": 0,
                "/learn": 0,
                "/park": 0,
                "/search": 0,
                "/voice": 0,
                "/remember": 0,
                "/forget": 0,
                "/memory": 0,
                "/cost": 0,
            }
            chat_rows = await (
                await db.execute(
                    """
                    SELECT content
                    FROM messages
                    WHERE date(created_at, '+7 hours') = ? AND role = 'user'
                    ORDER BY id DESC
                    LIMIT 200
                    """,
                    (today,),
                )
            ).fetchall()
            for row in chat_rows:
                usage_counts[_extract_command_label(row["content"])] = usage_counts.get(
                    _extract_command_label(row["content"]), 0
                ) + 1

            audit_rows = await (
                await db.execute(
                    """
                    SELECT action, details
                    FROM audit_logs
                    WHERE date(created_at, '+7 hours') = ?
                    ORDER BY id DESC
                    LIMIT 200
                    """,
                    (today,),
                )
            ).fetchall()
            audit_map = {
                "note_saved": "/note",
                "task_created": "/task",
                "task_done": "/done",
                "lesson_recorded": "/learn",
                "brainstorm_completed": "/think",
                "news_fetch_completed": "/news",
                "memory_searched": "/search",
                "idea_parked": "/park",
                "long_term_memory_deleted": "/forget",
                "long_term_memory_viewed": "/memory",
                "cost_viewed": "/cost",
                "voice_mode_updated": "/voice",
            }
            for row in audit_rows:
                command = audit_map.get(row["action"])
                if row["action"] == "long_term_memory_saved" and "type=manual" in str(row["details"]):
                    command = "/remember"
                if not command:
                    continue
                usage_counts[command] = usage_counts.get(command, 0) + 1

            overview["agent_usage"] = [
                {"label": label, "count": count}
                for label, count in sorted(usage_counts.items(), key=lambda item: item[1], reverse=True)
                if count > 0
            ][:12]
        except Exception:
            overview["agent_usage"] = []

        scheduler_rows = []
        try:
            scheduler_audit_actions = sorted(
                {
                    action
                    for meta in _SCHEDULER_JOB_META
                    for action in (meta["success_actions"] + meta["failure_actions"])
                }
            )
            audit_rows = await (
                await db.execute(
                    f"""
                    SELECT action, datetime(created_at, '+7 hours') AS local_created_at, details
                    FROM audit_logs
                    WHERE action IN ({",".join("?" for _ in scheduler_audit_actions)})
                    ORDER BY id DESC
                    LIMIT 200
                    """,
                    tuple(scheduler_audit_actions),
                )
            ).fetchall()
            audit_rows = [dict(row) for row in audit_rows]
            latest_metric_row = await (
                await db.execute(
                    """
                    SELECT datetime(recorded_at, '+7 hours') AS local_recorded_at
                    FROM server_metrics
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
            for meta in _SCHEDULER_JOB_META:
                last_success = _latest_time(audit_rows, meta["success_actions"])
                last_failure = _latest_time(audit_rows, meta["failure_actions"])
                if meta["id"] == "server_metrics":
                    last_run = latest_metric_row["local_recorded_at"] if latest_metric_row else None
                    tone = "ok" if last_run else "unknown"
                    status_label = "OK" if last_run else "Unknown"
                elif meta["id"] == "health_check":
                    last_run = last_failure or last_success
                    if last_failure and (not last_success or last_failure >= last_success):
                        tone = "warning"
                        status_label = "Warning"
                    elif last_success:
                        tone = "ok"
                        status_label = "OK"
                    else:
                        tone = "unknown"
                        status_label = "Unknown"
                else:
                    last_run = last_failure or last_success
                    if last_failure and (not last_success or last_failure >= last_success):
                        tone = "danger"
                        status_label = "Error"
                    elif last_success:
                        tone = "ok"
                        status_label = "OK"
                    else:
                        tone = "unknown"
                        status_label = "Unknown"
                scheduler_rows.append(
                    {
                        "name": meta["name"],
                        "schedule": meta["schedule"],
                        "last_run": _dashboard_timestamp(last_run),
                        "next_run": _scheduler_next_run(meta["id"]),
                        "status": status_label,
                        "tone": tone,
                    }
                )
        except Exception:
            scheduler_rows = [
                {
                    "name": meta["name"],
                    "schedule": meta["schedule"],
                    "last_run": "Unknown",
                    "next_run": "Unknown",
                    "status": "Unknown",
                    "tone": "unknown",
                }
                for meta in _SCHEDULER_JOB_META
            ]
        overview["scheduler_health"] = scheduler_rows

        try:
            latest_server_row = await (
                await db.execute(
                    """
                    SELECT
                        datetime(recorded_at, '+7 hours') AS local_recorded_at,
                        cpu_percent,
                        ram_percent,
                        disk_percent
                    FROM server_metrics
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
            last_health_row = await (
                await db.execute(
                    """
                    SELECT action, datetime(created_at, '+7 hours') AS local_created_at
                    FROM audit_logs
                    WHERE action IN ('health_check_probe', 'health_warning_sent')
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
            ).fetchone()
            cpu_percent = float(metrics.get("realtime", {}).get("cpu_percent", 0.0))
            ram_percent = float(metrics.get("realtime", {}).get("ram_percent", 0.0))
            disk_percent = float(metrics.get("realtime", {}).get("disk_percent", 0.0))

            def _metric_tone(value: float) -> str:
                if value > 90:
                    return "danger"
                if value > 80:
                    return "warning"
                return "ok"

            server_rows = [
                {
                    "label": "CPU",
                    "value": f"{cpu_percent:.0f}%",
                    "width": min(max(cpu_percent, 0.0), 100.0),
                    "tone": _metric_tone(cpu_percent),
                    "detail": "Current load",
                },
                {
                    "label": "RAM",
                    "value": f"{ram_percent:.0f}%",
                    "width": min(max(ram_percent, 0.0), 100.0),
                    "tone": _metric_tone(ram_percent),
                    "detail": f"{metrics.get('realtime', {}).get('ram_used_mb', 0)} / {metrics.get('realtime', {}).get('ram_total_mb', 0)} MB",
                },
                {
                    "label": "Disk",
                    "value": f"{disk_percent:.0f}%",
                    "width": min(max(disk_percent, 0.0), 100.0),
                    "tone": _metric_tone(disk_percent),
                    "detail": "Current usage",
                },
            ]
            overview["server_health"] = {
                "rows": server_rows,
                "uptime": status.get("health", {}).get("uptime", "Unknown"),
                "last_backup": status.get("last_backup_time", "Unknown"),
                "last_health_check": _dashboard_timestamp(last_health_row["local_created_at"]) if last_health_row else "Unknown",
                "health_check_status": "Warning" if last_health_row and last_health_row["action"] == "health_warning_sent" else ("OK" if last_health_row else "Unknown"),
                "latest_metrics_time": _dashboard_timestamp(latest_server_row["local_recorded_at"]) if latest_server_row else "Unknown",
            }
        except Exception:
            overview["server_health"] = {
                "rows": [],
                "uptime": "Unknown",
                "last_backup": "Unknown",
                "last_health_check": "Unknown",
                "health_check_status": "Unknown",
                "latest_metrics_time": "Unknown",
            }

    model_rows = []
    availability = status.get("model_availability", {})
    active_model = status.get("active_model", "")
    for row in _MODEL_PANEL_ROWS:
        available = bool(availability.get(row["key"], False))
        is_active = active_model == row["key"]
        status_label = "Active" if is_active else ("Available" if available else "Unavailable")
        tone = "ok" if is_active else ("unknown" if available else "empty")
        model_rows.append(
            {
                **row,
                "available": available,
                "active": is_active,
                "status_label": status_label,
                "tone": tone,
            }
        )
    overview["model_panel"] = {
        "rows": model_rows,
        "routing": [
            ("Chat mode", status.get("active_model_label", "Unknown")),
            ("/news", "Gemini Flash"),
            ("/think", "Multi-model"),
            ("Local fallback", "Qwen 3B"),
        ],
    }

    cron_tones = [row["tone"] for row in overview["scheduler_health"]]
    if any(tone == "danger" for tone in cron_tones):
        cron_label = "Action needed"
        cron_tone = "danger"
    elif any(tone == "warning" for tone in cron_tones):
        cron_label = "Warning"
        cron_tone = "warning"
    elif any(tone == "ok" for tone in cron_tones):
        cron_label = "Healthy"
        cron_tone = "ok"
    else:
        cron_label = "Unknown"
        cron_tone = "unknown"

    server_tones = [row["tone"] for row in overview.get("server_health", {}).get("rows", [])]
    if any(tone == "danger" for tone in server_tones):
        server_label = "Action needed"
        server_tone = "danger"
    elif any(tone == "warning" for tone in server_tones):
        server_label = "Warning"
        server_tone = "warning"
    elif any(tone == "ok" for tone in server_tones):
        server_label = "Healthy"
        server_tone = "ok"
    else:
        server_label = "Unknown"
        server_tone = "unknown"

    if overview["kpis"]:
        overview["kpis"][-2]["value"] = cron_label
        overview["kpis"][-2]["tone"] = cron_tone
        overview["kpis"][-1]["value"] = server_label
        overview["kpis"][-1]["tone"] = server_tone

    try:
        raw_logs = await _load_log_entries("ALL", 120)
        recent_logs = []
        for entry in reversed(raw_logs):
            level = entry["level"]
            message = str(entry["message"])
            lowered = message.lower()
            if level == "INFO" and not any(
                keyword in lowered
                for keyword in ["failed", "warning", "error", "exception", "backup", "health_warning", "traceback"]
            ):
                continue
            recent_logs.append(
                {
                    "time": entry["time"],
                    "level": level,
                    "tone": "danger" if level == "ERROR" else ("warning" if level == "WARNING" else "ok"),
                    "source": _truncate_text(message.split()[0], 24),
                    "message": _truncate_text(message, 96),
                }
            )
            if len(recent_logs) >= 8:
                break
        if not recent_logs:
            for entry in reversed(raw_logs[-5:]):
                recent_logs.append(
                    {
                        "time": entry["time"],
                        "level": entry["level"],
                        "tone": "ok",
                        "source": _truncate_text(str(entry["message"]).split()[0], 24),
                        "message": _truncate_text(entry["message"], 96),
                    }
                )
        overview["recent_logs"] = recent_logs
    except Exception:
        overview["recent_logs"] = []

    return overview


def _render_sidebar_items() -> str:
    items = []
    for label, href, item_id, exists in _ADMIN_SIDEBAR_ITEMS:
        if label == "Overview":
            items.append(f'<a class="sidebar-link active" href="{escape(href, quote=True)}" data-nav="{item_id}">{escape(label)}</a>')
            continue
        if exists:
            items.append(f'<a class="sidebar-link" href="{escape(href, quote=True)}" data-nav="{item_id}">{escape(label)}</a>')
        else:
            items.append(
                f'<a class="sidebar-link muted-link" href="{escape(href, quote=True)}" data-nav="{item_id}">'
                f'<span>{escape(label)}</span><span class="sidebar-badge">soon</span></a>'
            )
    return "\n".join(items)


def _render_kpis(kpis: list[dict]) -> str:
    cards = []
    for item in kpis:
        cards.append(
            f"""
            <article class="metric-card tone-{escape(item['tone'])}">
              <div class="metric-label">{escape(item['label'])}</div>
              <div class="metric-value">{escape(item['value'])}</div>
              <div class="metric-meta">{escape(item['meta'])}</div>
            </article>
            """
        )
    return "\n".join(cards)


def _render_brain_status(rows: list[dict]) -> str:
    items = []
    for row in rows:
        items.append(
            f"""
            <div class="brain-row">
              <div class="brain-main">
                <div class="brain-name">{escape(row['name'])}</div>
                <div class="brain-note">{escape(row['note'])}</div>
              </div>
              <div class="brain-meta">
                <div class="brain-count">{escape(row['count'])}</div>
                <span class="status-badge tone-{escape(row['tone'])}">{escape(row['status'])}</span>
                <div class="brain-latest">{escape(row['latest'])}</div>
              </div>
              <div class="brain-action"><a href="{escape(row['href'], quote=True)}">{escape(row['action'])}</a></div>
            </div>
            """
        )
    return "\n".join(items)


def _render_timeline(items: list[dict]) -> str:
    if not items:
        return '<div class="empty-state">No timeline data today</div>'
    rows = []
    for item in items:
        rows.append(
            f"""
            <div class="timeline-item" data-type="{escape(item['type'], quote=True)}">
              <div class="timeline-time">{escape(item['time'])}</div>
              <div class="timeline-content">
                <div class="timeline-title-row">
                  <span class="timeline-title">{escape(item['title'])}</span>
                  <span class="status-badge tone-{escape(item['tone'])}">{escape(item['type'].title())}</span>
                </div>
                <div class="timeline-message">{escape(item['message'])}</div>
                <div class="timeline-meta">{escape(item['meta'])}</div>
              </div>
            </div>
            """
        )
    return "\n".join(rows)


def _render_model_panel(model_panel: dict) -> str:
    rows = []
    for row in model_panel.get("rows", []):
        action_html = (
            '<span class="small-action disabled">Active</span>'
            if row["active"]
            else (
                f'<form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="{escape(row["key"], quote=True)}"><button class="small-action" type="submit">Set Active</button></form>'
                if row["available"]
                else '<span class="small-action disabled">Unavailable</span>'
            )
        )
        rows.append(
            f"""
            <div class="model-row">
              <div class="model-main">
                <div class="model-name">{escape(row['name'])}</div>
                <div class="model-meta">{escape(row['role'])} · {escape(row['speed'])} · {escape(row['cost'])}</div>
              </div>
              <div class="model-status">
                <span class="status-badge tone-{escape(row['tone'])}">{escape(row['status_label'])}</span>
                {action_html}
              </div>
            </div>
            """
        )
    routing_rows = []
    for source, target in model_panel.get("routing", []):
        routing_rows.append(
            f'<div class="routing-row"><span>{escape(source)}</span><span>{escape(target)}</span></div>'
        )
    return "\n".join(rows) + f'<div class="routing-box">{"".join(routing_rows)}</div>'


def _render_agent_usage(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty-state">No command activity today</div>'
    max_count = max((item["count"] for item in rows), default=1)
    bars = []
    for row in rows:
        width = 0 if max_count <= 0 else (row["count"] / max_count) * 100
        bars.append(
            f"""
            <div class="agent-bar-row">
              <div class="agent-bar-label">{escape(row['label'])}</div>
              <div class="agent-bar-track"><div class="agent-bar-fill" style="width:{width:.2f}%"></div></div>
              <div class="agent-bar-count">{row['count']}</div>
            </div>
            """
        )
    return "\n".join(bars)


def _render_scheduler_health(rows: list[dict]) -> str:
    items = []
    for row in rows:
        items.append(
            f"""
            <div class="scheduler-row">
              <div class="scheduler-main">
                <div class="scheduler-name">{escape(row['name'])}</div>
                <div class="scheduler-meta">{escape(row['schedule'])}</div>
              </div>
              <div class="scheduler-times">
                <div>Last: {escape(row['last_run'])}</div>
                <div>Next: {escape(row['next_run'])}</div>
              </div>
              <div class="scheduler-actions">
                <span class="status-badge tone-{escape(row['tone'])}">{escape(row['status'])}</span>
                <button type="button" class="small-action disabled" disabled>Run Now</button>
                <button type="button" class="small-action disabled" disabled>View Error</button>
                <button type="button" class="small-action disabled" disabled>Disable</button>
              </div>
            </div>
            """
        )
    return "\n".join(items)


def _render_server_health(panel: dict) -> str:
    bars = []
    for row in panel.get("rows", []):
        bars.append(
            f"""
            <div class="server-row">
              <div class="server-row-header">
                <span>{escape(row['label'])}</span>
                <span>{escape(row['value'])}</span>
              </div>
              <div class="mini-bar"><div class="mini-bar-fill tone-{escape(row['tone'])}" style="width:{float(row['width']):.2f}%"></div></div>
              <div class="server-row-detail">{escape(row['detail'])}</div>
            </div>
            """
        )
    return (
        "\n".join(bars)
        + f"""
        <div class="server-meta-grid">
          <div><span class="soft">Backup</span><strong>{escape(panel.get('last_backup', 'Unknown'))}</strong></div>
          <div><span class="soft">Health Check</span><strong>{escape(panel.get('health_check_status', 'Unknown'))} · {escape(panel.get('last_health_check', 'Unknown'))}</strong></div>
          <div><span class="soft">Uptime</span><strong>{escape(panel.get('uptime', 'Unknown'))}</strong></div>
          <div><span class="soft">Metrics Row</span><strong>{escape(panel.get('latest_metrics_time', 'Unknown'))}</strong></div>
        </div>
        """
    )


def _render_recent_logs(rows: list[dict]) -> str:
    if not rows:
        return '<div class="empty-state">No high-signal logs</div>'
    items = []
    for row in rows:
        items.append(
            f"""
            <a class="log-preview-row" href="/admin/logs">
              <span class="log-time">{escape(row['time'])}</span>
              <span class="status-badge tone-{escape(row['tone'])}">{escape(row['level'])}</span>
              <span class="log-source">{escape(row['source'])}</span>
              <span class="log-message">{escape(row['message'])}</span>
            </a>
            """
        )
    return "\n".join(items)


def _render_workspace_placeholders() -> str:
    placeholders = [
        ("Conversations", "Detailed private conversation view stays off Overview for privacy."),
        ("Notes", "Dedicated notes management can be added later without changing the shell."),
        ("Tasks", "Use KPI, timeline, and scheduler panels now; detailed task page can come next."),
        ("Memory", "Overview shows health only; full memory content should stay on a dedicated page."),
        ("Daily Digest", "Latest digest health is visible above; drill-down page can be added later."),
        ("Agents", "Agent usage is active above; deeper per-agent controls are not implemented yet."),
        ("Settings", "Model switch and voice status are live; more settings can land here later."),
    ]
    return "\n".join(
        [
            f"""
            <div class="placeholder-card">
              <div class="placeholder-title">{escape(title)}</div>
              <div class="placeholder-copy">{escape(copy)}</div>
            </div>
            """
            for title, copy in placeholders
        ]
    )


def build_admin_html(overview: dict) -> HTMLResponse:
    topbar = overview.get("topbar", {})
    kpis_html = _render_kpis(overview.get("kpis", []))
    brain_html = _render_brain_status(overview.get("brain_status", []))
    timeline_html = _render_timeline(overview.get("timeline", []))
    model_html = _render_model_panel(overview.get("model_panel", {}))
    usage_html = _render_agent_usage(overview.get("agent_usage", []))
    scheduler_html = _render_scheduler_health(overview.get("scheduler_health", []))
    server_html = _render_server_health(overview.get("server_health", {}))
    logs_html = _render_recent_logs(overview.get("recent_logs", []))
    sidebar_html = _render_sidebar_items()
    workspace_html = _render_workspace_placeholders()
    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Admin</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0b1020;
      --sidebar: #10182d;
      --panel: #121a2c;
      --panel-2: #0f1727;
      --border: #24304a;
      --text: #eef3ff;
      --muted: #9aa8c7;
      --ok: #73bf69;
      --warn: #fade2a;
      --danger: #ff7383;
      --unknown: #7f8ca8;
      --empty: #4f5d7a;
      --accent: #7c9cff;
      --shadow: 0 20px 50px rgba(0, 0, 0, 0.22);
    }}
    * {{
      box-sizing: border-box;
    }}
    html {{
      scroll-behavior: smooth;
    }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top, #13203b 0%, var(--bg) 36%);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    a {{
      color: inherit;
      text-decoration: none;
    }}
    button {{
      font: inherit;
    }}
    .admin-shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
    }}
    .admin-sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 22px 16px;
      background: rgba(16, 24, 45, 0.92);
      border-right: 1px solid var(--border);
      backdrop-filter: blur(18px);
    }}
    .sidebar-brand {{
      margin-bottom: 18px;
    }}
    .sidebar-eyebrow {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }}
    .sidebar-title {{
      font-size: 22px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .sidebar-copy {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
      margin-bottom: 18px;
    }}
    .sidebar-nav {{
      display: grid;
      gap: 6px;
    }}
    .sidebar-link {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid transparent;
      color: #dce5ff;
      background: transparent;
    }}
    .sidebar-link:hover {{
      border-color: var(--border);
      background: rgba(255, 255, 255, 0.03);
    }}
    .sidebar-link.active {{
      background: rgba(124, 156, 255, 0.14);
      border-color: rgba(124, 156, 255, 0.35);
    }}
    .muted-link {{
      color: #c2cce6;
    }}
    .sidebar-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 40px;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid rgba(127, 140, 168, 0.32);
      background: rgba(127, 140, 168, 0.12);
      color: var(--muted);
      font-size: 10px;
      line-height: 1.4;
      text-transform: lowercase;
    }}
    .sidebar-footer {{
      margin-top: 18px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.7;
    }}
    .admin-main {{
      min-width: 0;
      padding: 20px;
    }}
    .admin-topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
      flex-wrap: wrap;
    }}
    .topbar-title {{
      font-size: 28px;
      font-weight: 700;
    }}
    .topbar-subtitle {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
    }}
    .topbar-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
    }}
    .topbar-chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 12px;
      background: rgba(18, 26, 44, 0.92);
      border: 1px solid var(--border);
      border-radius: 12px;
      font-size: 12px;
    }}
    .topbar-chip strong {{
      font-size: 13px;
    }}
    .refresh-btn {{
      border: 1px solid rgba(124, 156, 255, 0.42);
      background: rgba(124, 156, 255, 0.12);
      color: var(--text);
      border-radius: 12px;
      padding: 10px 12px;
      cursor: pointer;
    }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }}
    .metric-card,
    .panel {{
      background: linear-gradient(180deg, rgba(18, 26, 44, 0.96), rgba(15, 23, 39, 0.94));
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }}
    .metric-card {{
      padding: 16px;
      min-height: 118px;
    }}
    .metric-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.07em;
      margin-bottom: 8px;
    }}
    .metric-value {{
      font-size: 31px;
      font-weight: 700;
      line-height: 1.15;
      margin-bottom: 10px;
    }}
    .metric-meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .overview-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, 0.9fr);
      gap: 16px;
    }}
    .stack {{
      display: grid;
      gap: 16px;
    }}
    .panel {{
      padding: 16px;
    }}
    .panel-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    .panel-title {{
      font-size: 17px;
      font-weight: 700;
    }}
    .panel-copy {{
      color: var(--muted);
      font-size: 13px;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 5px 9px;
      border-radius: 999px;
      font-size: 11px;
      border: 1px solid transparent;
      white-space: nowrap;
    }}
    .tone-ok {{
      border-color: rgba(115, 191, 105, 0.45);
      background: rgba(115, 191, 105, 0.12);
      color: #bbf1b4;
    }}
    .tone-warning {{
      border-color: rgba(250, 222, 42, 0.4);
      background: rgba(250, 222, 42, 0.12);
      color: #fff0a3;
    }}
    .tone-danger {{
      border-color: rgba(255, 115, 131, 0.45);
      background: rgba(255, 115, 131, 0.12);
      color: #ffc0c8;
    }}
    .tone-unknown {{
      border-color: rgba(127, 140, 168, 0.4);
      background: rgba(127, 140, 168, 0.12);
      color: #d1d9ea;
    }}
    .tone-empty {{
      border-color: rgba(79, 93, 122, 0.42);
      background: rgba(79, 93, 122, 0.14);
      color: #b5bfd8;
    }}
    .brain-row,
    .timeline-item,
    .scheduler-row,
    .model-row,
    .agent-bar-row,
    .log-preview-row {{
      display: grid;
      gap: 10px;
      border: 1px solid rgba(36, 48, 74, 0.88);
      border-radius: 14px;
      background: rgba(9, 15, 28, 0.62);
      padding: 12px;
    }}
    .brain-row {{
      grid-template-columns: minmax(0, 1fr) auto auto;
      align-items: center;
    }}
    .brain-name,
    .model-name,
    .scheduler-name {{
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .brain-note,
    .model-meta,
    .scheduler-meta,
    .brain-latest,
    .soft,
    .server-row-detail {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .brain-count {{
      font-size: 13px;
      margin-bottom: 6px;
    }}
    .brain-meta {{
      text-align: right;
      min-width: 118px;
    }}
    .brain-action a,
    .panel-link {{
      color: #bad0ff;
      font-size: 12px;
    }}
    .timeline-filters {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, auto));
      gap: 8px;
    }}
    .filter-chip {{
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.02);
      color: var(--text);
      border-radius: 999px;
      padding: 7px 10px;
      cursor: pointer;
    }}
    .filter-chip.active {{
      border-color: rgba(124, 156, 255, 0.42);
      background: rgba(124, 156, 255, 0.12);
    }}
    .timeline-stream {{
      display: grid;
      gap: 10px;
      max-height: 680px;
      overflow: auto;
    }}
    .timeline-item {{
      grid-template-columns: 64px minmax(0, 1fr);
      align-items: start;
    }}
    .timeline-time,
    .log-time {{
      color: var(--muted);
      font-size: 12px;
      padding-top: 2px;
    }}
    .timeline-title-row,
    .server-row-header,
    .routing-row {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .timeline-title,
    .log-source {{
      font-weight: 700;
      font-size: 13px;
    }}
    .timeline-message,
    .log-message {{
      line-height: 1.55;
      font-size: 13px;
      word-break: break-word;
    }}
    .timeline-meta {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 6px;
    }}
    .small-action {{
      border: 1px solid rgba(124, 156, 255, 0.28);
      background: rgba(124, 156, 255, 0.1);
      color: var(--text);
      border-radius: 10px;
      padding: 7px 10px;
      cursor: pointer;
    }}
    .small-action.disabled {{
      cursor: not-allowed;
      opacity: 0.55;
      border-color: var(--border);
      background: rgba(255, 255, 255, 0.03);
    }}
    .model-row {{
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
    }}
    .model-status {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .model-status form {{
      margin: 0;
    }}
    .routing-box {{
      margin-top: 14px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
      display: grid;
      gap: 8px;
    }}
    .agent-bar-row {{
      grid-template-columns: 140px minmax(0, 1fr) 46px;
      align-items: center;
    }}
    .agent-bar-label,
    .agent-bar-count {{
      font-size: 13px;
    }}
    .agent-bar-track,
    .mini-bar {{
      height: 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.06);
      overflow: hidden;
    }}
    .agent-bar-fill {{
      height: 100%;
      background: linear-gradient(90deg, var(--accent), #73bf69);
    }}
    .scheduler-row {{
      grid-template-columns: minmax(0, 1.1fr) auto auto;
      align-items: center;
    }}
    .scheduler-times,
    .scheduler-actions {{
      display: grid;
      gap: 6px;
      justify-items: end;
      font-size: 12px;
    }}
    .server-row {{
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
    }}
    .mini-bar-fill {{
      height: 100%;
      display: block;
    }}
    .mini-bar-fill.tone-ok {{
      background: linear-gradient(90deg, rgba(115, 191, 105, 0.95), rgba(115, 191, 105, 0.62));
    }}
    .mini-bar-fill.tone-warning {{
      background: linear-gradient(90deg, rgba(250, 222, 42, 0.96), rgba(250, 222, 42, 0.62));
    }}
    .mini-bar-fill.tone-danger {{
      background: linear-gradient(90deg, rgba(255, 115, 131, 0.96), rgba(255, 115, 131, 0.62));
    }}
    .server-meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .server-meta-grid div {{
      border: 1px solid rgba(36, 48, 74, 0.88);
      border-radius: 12px;
      padding: 10px;
      background: rgba(9, 15, 28, 0.62);
      display: grid;
      gap: 5px;
    }}
    .log-preview-list {{
      display: grid;
      gap: 10px;
    }}
    .log-preview-row {{
      grid-template-columns: 52px auto 88px minmax(0, 1fr);
      align-items: center;
    }}
    .workspace-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .placeholder-card {{
      border: 1px dashed rgba(124, 156, 255, 0.28);
      background: rgba(255, 255, 255, 0.02);
      border-radius: 14px;
      padding: 14px;
    }}
    .placeholder-title {{
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    .placeholder-copy,
    .empty-state {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.65;
    }}
    .mobile-nav {{
      display: none;
    }}
    @media (max-width: 1100px) {{
      .admin-shell {{
        grid-template-columns: 1fr;
      }}
      .admin-sidebar {{
        position: static;
        height: auto;
      }}
      .overview-grid {{
        grid-template-columns: 1fr;
      }}
      .metric-grid {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
    }}
    @media (max-width: 700px) {{
      .admin-main {{
        padding: 14px;
      }}
      .mobile-nav {{
        display: block;
      }}
      .sidebar-nav {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .metric-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }}
      .brain-row,
      .model-row,
      .scheduler-row,
      .log-preview-row {{
        grid-template-columns: 1fr;
      }}
      .brain-meta,
      .scheduler-times,
      .scheduler-actions {{
        justify-items: start;
        text-align: left;
      }}
      .timeline-filters {{
        grid-template-columns: repeat(3, minmax(0, auto));
      }}
      .agent-bar-row,
      .server-meta-grid,
      .workspace-grid {{
        grid-template-columns: 1fr;
      }}
    }}
    @media (max-width: 560px) {{
      .metric-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="admin-shell">
    <aside class="admin-sidebar">
      <div class="sidebar-brand">
        <div class="sidebar-eyebrow">Personal AI Command Center</div>
        <div class="sidebar-title">Ener-AI</div>
        <div class="sidebar-copy">Overview is fully live now. Metrics and Logs keep their existing pages. Other sections stay safe placeholders until backend routes exist.</div>
      </div>
      <nav class="sidebar-nav">
        {sidebar_html}
      </nav>
      <div class="sidebar-footer">
        <div>Basic Auth stays enabled.</div>
        <div>No secrets or raw memory content are exposed on this page.</div>
      </div>
    </aside>

    <main class="admin-main">
      <div class="admin-topbar" id="overview">
        <div>
          <div class="topbar-title">{escape(topbar.get("title", "Ener-AI Admin"))}</div>
          <div class="topbar-subtitle">Personal AI Command Center overview</div>
        </div>
        <div class="topbar-meta">
          <div class="topbar-chip"><span>Env</span><strong>{escape(topbar.get("environment", "Unknown"))}</strong></div>
          <div class="topbar-chip"><span>Server</span><strong>{escape(topbar.get("server", "Unknown"))}</strong></div>
          <div class="topbar-chip"><span>Time</span><strong>{escape(topbar.get("timezone", "Unknown"))}</strong></div>
          <div class="topbar-chip"><span>Model</span><strong>{escape(topbar.get("active_model", "Unknown"))}</strong></div>
          <div class="topbar-chip"><strong>{escape(topbar.get("voice", "Unknown"))}</strong></div>
          <div class="topbar-chip"><span>Health</span><strong>{escape(topbar.get("health", "Unknown"))}</strong></div>
          <div class="topbar-chip"><span>Cost Today</span><strong>{escape(topbar.get("cost_today", "฿0.00"))}</strong></div>
          <div class="topbar-chip"><span>Last Refresh</span><strong>{escape(topbar.get("last_refresh", "Unknown"))}</strong></div>
          <button class="refresh-btn" type="button" onclick="window.location.reload()">Refresh</button>
        </div>
      </div>

      <section class="metric-grid">
        {kpis_html}
      </section>

      <section class="overview-grid">
        <div class="stack">
          <section class="panel" id="brain-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Brain Status</div>
                <div class="panel-copy">Only counts, timestamps, and health indicators are shown here.</div>
              </div>
              <a class="panel-link" href="#workspace-panel">Sections</a>
            </div>
            <div class="stack">{brain_html}</div>
          </section>

          <section class="panel" id="timeline-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Today Timeline</div>
                <div class="panel-copy">Recent important events merged from logs, AI runs, messages, and digests.</div>
              </div>
              <div class="timeline-filters">
                <button class="filter-chip active" type="button" data-filter="all">All</button>
                <button class="filter-chip" type="button" data-filter="chat">Chat</button>
                <button class="filter-chip" type="button" data-filter="task">Task</button>
                <button class="filter-chip" type="button" data-filter="note">Note</button>
                <button class="filter-chip" type="button" data-filter="memory">Memory</button>
                <button class="filter-chip" type="button" data-filter="cron">Cron</button>
              </div>
            </div>
            <div class="timeline-stream" id="timeline-stream">{timeline_html}</div>
          </section>

          <section class="panel" id="usage-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Agent Usage</div>
                <div class="panel-copy">Today command usage inferred from audit logs and chat records.</div>
              </div>
            </div>
            <div class="stack">{usage_html}</div>
          </section>

          <section class="panel" id="workspace-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Workspace Sections</div>
                <div class="panel-copy">Safe placeholders for sections without dedicated backend pages yet.</div>
              </div>
            </div>
            <div class="workspace-grid">{workspace_html}</div>
          </section>
        </div>

        <div class="stack">
          <section class="panel" id="models-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">AI Model Panel</div>
                <div class="panel-copy">Active model uses the existing `/admin/switch-model` route. Other actions stay disabled.</div>
              </div>
            </div>
            <div class="stack">{model_html}</div>
          </section>

          <section class="panel" id="scheduler-panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Scheduler Health</div>
                <div class="panel-copy">Last run is inferred from audit logs and metrics rows. Unknown means no reliable signal yet.</div>
              </div>
            </div>
            <div class="stack">{scheduler_html}</div>
          </section>

          <section class="panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Server Health</div>
                <div class="panel-copy">Current thresholds only. Metrics page still contains the detailed charts.</div>
              </div>
              <a class="panel-link" href="/admin/metrics">Open Metrics</a>
            </div>
            {server_html}
          </section>

          <section class="panel">
            <div class="panel-header">
              <div>
                <div class="panel-title">Recent Errors / Logs</div>
                <div class="panel-copy">High-signal entries only. Full log stream stays on the dedicated logs page.</div>
              </div>
              <a class="panel-link" href="/admin/logs">Open Logs</a>
            </div>
            <div class="log-preview-list">{logs_html}</div>
          </section>
        </div>
      </section>
    </main>
  </div>
  <script>
    const chips = document.querySelectorAll(".filter-chip");
    const rows = document.querySelectorAll(".timeline-item");
    chips.forEach((chip) => {{
      chip.addEventListener("click", () => {{
        chips.forEach((item) => item.classList.remove("active"));
        chip.classList.add("active");
        const filter = chip.dataset.filter;
        rows.forEach((row) => {{
          const matches = filter === "all" || row.dataset.type === filter;
          row.style.display = matches ? "grid" : "none";
        }});
      }});
    }});
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def build_metrics_html(status: dict, metrics: dict) -> HTMLResponse:
    status_json = json.dumps(status, ensure_ascii=False)
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Metrics</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/css/tabler.min.css">
  <script src="https://cdn.jsdelivr.net/npm/@tabler/core@1.0.0-beta20/dist/js/tabler.min.js" defer></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>
    body {{
      background: #0b0c0f;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .navbar,
    .card {{
      background: #111217 !important;
      border-color: #252932 !important;
    }}
    .grafana-select,
    .grafana-btn {{
      background: #151820 !important;
      color: #f2f3f7 !important;
      border: 1px solid #2d3340 !important;
      font-family: inherit;
    }}
    .grafana-btn {{
      border-radius: 10px;
      padding: 9px 11px;
      text-decoration: none;
    }}
    .card.card-sm .subheader {{
      color: #ffffff !important;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    .card.card-sm .h1 {{
      font-size: 1.8rem;
      line-height: 1.2;
      color: #F5F5FF !important;
      font-weight: 700;
    }}
    .chart-wrap {{
      position: relative;
      height: 260px;
    }}
    .conversation-panel {{
      max-height: 420px;
      overflow: auto;
    }}
    .conversation-item {{
      padding: 12px 14px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.07);
      color: #F5F5FF;
      background: rgba(255, 255, 255, 0.03);
      border-radius: 12px;
      margin-bottom: 8px;
      line-height: 1.55;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .conversation-item:last-child {{
      border-bottom: none;
      margin-bottom: 0;
    }}
    .table thead th,
    .table tbody td,
    .card-title,
    .subheader,
    .text-muted,
    .navbar-brand,
    .navbar-nav .nav-link {{
      color: #dbe1ea !important;
    }}
    .table tbody td {{
      border-color: rgba(255, 255, 255, 0.07) !important;
    }}
  </style>
</head>
<body class="antialiased theme-dark">
  <div class="page">
    <div class="navbar navbar-expand-md d-print-none">
      <div class="container-xl">
        <div class="navbar-brand navbar-brand-autodark">📊 Ener-AI Metrics</div>
        <div class="navbar-nav flex-row order-md-last gap-2">
          <a class="grafana-btn" href="/admin">← Admin</a>
          <button class="grafana-btn" id="prev-range" type="button">◄</button>
          <select id="range-select" class="form-select grafana-select">
            <option value="1h">Last 1h</option>
            <option value="3h">Last 3h</option>
            <option value="10h" selected>Last 10h</option>
            <option value="24h">Last 24h</option>
            <option value="7d">Last 7d</option>
          </select>
          <button class="grafana-btn" id="next-range" type="button">►</button>
          <select id="refresh-select" class="form-select grafana-select">
            <option value="10000">Refresh: 10s</option>
            <option value="30000" selected>Refresh: 30s</option>
            <option value="60000">Refresh: 1m</option>
            <option value="300000">Refresh: 5m</option>
            <option value="0">Refresh: off</option>
          </select>
        </div>
      </div>
    </div>

    <div class="page-wrapper">
      <div class="container-xl py-3">
        <div class="row row-cards mb-3">
          <div class="col-6 col-md-4 col-xl">
            <div class="card card-sm">
              <div class="card-body">
                <div class="subheader">CPU</div>
                <div class="h1 mb-2" id="stat-cpu">0%</div>
                <div class="text-muted">Last 1m</div>
              </div>
            </div>
          </div>
          <div class="col-6 col-md-4 col-xl">
            <div class="card card-sm">
              <div class="card-body">
                <div class="subheader">RAM</div>
                <div class="h1 mb-2" id="stat-ram">0%</div>
                <div class="text-muted" id="stat-ram-meta">0/0 MB</div>
              </div>
            </div>
          </div>
          <div class="col-6 col-md-4 col-xl">
            <div class="card card-sm">
              <div class="card-body">
                <div class="subheader">DISK</div>
                <div class="h1 mb-2" id="stat-disk">0%</div>
                <div class="text-muted">ใช้งานปัจจุบัน</div>
              </div>
            </div>
          </div>
          <div class="col-6 col-md-4 col-xl">
            <div class="card card-sm">
              <div class="card-body">
                <div class="subheader">AI Calls</div>
                <div class="h1 mb-2" id="stat-calls">0</div>
                <div class="text-muted">Today</div>
              </div>
            </div>
          </div>
          <div class="col-6 col-md-4 col-xl">
            <div class="card card-sm">
              <div class="card-body">
                <div class="subheader">Cost</div>
                <div class="h1 mb-2" id="stat-cost">฿0.00</div>
                <div class="text-muted">Today</div>
              </div>
            </div>
          </div>
        </div>

        <div class="row row-cards">
          <div class="col-lg-6">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">CPU Usage (%)</h3>
              </div>
              <div class="card-body">
                <div class="chart-wrap"><canvas id="cpuChart" height="120"></canvas></div>
              </div>
              <div class="card-table">
                <table class="table table-vcenter">
                  <thead><tr><th>Name</th><th>Last</th><th>Min</th><th>Max</th><th>Mean</th></tr></thead>
                  <tbody id="cpu-table"></tbody>
                </table>
              </div>
            </div>
          </div>
          <div class="col-lg-6">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">Memory Usage (%)</h3>
              </div>
              <div class="card-body">
                <div class="chart-wrap"><canvas id="ramChart" height="120"></canvas></div>
              </div>
              <div class="card-table">
                <table class="table table-vcenter">
                  <thead><tr><th>Name</th><th>Last</th><th>Min</th><th>Max</th><th>Mean</th></tr></thead>
                  <tbody id="ram-table"></tbody>
                </table>
              </div>
            </div>
          </div>
          <div class="col-lg-6">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">AI Calls per hour</h3>
              </div>
              <div class="card-body">
                <div class="chart-wrap"><canvas id="callsChart" height="120"></canvas></div>
              </div>
              <div class="card-table">
                <table class="table table-vcenter">
                  <thead><tr><th>Name</th><th>Last</th><th>Min</th><th>Max</th><th>Mean</th></tr></thead>
                  <tbody id="calls-table"></tbody>
                </table>
              </div>
            </div>
          </div>
          <div class="col-lg-6">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">Cost per day (7d)</h3>
              </div>
              <div class="card-body">
                <div class="chart-wrap"><canvas id="costChart" height="120"></canvas></div>
              </div>
              <div class="card-table">
                <table class="table table-vcenter">
                  <thead><tr><th>Name</th><th>Last</th><th>Min</th><th>Max</th><th>Mean</th></tr></thead>
                  <tbody id="cost-table"></tbody>
                </table>
              </div>
            </div>
          </div>
          <div class="col-12">
            <div class="card">
              <div class="card-header">
                <h3 class="card-title">💬 บทสนทนาล่าสุด</h3>
              </div>
              <div class="card-body conversation-panel" id="conversation-list"></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const initialStatus = {status_json};
    const initialMetrics = {metrics_json};
    const rangeOptions = ["1h", "3h", "10h", "24h", "7d"];
    let currentRange = initialMetrics.range || "10h";
    let refreshHandle = null;
    let cpuChart = null;
    let ramChart = null;
    let callsChart = null;
    let costChart = null;

    function formatMetricValue(value, suffix = "") {{
      const amount = Number(value || 0);
      if (suffix === "฿") return `฿${{amount.toFixed(2)}}`;
      if (!suffix) return `${{amount.toFixed(1)}}`;
      return `${{amount.toFixed(1)}}${{suffix}}`;
    }}

    function statRow(name, data, suffix = "") {{
      return `<tr><td>${{name}}</td><td>${{formatMetricValue(data.last, suffix)}}</td><td>${{formatMetricValue(data.min, suffix)}}</td><td>${{formatMetricValue(data.max, suffix)}}</td><td>${{formatMetricValue(data.mean, suffix)}}</td></tr>`;
    }}

    function graphOptions() {{
      return {{
        responsive: true,
        maintainAspectRatio: false,
        plugins: {{
          legend: {{ labels: {{ color: "#dbe1ea" }} }},
          tooltip: {{
            backgroundColor: "#111217",
            borderColor: "#2d3340",
            borderWidth: 1,
            titleColor: "#f2f3f7",
            bodyColor: "#f2f3f7",
          }}
        }},
        scales: {{
          x: {{ ticks: {{ color: "#98a2b3" }}, grid: {{ color: "rgba(255,255,255,0.07)" }} }},
          y: {{ ticks: {{ color: "#98a2b3" }}, grid: {{ color: "rgba(255,255,255,0.07)" }} }}
        }}
      }};
    }}

    function makeLineChart(el, label, labels, values, color) {{
      const ctx = document.getElementById(el).getContext("2d");
      const gradient = ctx.createLinearGradient(0, 0, 0, 260);
      gradient.addColorStop(0, color + "55");
      gradient.addColorStop(1, color + "00");
      return new Chart(ctx, {{
        type: "line",
        data: {{
          labels,
          datasets: [{{
            label,
            data: values,
            borderColor: color,
            backgroundColor: gradient,
            fill: true,
            pointRadius: 0,
            tension: 0.3
          }}]
        }},
        options: graphOptions()
      }});
    }}

    function makeBarChart(el, labels, datasets) {{
      return new Chart(document.getElementById(el), {{
        type: "bar",
        data: {{ labels, datasets }},
        options: graphOptions()
      }});
    }}

    function renderStatus(status) {{
      document.getElementById("stat-calls").textContent = status.today_calls;
      document.getElementById("stat-cost").textContent = `฿${{Number(status.today_cost_thb).toFixed(2)}}`;
      const list = document.getElementById("conversation-list");
      list.innerHTML = "";
      if (!status.recent_conversations.length) {{
        list.innerHTML = "<div class='conversation-item'>ยังไม่มีบทสนทนา</div>";
      }} else {{
        for (const item of status.recent_conversations) {{
          const row = document.createElement("div");
          row.className = "conversation-item";
          row.textContent = `${{item.time}} [${{item.model_label}}] ${{item.user || "-"}} / ${{item.assistant || "-"}}`;
          list.appendChild(row);
        }}
      }}
    }}

    function renderMetrics(metrics) {{
      currentRange = metrics.range;
      document.getElementById("range-select").value = currentRange;
      document.getElementById("stat-cpu").textContent = `${{Number(metrics.realtime.cpu_percent).toFixed(0)}}%`;
      document.getElementById("stat-ram").textContent = `${{Number(metrics.realtime.ram_percent).toFixed(0)}}%`;
      document.getElementById("stat-ram-meta").textContent = `${{metrics.realtime.ram_used_mb}}/${{metrics.realtime.ram_total_mb}}`;
      document.getElementById("stat-disk").textContent = `${{Number(metrics.realtime.disk_percent).toFixed(0)}}%`;

      if (!cpuChart) {{
        cpuChart = makeLineChart("cpuChart", "CPU", metrics.labels, metrics.cpu, "#73bf69");
        ramChart = makeLineChart("ramChart", "RAM", metrics.labels, metrics.ram, "#5794f2");
        callsChart = makeBarChart("callsChart", Object.keys(metrics.ai_calls_hourly), Object.keys(metrics.ai_calls_by_model).map((model, idx) => ({{
          label: model,
          data: Object.keys(metrics.ai_calls_hourly).map((label) => metrics.ai_calls_by_model[model][label] || 0),
          backgroundColor: ["#73bf69", "#5794f2", "#fade2a", "#ff9830", "#e24d42"][idx % 5]
        }})));
        costChart = makeBarChart("costChart", Object.keys(metrics.cost_daily), [{{
          label: "Cost",
          data: Object.values(metrics.cost_daily),
          backgroundColor: "#fade2a"
        }}]);
      }} else {{
        cpuChart.data.labels = metrics.labels;
        cpuChart.data.datasets[0].data = metrics.cpu;
        cpuChart.update();
        ramChart.data.labels = metrics.labels;
        ramChart.data.datasets[0].data = metrics.ram;
        ramChart.update();
        const callLabels = Object.keys(metrics.ai_calls_hourly);
        callsChart.data.labels = callLabels;
        callsChart.data.datasets = Object.keys(metrics.ai_calls_by_model).map((model, idx) => ({{
          label: model,
          data: callLabels.map((label) => metrics.ai_calls_by_model[model][label] || 0),
          backgroundColor: ["#73bf69", "#5794f2", "#fade2a", "#ff9830", "#e24d42"][idx % 5]
        }}));
        callsChart.update();
        costChart.data.labels = Object.keys(metrics.cost_daily);
        costChart.data.datasets[0].data = Object.values(metrics.cost_daily);
        costChart.update();
      }}

      document.getElementById("cpu-table").innerHTML = statRow("CPU", metrics.stats.cpu, "%");
      document.getElementById("ram-table").innerHTML = statRow("RAM", metrics.stats.ram, "%");
      document.getElementById("calls-table").innerHTML = statRow("Calls", metrics.stats.calls, "");
      document.getElementById("cost-table").innerHTML = statRow("Cost", metrics.stats.cost, "฿");
    }}

    async function loadStatus() {{
      const response = await fetch("/admin/api/status", {{ cache: "no-store" }});
      if (!response.ok) return;
      renderStatus(await response.json());
    }}

    async function loadMetrics() {{
      const response = await fetch(`/admin/api/metrics?range=${{encodeURIComponent(currentRange)}}`, {{ cache: "no-store" }});
      if (!response.ok) return;
      renderMetrics(await response.json());
    }}

    function applyRefreshInterval() {{
      if (refreshHandle) clearInterval(refreshHandle);
      const value = Number(document.getElementById("refresh-select").value);
      if (value > 0) {{
        refreshHandle = setInterval(() => {{
          loadStatus();
          loadMetrics();
        }}, value);
      }}
    }}

    document.getElementById("range-select").addEventListener("change", async (event) => {{
      currentRange = event.target.value;
      await loadMetrics();
    }});
    document.getElementById("refresh-select").addEventListener("change", applyRefreshInterval);
    document.getElementById("prev-range").addEventListener("click", async () => {{
      const idx = rangeOptions.indexOf(currentRange);
      currentRange = rangeOptions[(idx - 1 + rangeOptions.length) % rangeOptions.length];
      await loadMetrics();
    }});
    document.getElementById("next-range").addEventListener("click", async () => {{
      const idx = rangeOptions.indexOf(currentRange);
      currentRange = rangeOptions[(idx + 1) % rangeOptions.length];
      await loadMetrics();
    }});

    renderStatus(initialStatus);
    renderMetrics(initialMetrics);
    applyRefreshInterval();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def build_logs_html() -> HTMLResponse:
    html = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Logs</title>
  <style>
    body { margin: 0; background: #0f0f1a; color: #f2f3f7; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .wrap { max-width: 1280px; margin: 0 auto; padding: 18px 14px 40px; }
    .topbar { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; margin-bottom: 16px; }
    .controls { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
    input, button, a { font-family: inherit; }
    input { background: #17172a; border: 1px solid #35385a; color: #f2f3f7; border-radius: 10px; padding: 10px 12px; }
    button, .link-btn { background: #22243a; border: 1px solid #35385a; color: #f2f3f7; border-radius: 10px; padding: 10px 12px; cursor: pointer; text-decoration: none; }
    .active { border-color: #58d68d; }
    .log-box { background: #111224; border: 1px solid #2b2d42; border-radius: 16px; padding: 12px; height: 70vh; overflow: auto; }
    .log-line { padding: 4px 0; white-space: pre-wrap; word-break: break-word; border-bottom: 1px solid #23253a; }
    .log-line:last-child { border-bottom: none; }
    .ERROR { color: #ff7f7f; }
    .WARNING { color: #ffd166; }
    .INFO { color: #7bd88f; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>📌 Ener-AI Logs</div>
      <a class="link-btn" href="/admin">← กลับหน้า Admin</a>
    </div>
    <div class="controls">
      <input id="search-box" type="text" placeholder="ค้นหา log..." />
      <button class="filter-btn active" data-filter="ALL">ALL</button>
      <button class="filter-btn" data-filter="ERROR">ERROR</button>
      <button class="filter-btn" data-filter="WARNING">WARNING</button>
      <button class="filter-btn" data-filter="INFO">INFO</button>
      <button id="refresh-btn">Refresh</button>
      <button id="toggle-btn">Auto-refresh ON</button>
    </div>
    <div id="log-box" class="log-box"></div>
  </div>
  <script>
    let currentFilter = "ALL";
    let autoRefresh = true;
    let allLogs = [];
    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }
    function renderLogs() {
      const logBox = document.getElementById("log-box");
      const keyword = document.getElementById("search-box").value.trim().toLowerCase();
      const filtered = allLogs.filter((entry) => {
        const text = `${entry.time} ${entry.level} ${entry.message}`.toLowerCase();
        return !keyword || text.includes(keyword);
      });
      logBox.innerHTML = filtered.map((entry) => `
        <div class="log-line ${entry.level}">
          <strong>[${escapeHtml(entry.time)}]</strong> <strong>${escapeHtml(entry.level)}</strong> ${escapeHtml(entry.message)}
        </div>
      `).join("");
      logBox.scrollTop = logBox.scrollHeight;
    }
    async function loadLogs() {
      const response = await fetch(`/admin/api/logs?filter=${currentFilter}&lines=200`, { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      allLogs = payload.lines || [];
      renderLogs();
    }
    for (const button of document.querySelectorAll(".filter-btn")) {
      button.addEventListener("click", async () => {
        currentFilter = button.dataset.filter;
        document.querySelectorAll(".filter-btn").forEach((item) => item.classList.toggle("active", item === button));
        await loadLogs();
      });
    }
    document.getElementById("search-box").addEventListener("input", renderLogs);
    document.getElementById("refresh-btn").addEventListener("click", loadLogs);
    document.getElementById("toggle-btn").addEventListener("click", () => {
      autoRefresh = !autoRefresh;
      document.getElementById("toggle-btn").textContent = autoRefresh ? "Auto-refresh ON" : "Auto-refresh OFF";
    });
    setInterval(() => { if (autoRefresh) loadLogs(); }, 10000);
    loadLogs();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    await init_db()
    await telegram_app.initialize()
    await telegram_app.bot.set_webhook(url=f"{settings.telegram_webhook_url}/webhook")
    await telegram_app.start()
    scheduler = build_scheduler(telegram_app.bot)
    scheduler.start()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/admin")
async def admin_dashboard(request: Request):
    await _require_admin(request)
    return build_admin_html(await _load_admin_overview())


@app.get("/admin/metrics")
async def admin_metrics_dashboard(request: Request):
    await _require_admin(request)
    status, metrics = await asyncio.gather(_load_admin_status(), _load_metrics_payload("10h"))
    return build_metrics_html(status, metrics)


@app.get("/admin/logs")
async def admin_logs(request: Request):
    await _require_admin(request)
    return build_logs_html()


@app.post("/admin/switch-model")
async def admin_switch_model(request: Request):
    await _require_admin(request)
    form_data = parse_qs((await request.body()).decode("utf-8"))
    model = form_data.get("model", [""])[0].strip().lower()
    if model not in {"haiku", "groq", "gemini", "qwen3b", "qwen7b"}:
        raise HTTPException(status_code=400, detail="โมเดลไม่ถูกต้อง")
    if model == "haiku" and not settings.anthropic_api_key:
        raise HTTPException(status_code=400, detail="Claude Haiku ยังไม่มี key")
    if model == "groq" and not settings.groq_api_key:
        raise HTTPException(status_code=400, detail="Groq ยังไม่มี key")
    if model == "gemini" and not settings.gemini_api_key:
        raise HTTPException(status_code=400, detail="Gemini ยังไม่มี key")

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO memories (key, value, tag)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                tag = excluded.tag,
                updated_at = CURRENT_TIMESTAMP
            """,
            ("active_model", model, "system"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("admin_model_switched", f"model={model}"),
        )
        await db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/api/status")
async def admin_status(request: Request):
    await _require_admin(request)
    return JSONResponse(await _load_admin_status())


@app.get("/admin/api/metrics")
async def admin_metrics(request: Request):
    await _require_admin(request)
    return JSONResponse(await _load_metrics_payload(request.query_params.get("range", "10h")))


@app.get("/admin/api/logs")
async def admin_api_logs(request: Request):
    await _require_admin(request)
    filter_value = request.query_params.get("filter", "ALL").upper()
    try:
        lines = int(request.query_params.get("lines", "200"))
    except ValueError:
        lines = 200
    if filter_value not in {"ALL", "ERROR", "WARNING", "INFO"}:
        filter_value = "ALL"
    return JSONResponse({"lines": await _load_log_entries(filter_value, lines)})
