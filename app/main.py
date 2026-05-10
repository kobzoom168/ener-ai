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
from app.core.agents import COMMAND_AGENT_MAP, SCHEDULER_AGENTS
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
_AGENT_ORDER = [
    "MainChatAgent",
    "NoteAgent",
    "TaskAgent",
    "MemoryAgent",
    "LessonAgent",
    "ThinkTeam",
    "NewsAgent",
    "DigestAgent",
    "HealthAgent",
    "BackupAgent",
    "MetricsAgent",
    "VoiceAgent",
    "CostAgent",
]
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


async def _load_agent_stats_payload() -> dict:
    today = datetime.now(_BANGKOK).date().isoformat()
    known_agents = list(dict.fromkeys(_AGENT_ORDER + list(COMMAND_AGENT_MAP.values()) + list(SCHEDULER_AGENTS.values())))
    uptime_delta = datetime.now(_BANGKOK) - _APP_STARTED_AT
    uptime_minutes = int(uptime_delta.total_seconds() // 60)
    uptime_text = f"{uptime_minutes // 60}h {uptime_minutes % 60}m"

    async with get_db() as db:
        stats_cursor = await db.execute(
            """
            SELECT
              agent_name,
              COUNT(*) AS total_runs,
              SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
              SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS fail_count,
              ROUND(AVG(duration_ms)) AS avg_ms,
              ROUND(SUM(cost_thb), 4) AS total_cost
            FROM agent_runs
            WHERE date(created_at, '+7 hours') = date('now', '+7 hours')
            GROUP BY agent_name
            ORDER BY total_runs DESC
            """
        )
        stats_rows = await stats_cursor.fetchall()

        failures_cursor = await db.execute(
            """
            SELECT
                agent_name,
                error_msg,
                datetime(created_at, '+7 hours') AS local_created_at
            FROM agent_runs
            WHERE date(created_at, '+7 hours') = ? AND success = 0
            ORDER BY id DESC
            LIMIT 10
            """,
            (today,),
        )
        failure_rows = await failures_cursor.fetchall()

    stats_by_name = {str(row["agent_name"]): row for row in stats_rows}
    stats = []
    for agent_name in known_agents:
        row = stats_by_name.get(agent_name)
        total_runs = int(row["total_runs"]) if row else 0
        success_count = int(row["success_count"]) if row and row["success_count"] is not None else 0
        fail_count = int(row["fail_count"]) if row and row["fail_count"] is not None else 0
        avg_ms = int(row["avg_ms"]) if row and row["avg_ms"] is not None else 0
        total_cost = float(row["total_cost"] or 0.0) if row else 0.0
        stats.append(
            {
                "agent_name": agent_name,
                "total_runs": total_runs,
                "success_count": success_count,
                "fail_count": fail_count,
                "avg_ms": avg_ms,
                "total_cost": round(total_cost, 4),
                "status": "ok" if total_runs > 0 and fail_count == 0 else ("warning" if fail_count > 0 else "unknown"),
            }
        )

    failures = [
        {
            "agent_name": row["agent_name"],
            "error_msg": _truncate_text(_sanitize_admin_text(row["error_msg"] or "Unknown error"), 120),
            "time": _format_short_time(row["local_created_at"]),
        }
        for row in failure_rows
    ]

    return {
        "main_agent": {
            "name": "Main Agent",
            "status": "ONLINE",
            "uptime": uptime_text,
        },
        "stats": stats,
        "failures": failures,
        "costs": [
            {"agent_name": row["agent_name"], "total_cost": row["total_cost"], "total_runs": row["total_runs"]}
            for row in sorted(stats, key=lambda item: item["total_cost"], reverse=True)
            if int(row["total_runs"]) > 0 or float(row["total_cost"]) > 0
        ],
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


def _format_number(value: int | float, decimals: int = 0) -> str:
    try:
        number = float(value)
    except Exception:
        return "0"
    if decimals <= 0:
        return f"{int(round(number)):,}"
    return f"{number:,.{decimals}f}"


def _format_baht(value: int | float) -> str:
    try:
        number = float(value)
    except Exception:
        number = 0.0
    return f"฿{number:,.2f}"


def _humanize_action(action: str) -> str:
    raw = str(action or "").strip("_ ")
    if not raw:
        return "Unknown"
    return raw.replace("_", " ")


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
    today = now.date().isoformat()

    try:
        status = await _load_admin_status()
    except Exception:
        status = {
            "active_model": "haiku",
            "active_model_label": "Claude Haiku",
            "model_availability": {},
            "today_cost_thb": 0.0,
            "today_calls": 0,
            "month_cost_thb": 0.0,
            "health": {"summary": "0/3 OK", "uptime": "Unknown"},
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
            },
            "ai_usage": {
                "cost_7d_labels": [],
                "cost_7d_values": [],
            },
        }

    try:
        agent_payload = await _load_agent_stats_payload()
    except Exception:
        agent_payload = {"stats": [], "failures": []}

    overview = {
        "topbar": {},
        "stats": [],
        "model_panel": {},
        "cost_breakdown": [],
        "cost_chart": {
            "labels": metrics.get("ai_usage", {}).get("cost_7d_labels", []),
            "values": metrics.get("ai_usage", {}).get("cost_7d_values", []),
        },
        "timeline": [],
        "top_commands": [],
        "server": {},
        "top_agents": [],
        "errors": [],
    }

    async with get_db() as db:
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
        avg_response_row = await (
            await db.execute(
                """
                SELECT COALESCE(AVG(duration_ms), 0) AS avg_ms
                FROM agent_runs
                WHERE date(created_at, '+7 hours') = ? AND success = 1
                """,
                (today,),
            )
        ).fetchone()
        top_commands_rows = await (
            await db.execute(
                """
                SELECT action, COUNT(*) AS total
                FROM audit_logs
                WHERE date(created_at, '+7 hours') = ?
                GROUP BY action
                ORDER BY total DESC, action
                LIMIT 3
                """,
                (today,),
            )
        ).fetchall()
        cost_rows = await (
            await db.execute(
                """
                SELECT agent_name, COUNT(*) AS runs, COALESCE(SUM(cost_thb), 0) AS total_cost
                FROM agent_runs
                WHERE date(created_at, '+7 hours') = ?
                GROUP BY agent_name
                ORDER BY total_cost DESC, runs DESC, agent_name
                LIMIT 8
                """,
                (today,),
            )
        ).fetchall()
        audit_rows = await (
            await db.execute(
                """
                SELECT datetime(created_at, '+7 hours') AS local_created_at, action, details
                FROM audit_logs
                WHERE date(created_at, '+7 hours') = ?
                ORDER BY id DESC
                LIMIT 40
                """,
                (today,),
            )
        ).fetchall()
        ai_run_rows = await (
            await db.execute(
                """
                SELECT datetime(created_at, '+7 hours') AS local_created_at, model, estimated_cost_thb, success
                FROM ai_runs
                WHERE date(created_at, '+7 hours') = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (today,),
            )
        ).fetchall()
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
        error_rows = await (
            await db.execute(
                """
                SELECT agent_name, error_msg, datetime(created_at, '+7 hours') AS local_created_at
                FROM agent_runs
                WHERE date(created_at, '+7 hours') = ? AND success = 0
                ORDER BY id DESC
                LIMIT 8
                """,
                (today,),
            )
        ).fetchall()

    message_total = int(message_count_row["total"] or 0) if message_count_row else 0
    open_tasks = int(task_count_row["total"] or 0) if task_count_row else 0
    avg_response_ms = float(avg_response_row["avg_ms"] or 0.0) if avg_response_row else 0.0

    availability = status.get("model_availability", {})
    active_model = status.get("active_model", "haiku")
    model_rows = []
    for row in _MODEL_PANEL_ROWS:
        model_rows.append(
            {
                "key": row["key"],
                "name": row["name"],
                "active": row["key"] == active_model,
                "available": bool(availability.get(row["key"], False)),
                "cost": row["cost"],
            }
        )

    top_agents = []
    for row in agent_payload.get("stats", [])[:6]:
        if int(row.get("total_runs", 0) or 0) <= 0:
            continue
        top_agents.append(
            {
                "name": row["agent_name"],
                "runs": int(row.get("total_runs", 0) or 0),
                "avg_ms": int(row.get("avg_ms", 0) or 0),
            }
        )

    timeline_events = []
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
                "meta": _humanize_action(row["action"]),
            }
        )
    for row in ai_run_rows:
        timeline_events.append(
            {
                "sort_key": row["local_created_at"],
                "time": _format_short_time(row["local_created_at"]),
                "type": "chat" if row["success"] else "error",
                "title": get_model_label(row["model"]),
                "message": f"฿{float(row['estimated_cost_thb'] or 0):.2f} {'✅' if row['success'] else '❌'}",
                "tone": "ok" if row["success"] else "danger",
                "meta": "ai_run",
            }
        )
    for row in message_rows:
        timeline_events.append(
            {
                "sort_key": row["local_created_at"],
                "time": _format_short_time(row["local_created_at"]),
                "type": "chat",
                "title": "Chat",
                "message": _truncate_text(_sanitize_admin_text(row["content"]), 80),
                "tone": "ok",
                "meta": "message",
            }
        )
    timeline_events.sort(key=lambda item: item["sort_key"], reverse=True)

    overview["topbar"] = {
        "model": status.get("active_model_label", "Unknown"),
        "cost_today": _format_baht(status.get("today_cost_thb", 0.0)),
        "health": status.get("health", {}).get("summary", "0/3 OK"),
        "time": now.strftime("%H:%M"),
    }
    overview["stats"] = [
        {
            "label": "AI CALLS",
            "value": _format_number(status.get("today_calls", 0)),
            "meta": "วันนี้",
        },
        {
            "label": "COST",
            "value": _format_baht(status.get("today_cost_thb", 0.0)),
            "meta": f"เดือน {_format_baht(status.get('month_cost_thb', 0.0))}",
        },
        {
            "label": "MSGS",
            "value": _format_number(message_total),
            "meta": "วันนี้",
        },
        {
            "label": "TASKS",
            "value": _format_number(open_tasks),
            "meta": "open",
        },
    ]
    overview["model_panel"] = {
        "active_model": status.get("active_model_label", "Unknown"),
        "rows": model_rows,
        "avg_response_ms": int(round(avg_response_ms)),
        "top_commands": [
            {"label": _humanize_action(row["action"]), "count": int(row["total"] or 0)}
            for row in top_commands_rows
            if int(row["total"] or 0) > 0
        ],
    }
    overview["cost_breakdown"] = [
        {
            "agent_name": row["agent_name"],
            "runs": int(row["runs"] or 0),
            "total_cost": float(row["total_cost"] or 0.0),
        }
        for row in cost_rows
        if int(row["runs"] or 0) > 0 or float(row["total_cost"] or 0.0) > 0
    ]
    overview["timeline"] = timeline_events[:30]
    overview["server"] = {
        "cpu_percent": float(metrics.get("realtime", {}).get("cpu_percent", 0.0) or 0.0),
        "ram_percent": float(metrics.get("realtime", {}).get("ram_percent", 0.0) or 0.0),
        "disk_percent": float(metrics.get("realtime", {}).get("disk_percent", 0.0) or 0.0),
        "ram_used_mb": int(metrics.get("realtime", {}).get("ram_used_mb", 0) or 0),
        "ram_total_mb": int(metrics.get("realtime", {}).get("ram_total_mb", 0) or 0),
        "uptime": status.get("health", {}).get("uptime", "Unknown"),
    }
    overview["top_agents"] = top_agents
    overview["errors"] = [
        {
            "time": _format_short_time(row["local_created_at"]),
            "agent": row["agent_name"],
            "message": _truncate_text(_sanitize_admin_text(row["error_msg"] or "Unknown error"), 120),
        }
        for row in error_rows
    ]
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


def _render_agent_status_panel(agent_payload: dict) -> str:
    main_agent = agent_payload.get("main_agent", {})
    stats = agent_payload.get("stats", [])
    rows = [
        f"""
        <div class="agent-summary-row">
          <div class="agent-summary-title">🤖 {escape(main_agent.get('name', 'Main Agent'))}</div>
          <div class="agent-summary-meta">
            <span class="status-badge tone-ok">{escape(main_agent.get('status', 'ONLINE'))}</span>
            <span>{escape(main_agent.get('uptime', 'Unknown'))}</span>
          </div>
        </div>
        """
    ]
    for row in stats:
        status_badge = "✅" if row["total_runs"] > 0 and row["fail_count"] == 0 else ("⚠️" if row["fail_count"] > 0 else "—")
        avg_text = f"{row['avg_ms']} ms" if row["total_runs"] > 0 else "—"
        rows.append(
            f"""
            <div class="agent-status-row">
              <div class="agent-status-name">{escape(row['agent_name'])}</div>
              <div class="agent-status-metrics">{row['total_runs']} runs · avg {escape(avg_text)}</div>
              <div class="agent-status-ok">{status_badge}</div>
            </div>
            """
        )
    return "\n".join(rows)


def _render_agent_failures_panel(agent_payload: dict) -> str:
    failures = agent_payload.get("failures", [])
    if not failures:
        return '<div class="empty-state">✅ ไม่มี failure วันนี้</div>'

    rows = []
    for row in failures:
        rows.append(
            f"""
            <div class="failure-row">
              <div class="failure-time">{escape(row['time'])}</div>
              <div class="failure-main">
                <div class="failure-agent">{escape(row['agent_name'])}</div>
                <div class="failure-msg">{escape(row['error_msg'])}</div>
              </div>
            </div>
            """
        )
    return "\n".join(rows)


def _render_agent_costs_panel(agent_payload: dict) -> str:
    cost_rows = agent_payload.get("costs", [])
    if not cost_rows:
        return '<div class="empty-state">ยังไม่มี cost ต่อ agent วันนี้</div>'

    rows = []
    for row in cost_rows:
        rows.append(
            f"""
            <div class="cost-row">
              <span>{escape(row['agent_name'])}</span>
              <strong>฿{float(row['total_cost']):.2f}</strong>
            </div>
            """
        )
    return "\n".join(rows)


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
    stats = overview.get("stats", [])
    model_panel = overview.get("model_panel", {})
    cost_breakdown = overview.get("cost_breakdown", [])
    cost_chart = overview.get("cost_chart", {})
    timeline = overview.get("timeline", [])
    server = overview.get("server", {})
    top_agents = overview.get("top_agents", [])
    errors = overview.get("errors", [])

    def _progress_class(value: float) -> str:
        if value >= 80:
            return "danger"
        if value >= 60:
            return "warning"
        return "ok"

    stats_html = "".join(
        f"""
        <section class="card stat-card">
          <div class="stat-label">{escape(str(item.get("label", "")))}</div>
          <div class="stat-number">{escape(str(item.get("value", "0")))}</div>
          <div class="stat-meta">{escape(str(item.get("meta", "")))}</div>
        </section>
        """
        for item in stats
    )

    model_switch_html = "".join(
        f"""
        <form method="post" action="/admin/switch-model" class="model-pill-form">
          <input type="hidden" name="model" value="{escape(row["key"], quote=True)}">
          <button class="model-pill {'active' if row['active'] else ''}" type="submit" {'disabled' if not row['available'] else ''}>
            {escape(row["name"])}
          </button>
        </form>
        """
        for row in model_panel.get("rows", [])
    )

    top_commands_html = ""
    if model_panel.get("top_commands"):
        top_commands_html = (
            '<div class="subsection"><div class="subheading">Top Commands Today</div><div class="mini-list">'
            + "".join(
                f'<div class="mini-row"><span>{escape(str(item["label"]))}</span><strong>{_format_number(item["count"])}</strong></div>'
                for item in model_panel.get("top_commands", [])
            )
            + "</div></div>"
        )

    left_cards_html = f"""
      <section class="card">
        <div class="card-title">🤖 MODEL</div>
        <div class="card-subtitle">Active: {escape(str(model_panel.get("active_model", "Unknown")))}</div>
        <div class="model-pills">{model_switch_html}</div>
        <div class="mini-row muted-row"><span>Avg response</span><strong>{_format_number(model_panel.get("avg_response_ms", 0))} ms</strong></div>
        {top_commands_html}
      </section>
    """

    if cost_breakdown or cost_chart.get("labels"):
        max_cost = max([float(item.get("total_cost", 0.0) or 0.0) for item in cost_breakdown] + [0.0])
        breakdown_rows = "".join(
            f"""
            <div class="cost-row">
              <div>
                <div class="row-title">{escape(str(item["agent_name"]))}</div>
                <div class="row-meta">{_format_number(item["runs"])} runs</div>
              </div>
              <div class="row-cost">{_format_baht(item["total_cost"])}</div>
            </div>
            <div class="agent-bar"><div class="agent-bar-fill" style="width:{(float(item['total_cost']) / max_cost * 100) if max_cost else 0:.1f}%"></div></div>
            """
            for item in cost_breakdown
        )
        left_cards_html += f"""
          <section class="card">
            <div class="card-title">💰 COST BREAKDOWN</div>
            <div class="list-stack">{breakdown_rows}</div>
            <div class="chart-block">
              <div class="subheading">7-Day Cost</div>
              <canvas id="cost7dChart" height="90"></canvas>
            </div>
          </section>
        """

    timeline_html = ""
    if timeline:
        rows_html = "".join(
            f"""
            <div class="timeline-item" data-type="{escape(str(item['type']), quote=True)}">
              <div class="timeline-time">{escape(str(item['time']))}</div>
              <div class="timeline-body">
                <div class="timeline-head">
                  <div class="timeline-title">{escape(str(item['title']))}</div>
                  <div class="timeline-tone {escape(str(item['tone']))}"></div>
                </div>
                <div class="timeline-message">{escape(str(item['message']))}</div>
              </div>
            </div>
            """
            for item in timeline
        )
        timeline_html = f"""
          <section class="card timeline-card">
            <div class="card-title">📊 TODAY</div>
            <div class="timeline-filters">
              <button class="filter-chip active" type="button" data-filter="all">All</button>
              <button class="filter-chip" type="button" data-filter="chat">Chat</button>
              <button class="filter-chip" type="button" data-filter="task">Task</button>
              <button class="filter-chip" type="button" data-filter="memory">Memory</button>
              <button class="filter-chip" type="button" data-filter="cron">Cron</button>
              <button class="filter-chip" type="button" data-filter="error">Error</button>
            </div>
            <div class="timeline-stream">{rows_html}</div>
          </section>
        """

    server_rows = []
    for label, key, detail in [
        ("CPU", "cpu_percent", f"{float(server.get('cpu_percent', 0.0) or 0.0):.0f}%"),
        (
            "RAM",
            "ram_percent",
            f"{int(server.get('ram_used_mb', 0) or 0)} / {int(server.get('ram_total_mb', 0) or 0)} MB",
        ),
        ("Disk", "disk_percent", f"{float(server.get('disk_percent', 0.0) or 0.0):.0f}%"),
    ]:
        value = float(server.get(key, 0.0) or 0.0)
        server_rows.append(
            f"""
            <div class="server-row">
              <div class="server-line">
                <span>{label}</span>
                <strong>{value:.0f}%</strong>
              </div>
              <div class="agent-bar">
                <div class="agent-bar-fill {_progress_class(value)}" style="width:{value:.1f}%"></div>
              </div>
              <div class="row-meta">{escape(detail)}</div>
            </div>
            """
        )

    top_agents_html = ""
    if top_agents:
        top_agents_html = (
            '<div class="subsection"><div class="subheading">🤖 TOP AGENTS</div><div class="mini-list">'
            + "".join(
                f'<div class="mini-row"><span>{escape(str(item["name"]))}</span><strong>{_format_number(item["runs"])} runs</strong></div>'
                f'<div class="row-meta">{_format_number(item["avg_ms"])} ms avg</div>'
                for item in top_agents[:6]
            )
            + "</div></div>"
        )

    right_html = f"""
      <section class="card">
        <div class="card-title">🖥 SERVER</div>
        {''.join(server_rows)}
        <div class="row-meta">Uptime {escape(str(server.get("uptime", "Unknown")))}</div>
        {top_agents_html}
      </section>
    """

    errors_html = ""
    if errors:
        errors_html = (
            '<section class="card errors-card"><div class="card-title">Recent Errors</div><div class="list-stack">'
            + "".join(
                f'<div class="error-row"><div class="error-time">{escape(str(item["time"]))}</div><div><div class="row-title">{escape(str(item["agent"]))}</div><div class="row-meta">{escape(str(item["message"]))}</div></div></div>'
                for item in errors
            )
            + "</div></section>"
        )

    cost_labels_json = json.dumps(cost_chart.get("labels", []), ensure_ascii=False)
    cost_values_json = json.dumps(cost_chart.get("values", []), ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Admin</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #000000;
      --card: #111111;
      --border: #222222;
      --text: #ffffff;
      --muted: #888888;
      --green: #00ff88;
      --yellow: #ffaa00;
      --red: #ff4444;
      --blue: #4488ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      background: #000;
      color: #fff;
      font-family: 'JetBrains Mono', monospace, sans-serif;
      margin: 0;
    }}
    a {{ color: inherit; text-decoration: none; }}
    button {{ font: inherit; }}
    .wrap {{ padding: 0 20px 24px; }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: #000;
      border-bottom: 1px solid #222;
      padding: 14px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .brand {{ display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }}
    .brand-title {{ font-size: 1.1rem; font-weight: 700; }}
    .top-chips, .top-nav {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    .chip, .nav-link, .refresh-link {{
      border: 1px solid #222;
      background: #111;
      color: #fff;
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 0.9rem;
    }}
    .nav-link.active {{ border-color: var(--blue); color: var(--blue); }}
    .refresh-link {{ margin-left: auto; }}
    .card {{
      background: #111;
      border: 1px solid #222;
      border-radius: 8px;
      padding: 16px;
    }}
    .stats-row {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 16px;
      margin: 20px 0;
    }}
    .stat-card {{ min-height: 128px; }}
    .stat-label {{
      color: var(--muted);
      font-size: 0.8rem;
      letter-spacing: 0.08em;
      margin-bottom: 14px;
    }}
    .stat-number {{
      font-size: 2rem;
      font-weight: bold;
      color: var(--green);
      margin-bottom: 8px;
    }}
    .stat-meta, .row-meta {{
      color: var(--muted);
      font-size: 0.82rem;
      line-height: 1.5;
    }}
    .main-grid {{
      display: grid;
      grid-template-columns: 35fr 40fr 25fr;
      gap: 16px;
      align-items: start;
    }}
    .column {{ display: grid; gap: 16px; min-width: 0; }}
    .card-title {{
      font-size: 1rem;
      font-weight: 700;
      margin-bottom: 6px;
    }}
    .card-subtitle {{
      color: var(--muted);
      margin-bottom: 14px;
      font-size: 0.85rem;
    }}
    .model-pills {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .model-pill-form {{ margin: 0; }}
    .model-pill {{
      border: 1px solid #222;
      background: #000;
      color: #fff;
      border-radius: 999px;
      padding: 7px 12px;
      cursor: pointer;
    }}
    .model-pill.active {{
      border-color: var(--green);
      color: var(--green);
    }}
    .model-pill:disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}
    .subsection {{
      margin-top: 16px;
      padding-top: 16px;
      border-top: 1px solid #222;
    }}
    .subheading {{
      font-size: 0.82rem;
      color: var(--muted);
      margin-bottom: 10px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .mini-list, .list-stack {{ display: grid; gap: 10px; }}
    .mini-row, .cost-row, .server-line, .timeline-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }}
    .row-title {{ font-weight: 700; font-size: 0.9rem; }}
    .row-cost {{ color: var(--green); font-weight: 700; }}
    .agent-bar {{
      height: 4px;
      background: #222;
      border-radius: 2px;
      overflow: hidden;
      margin-top: 6px;
    }}
    .agent-bar-fill {{
      height: 4px;
      background: var(--green);
      border-radius: 2px;
    }}
    .agent-bar-fill.warning {{ background: var(--yellow); }}
    .agent-bar-fill.danger {{ background: var(--red); }}
    .chart-block {{ margin-top: 16px; }}
    .timeline-card {{ min-height: 640px; }}
    .timeline-filters {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 12px 0 16px;
    }}
    .filter-chip {{
      border: 1px solid #222;
      background: #000;
      color: #fff;
      border-radius: 999px;
      padding: 7px 10px;
      cursor: pointer;
    }}
    .filter-chip.active {{
      border-color: var(--blue);
      color: var(--blue);
    }}
    .timeline-stream {{
      display: grid;
      gap: 10px;
      max-height: 760px;
      overflow: auto;
    }}
    .timeline-item {{
      display: grid;
      grid-template-columns: 56px minmax(0, 1fr);
      gap: 12px;
      padding: 12px 0;
      border-bottom: 1px solid #1a1a1a;
    }}
    .timeline-item:last-child {{ border-bottom: 0; }}
    .timeline-time {{ color: var(--muted); font-size: 0.82rem; }}
    .timeline-title {{ font-weight: 700; }}
    .timeline-message {{
      color: #d0d0d0;
      font-size: 0.88rem;
      line-height: 1.55;
      word-break: break-word;
    }}
    .timeline-tone {{
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--blue);
      flex: 0 0 auto;
    }}
    .timeline-tone.ok {{ background: var(--green); }}
    .timeline-tone.warning {{ background: var(--yellow); }}
    .timeline-tone.danger {{ background: var(--red); }}
    .timeline-tone.empty, .timeline-tone.unknown {{ background: #666; }}
    .errors-card {{ margin-top: 16px; }}
    .error-row {{
      display: grid;
      grid-template-columns: 56px minmax(0, 1fr);
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid #1a1a1a;
    }}
    .error-row:last-child {{ border-bottom: 0; }}
    .error-time {{ color: var(--red); font-size: 0.82rem; }}
    @media (max-width: 1100px) {{
      .main-grid {{ grid-template-columns: 1fr; }}
      .stats-row {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 700px) {{
      .wrap {{ padding: 0 14px 20px; }}
      .topbar {{ padding: 12px 14px; }}
      .stats-row {{ grid-template-columns: 1fr; }}
      .timeline-card {{ min-height: 0; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="brand-title">⚡ Ener-AI</div>
      <div class="top-chips">
        <div class="chip">Model: {escape(str(topbar.get("model", "Unknown")))}</div>
        <div class="chip">{escape(str(topbar.get("cost_today", "฿0.00")))}</div>
        <div class="chip">✅ {escape(str(topbar.get("health", "0/3 OK")))}</div>
        <div class="chip">{escape(str(topbar.get("time", "--:--")))}</div>
      </div>
    </div>
    <div class="top-nav">
      <a class="nav-link active" href="/admin">Overview</a>
      <a class="nav-link" href="/admin/metrics">Metrics</a>
      <a class="nav-link" href="/admin/logs">Logs</a>
      <a class="refresh-link" href="/admin">Refresh</a>
    </div>
  </div>

  <main class="wrap">
    <section class="stats-row">{stats_html}</section>

    <section class="main-grid">
      <div class="column">{left_cards_html}</div>
      <div class="column">{timeline_html}</div>
      <div class="column">{right_html}</div>
    </section>

    {errors_html}
  </main>

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

    const costCanvas = document.getElementById("cost7dChart");
    if (costCanvas) {{
      new Chart(costCanvas, {{
        type: "bar",
        data: {{
          labels: {cost_labels_json},
          datasets: [{{
            data: {cost_values_json},
            backgroundColor: "#00ff88",
            borderRadius: 4,
            borderSkipped: false,
          }}],
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{
            legend: {{ display: false }},
            tooltip: {{
              callbacks: {{
                label: (ctx) => `฿${{Number(ctx.raw || 0).toFixed(2)}}`,
              }},
            }},
          }},
          scales: {{
            x: {{
              grid: {{ display: false }},
              ticks: {{ color: "#888888" }},
              border: {{ color: "#222222" }},
            }},
            y: {{
              beginAtZero: true,
              grid: {{ color: "#1a1a1a" }},
              ticks: {{
                color: "#888888",
                callback: (value) => `฿${{Number(value).toFixed(0)}}`,
              }},
              border: {{ color: "#222222" }},
            }},
          }},
        }},
      }});
    }}
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


@app.get("/admin/api/agents")
async def admin_agents(request: Request):
    await _require_admin(request)
    return JSONResponse(await _load_agent_stats_payload())


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
