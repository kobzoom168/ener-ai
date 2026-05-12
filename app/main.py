import asyncio
import base64
import hashlib
import json
import random
import re
import secrets
import shutil
import subprocess
import time
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import httpx
import psutil
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from telegram import Update

from app.bot.router import build_application
from app.core.ai import get_active_model, get_model_availability, get_model_label
from app.core.agents import COMMAND_AGENT_MAP, SCHEDULER_AGENTS
from app.core.config import settings
from app.core.database import get_all_config, get_config, get_db, init_db, set_config
from app.core.terminal import handle_terminal_ws
from app.scheduler import build_scheduler

telegram_app = build_application()
scheduler = None
_BANGKOK = ZoneInfo("Asia/Bangkok")
_APP_STARTED_AT = datetime.now(_BANGKOK)
_LOG_DIR = Path("/var/log/ener-ai")
_DOCKER_CONTAINER_NAME = "ener-ai-ener-ai-1"
_ALLOWED_UPLOAD_PATHS = [
    Path("/root/ener-ai/data"),
    Path("/root/ener-ai/backups"),
    Path("/tmp"),
]
_TERMINAL_TOKEN_TTL_SECONDS = 1800
_terminal_tokens: dict[str, float] = {}
_admin_otp_lock = asyncio.Lock()
_terminal_otp_lock = asyncio.Lock()
_OTP_SEND_COOLDOWN = 10
_ADMIN_OTP_CODE_KEY = "admin_otp_code"
_ADMIN_OTP_EXPIRE_KEY = "admin_otp_expire"
_ADMIN_OTP_LAST_SENT_KEY = "admin_otp_last_sent"
_ADMIN_RESET_OTP_CODE_KEY = "admin_reset_otp_code"
_ADMIN_RESET_OTP_EXPIRE_KEY = "admin_reset_otp_expire"
_ADMIN_RESET_OTP_LAST_SENT_KEY = "admin_reset_otp_last_sent"
_ADMIN_PASSWORD_OVERRIDE_KEY = "admin_password_override"
_TERMINAL_OTP_CODE_KEY = "terminal_otp_code"
_TERMINAL_OTP_EXPIRE_KEY = "terminal_otp_expire"
_TERMINAL_OTP_LAST_SENT_KEY = "terminal_otp_last_sent"
_ADMIN_SESSION_PREFIX = "admin_session:"
OTP_EXPIRE = 300
SESSION_EXPIRE = 7200
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


def _admin_unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


async def _get_admin_password() -> str:
    values = await _get_memory_values([_ADMIN_PASSWORD_OVERRIDE_KEY])
    password = str(values.get(_ADMIN_PASSWORD_OVERRIDE_KEY, "")).strip()
    return password or settings.admin_password


async def _validate_admin_basic_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise _admin_unauthorized()
    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise _admin_unauthorized()
    current_password = await _get_admin_password()
    if not (
        secrets.compare_digest(username, "admin")
        and secrets.compare_digest(password, current_password)
    ):
        raise _admin_unauthorized()


def _generate_otp() -> str:
    return str(random.randint(100000, 999999))


def _generate_session_token() -> str:
    return hashlib.sha256(f"{time.time()}{random.random()}".encode()).hexdigest()[:48]


async def _send_otp_telegram(otp: str, title: str = "Ener-AI Admin OTP") -> None:
    msg = (
        f"🔐 {title}\n\n"
        f"รหัส: *{otp}*\n\n"
        "หมดอายุใน 5 นาที\n"
        "ห้ามบอกใคร"
    )
    await telegram_app.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=msg,
        parse_mode="Markdown",
    )


async def _get_memory_values(keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    placeholders = ", ".join("?" for _ in keys)
    async with get_db() as db:
        cursor = await db.execute(
            f"SELECT key, value FROM memories WHERE key IN ({placeholders})",
            tuple(keys),
        )
        rows = await cursor.fetchall()
    return {str(row["key"]): str(row["value"] or "") for row in rows}


async def _set_memory_values(values: dict[str, str], tag: str = "system") -> None:
    if not values:
        return
    async with get_db() as db:
        for key, value in values.items():
            await db.execute(
                """
                INSERT INTO memories (key, value, tag)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    tag = excluded.tag,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value, tag),
            )
        await db.commit()


async def _delete_memory_keys(keys: list[str]) -> None:
    if not keys:
        return
    placeholders = ", ".join("?" for _ in keys)
    async with get_db() as db:
        await db.execute(f"DELETE FROM memories WHERE key IN ({placeholders})", tuple(keys))
        await db.commit()


async def _get_admin_otp_state() -> dict[str, str]:
    return await _get_memory_values(
        [
            _ADMIN_OTP_CODE_KEY,
            _ADMIN_OTP_EXPIRE_KEY,
            _ADMIN_OTP_LAST_SENT_KEY,
        ]
    )


async def _store_admin_otp(otp: str, expires_at: float, sent_at: float) -> None:
    await _set_memory_values(
        {
            _ADMIN_OTP_CODE_KEY: otp,
            _ADMIN_OTP_EXPIRE_KEY: str(expires_at),
            _ADMIN_OTP_LAST_SENT_KEY: str(sent_at),
        },
        tag="admin_otp",
    )


async def _clear_admin_otp() -> None:
    await _delete_memory_keys([
        _ADMIN_OTP_CODE_KEY,
        _ADMIN_OTP_EXPIRE_KEY,
    ])


async def _get_admin_reset_otp_state() -> dict[str, str]:
    return await _get_memory_values(
        [
            _ADMIN_RESET_OTP_CODE_KEY,
            _ADMIN_RESET_OTP_EXPIRE_KEY,
            _ADMIN_RESET_OTP_LAST_SENT_KEY,
        ]
    )


async def _store_admin_reset_otp(otp: str, expires_at: float, sent_at: float) -> None:
    await _set_memory_values(
        {
            _ADMIN_RESET_OTP_CODE_KEY: otp,
            _ADMIN_RESET_OTP_EXPIRE_KEY: str(expires_at),
            _ADMIN_RESET_OTP_LAST_SENT_KEY: str(sent_at),
        },
        tag="admin_reset_otp",
    )


async def _clear_admin_reset_otp() -> None:
    await _delete_memory_keys([
        _ADMIN_RESET_OTP_CODE_KEY,
        _ADMIN_RESET_OTP_EXPIRE_KEY,
    ])


async def _set_admin_password(password: str) -> None:
    await _set_memory_values(
        {_ADMIN_PASSWORD_OVERRIDE_KEY: password},
        tag="admin_auth",
    )


async def _clear_all_admin_sessions() -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM memories WHERE key LIKE ?",
            (f"{_ADMIN_SESSION_PREFIX}%",),
        )
        await db.commit()


def _validate_new_admin_password(password: str, confirm_password: str) -> str:
    candidate = str(password or "").strip()
    confirm = str(confirm_password or "").strip()
    if len(candidate) < 8:
        return "รหัสผ่านใหม่ต้องยาวอย่างน้อย 8 ตัวอักษร"
    if len(candidate) > 128:
        return "รหัสผ่านใหม่ยาวเกินไป"
    if candidate != confirm:
        return "ยืนยันรหัสผ่านไม่ตรงกัน"
    return ""


async def _get_terminal_otp_state() -> dict[str, str]:
    return await _get_memory_values(
        [
            _TERMINAL_OTP_CODE_KEY,
            _TERMINAL_OTP_EXPIRE_KEY,
            _TERMINAL_OTP_LAST_SENT_KEY,
        ]
    )


async def _store_terminal_otp(otp: str, expires_at: float, sent_at: float) -> None:
    await _set_memory_values(
        {
            _TERMINAL_OTP_CODE_KEY: otp,
            _TERMINAL_OTP_EXPIRE_KEY: str(expires_at),
            _TERMINAL_OTP_LAST_SENT_KEY: str(sent_at),
        },
        tag="terminal_otp",
    )


async def _clear_terminal_otp() -> None:
    await _delete_memory_keys([
        _TERMINAL_OTP_CODE_KEY,
        _TERMINAL_OTP_EXPIRE_KEY,
    ])


async def _store_admin_session(token: str, expires_at: float) -> None:
    await _set_memory_values(
        {
            f"{_ADMIN_SESSION_PREFIX}{token}": str(expires_at),
        },
        tag="admin_session",
    )


async def _delete_admin_session(token: str) -> None:
    if not token:
        return
    await _delete_memory_keys([f"{_ADMIN_SESSION_PREFIX}{token}"])


async def _is_valid_session(request: Request) -> bool:
    token = request.cookies.get("admin_session", "")
    if not token:
        return False
    session_state = await _get_memory_values([f"{_ADMIN_SESSION_PREFIX}{token}"])
    raw_expiry = session_state.get(f"{_ADMIN_SESSION_PREFIX}{token}", "")
    if not raw_expiry:
        return False
    try:
        expires_at = float(raw_expiry or 0)
    except Exception:
        await _delete_admin_session(token)
        return False
    if time.time() > expires_at:
        await _delete_admin_session(token)
        return False
    return True


async def _require_admin(request: Request):
    if await _is_valid_session(request):
        return
    if request.url.path.startswith("/admin/api/"):
        raise HTTPException(status_code=401, detail="Session expired")
    await _validate_admin_basic_auth(request)
    raise HTTPException(status_code=307, detail="OTP Required", headers={"Location": "/admin/otp"})


async def _verify_admin_session(request: Request):
    if await _is_valid_session(request):
        return
    if request.method.upper() == "GET":
        raise HTTPException(status_code=307, detail="Session expired", headers={"Location": "/admin"})
    raise HTTPException(status_code=401, detail="Session expired")


def _resolve_upload_dir(raw_path: str) -> Path:
    try:
        target = Path(raw_path or "/root/ener-ai/data/").expanduser().resolve(strict=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Path ไม่ถูกต้อง") from exc

    for allowed_root in _ALLOWED_UPLOAD_PATHS:
        if target == allowed_root or allowed_root in target.parents:
            return target
    raise HTTPException(status_code=400, detail="Path ไม่อนุญาต")


def _prune_terminal_tokens(now: float | None = None) -> None:
    current = now if now is not None else time.time()
    expired = [
        token
        for token, issued_at in _terminal_tokens.items()
        if current - issued_at > _TERMINAL_TOKEN_TTL_SECONDS
    ]
    for token in expired:
        _terminal_tokens.pop(token, None)


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
        raw = str(value).strip()
        if raw.startswith("$"):
            number = float(raw[1:]) * 33
        elif raw.upper().endswith("USD"):
            number = float(raw[:-3].strip()) * 33
        else:
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

    stat_card_ids = ["stats-calls", "stats-cost", "stats-msgs", "stats-tasks"]
    stats_html = "".join(
        f"""
        <section class="card stat-card" data-card-id="{stat_card_ids[idx] if idx < len(stat_card_ids) else f'stats-{idx + 1}'}">
          <div class="stat-label">{escape(str(item.get("label", "")))}</div>
          <div class="stat-number">{escape(str(item.get("value", "0")))}</div>
          <div class="stat-meta">{escape(str(item.get("meta", "")))}</div>
        </section>
        """
        for idx, item in enumerate(stats)
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
      <section class="card" data-card-id="model">
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
          <section class="card chart-card" data-card-id="cost">
            <div class="card-title">💰 COST BREAKDOWN</div>
            <div class="list-stack">{breakdown_rows}</div>
            <div class="chart-block">
              <div class="subheading">7-Day Cost</div>
              <div style="height:120px; width:100%; position:relative;">
                <canvas id="costChart"></canvas>
              </div>
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
          <section class="card timeline-card" data-card-id="timeline">
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
      <section class="card" data-card-id="server">
        <div class="card-title">🖥 SERVER</div>
        {''.join(server_rows)}
        <div class="row-meta">Uptime {escape(str(server.get("uptime", "Unknown")))}</div>
        {top_agents_html}
      </section>
    """

    errors_html = ""
    if errors:
        errors_html = (
            '<section class="card errors-card" data-card-id="errors"><div class="card-title">Recent Errors</div><div class="list-stack">'
            + "".join(
                f'<div class="error-row"><div class="error-time">{escape(str(item["time"]))}</div><div><div class="row-title">{escape(str(item["agent"]))}</div><div class="row-meta">{escape(str(item["message"]))}</div></div></div>'
                for item in errors
            )
            + "</div></section>"
        )

    live_log_tail_html = """
    <div id="log-tail-widget" class="log-widget dashboard-card">
      <div id="log-drag-handle" class="log-header">
        <span>📋 LIVE LOGS</span>
        <div class="log-controls">
          <button onclick="changeFontSize(-1)" title="ตัวเล็กลง">A-</button>
          <button onclick="changeFontSize(1)" title="ตัวใหญ่ขึ้น">A+</button>
          <span class="log-status">● LIVE</span>
          <button onclick="toggleLog()" title="ย่อ/ขยาย">_</button>
        </div>
      </div>
      <div id="log-tail-content" class="log-content"></div>
      <div id="log-resize-handle" class="resize-handle">⠿</div>
    </div>
    """

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
    #auto-refresh-select {{
      border: 1px solid #222;
      background: #111;
      color: #00ff88;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 0.85rem;
      cursor: pointer;
      margin-left: auto;
    }}
    .card {{
      background: #111;
      border: 1px solid #222;
      border-radius: 8px;
      padding: 16px;
    }}
    .card[data-card-id] {{
      transition: box-shadow 0.15s ease, border-color 0.15s ease;
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
    .dashboard-container {{
      display: grid;
      grid-template-columns: 35fr 40fr 25fr;
      gap: 16px;
      align-items: start;
    }}
    .dashboard-container.layout-freeform {{
      display: block;
      position: relative;
      min-height: 720px;
    }}
    .dashboard-card {{
      min-width: 0;
      position: relative;
    }}
    .dashboard-card.column {{ display: grid; gap: 16px; }}
    .dashboard-card-stats {{ grid-column: 1 / -1; }}
    .dashboard-container.layout-freeform .dashboard-card:not(#log-tail-widget) {{
      position: absolute;
      margin: 0;
    }}
    .dashboard-container.layout-freeform .dashboard-card:not(#log-tail-widget) > .card,
    .dashboard-container.layout-freeform .dashboard-card:not(#log-tail-widget) > .stats-row {{
      width: 100%;
    }}
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
    .chart-card {{
      max-height: 200px;
      overflow: hidden;
    }}
    .timeline-card {{ min-height: 640px; }}
    .edit-btn {{
      border: 1px solid #ffaa00;
      background: #221800;
      color: #ffaa00;
      border-radius: 8px;
      padding: 8px 12px;
      cursor: pointer;
    }}
    .edit-bar {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      padding: 6px 16px;
      background: #1a1a00;
      border-bottom: 1px solid #ffaa00;
      color: #ffaa00;
      font-size: 12px;
    }}
    .edit-bar button {{
      background: #333;
      border: 1px solid #555;
      color: #fff;
      padding: 4px 10px;
      border-radius: 4px;
      cursor: pointer;
    }}
    .edit-control {{
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      color: #aaa;
    }}
    .edit-control input[type=color] {{
      width: 28px;
      height: 22px;
      border: 1px solid #444;
      border-radius: 4px;
      padding: 1px;
      cursor: pointer;
      background: none;
    }}
    .card-drag-handle {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      cursor: grab;
      color: #444;
      font-size: 11px;
      padding: 4px 6px 4px 8px;
      user-select: none;
      background: #0d0d0d;
      border-bottom: 1px solid #1a1a1a;
      border-radius: 6px 6px 0 0;
      margin: -4px -4px 6px -4px;
    }}
    .card-drag-handle:active {{ cursor: grabbing; }}
    .card-drag-handle .card-collapse-btn {{
      background: none;
      border: none;
      color: #555;
      cursor: pointer;
      font-size: 13px;
      padding: 0 4px;
      line-height: 1;
    }}
    .card-drag-handle .card-collapse-btn:hover {{ color: #aaa; }}
    .card-resize-handle {{
      display: block;
      position: absolute;
      bottom: 2px;
      right: 2px;
      cursor: se-resize;
      color: #2a2a2a;
      font-size: 14px;
      user-select: none;
    }}
    .card-resize-handle:hover {{ color: #555; }}
    .dashboard-card.draggable {{
      border: 1px dashed #333 !important;
      cursor: default;
      padding: 4px;
      background: rgba(17, 17, 17, 0.25);
    }}
    .dashboard-card.draggable .card-drag-handle {{ display: flex; }}
    .dashboard-card.draggable .card-resize-handle {{ display: block; }}
    .card-selected {{
      border-color: var(--green) !important;
      box-shadow: 0 0 0 1px rgba(0, 255, 136, 0.7), 0 0 18px rgba(0, 255, 136, 0.15);
    }}
    .toast {{
      position: fixed;
      bottom: 80px;
      right: 20px;
      background: #111;
      border: 1px solid #00ff88;
      color: #00ff88;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 12px;
      z-index: 9999;
    }}
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
    .log-widget {{
      position: fixed;
      bottom: 20px;
      right: 20px;
      width: 500px;
      min-width: 250px;
      min-height: 80px;
      background: #0a0a0a;
      border: 1px solid #333;
      border-radius: 8px;
      z-index: 999;
      box-shadow: 0 4px 20px rgba(0,255,136,0.1);
      overflow: hidden;
    }}
    .log-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 10px;
      background: #111;
      border-bottom: 1px solid #222;
      cursor: grab;
      user-select: none;
      font-size: 11px;
      color: #888;
    }}
    .log-header:active {{ cursor: grabbing; }}
    .log-controls {{
      display: flex;
      gap: 6px;
      align-items: center;
    }}
    .log-controls button {{
      background: #222;
      border: 1px solid #333;
      color: #888;
      padding: 2px 6px;
      border-radius: 4px;
      cursor: pointer;
      font-size: 10px;
    }}
    .log-controls button:hover {{ background: #333; color: #fff; }}
    .log-status {{ color: #00ff88; font-size: 10px; }}
    .log-content {{
      height: 120px;
      overflow-y: auto;
      padding: 8px 10px;
      font-family: monospace;
      font-size: 11px;
      line-height: 1.6;
    }}
    .resize-handle {{
      position: absolute;
      bottom: 0;
      right: 0;
      width: 16px;
      height: 16px;
      cursor: se-resize;
      color: #333;
      font-size: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .log-line-error {{ color: #ff4444; }}
    .log-line-warn {{ color: #ffaa00; }}
    .log-line-info {{ color: #888; }}
    .log-line-ok {{ color: #00ff88; }}
    @media (max-width: 1100px) {{
      .dashboard-container {{ grid-template-columns: 1fr; }}
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
      <a class="nav-link" href="/admin/pipeline">⚡ Pipeline</a>
      <a class="nav-link" href="/admin/logs">Logs</a>
      <a class="nav-link" href="/admin/config">⚙️ Config</a>
      <a class="nav-link" href="/admin/routing">🔀 Routing</a>
      <a class="nav-link" href="/admin/api-status">📡 API Status</a>
      <a class="nav-link" href="/admin/terminal" target="_blank" rel="noopener noreferrer">💻 Terminal</a>
      <button class="edit-btn" type="button" onclick="fetch('/admin/logout',{{method:'POST'}}).then(() => location.reload())">🚪 Logout</button>
      <button id="edit-btn" class="edit-btn" type="button" onclick="enterEditMode()">✏️ Edit</button>
      <select id="auto-refresh-select" title="Auto Refresh">
        <option value="0">⟳ Off</option>
        <option value="15000">⟳ 15s</option>
        <option value="30000" selected>⟳ 30s</option>
        <option value="60000">⟳ 1m</option>
        <option value="300000">⟳ 5m</option>
      </select>
    </div>
  </div>

  <div id="edit-bar" class="edit-bar" style="display:none">
    <span>✏️ Edit Mode</span>
    <div class="edit-control">
      <span>ตัวอักษร:</span>
      <button type="button" onclick="globalFontSize(-1)">A-</button>
      <span id="font-size-display">12px</span>
      <button type="button" onclick="globalFontSize(1)">A+</button>
    </div>
    <div class="edit-control">
      <span>สีข้อความ:</span>
      <input type="color" id="text-color-pick" value="#ffffff" oninput="applyTextColor(this.value)">
    </div>
    <div class="edit-control">
      <span>สี accent:</span>
      <input type="color" id="accent-color-pick" value="#00ff88" oninput="applyAccentColor(this.value)">
    </div>
    <div class="edit-control">
      <span>สี card:</span>
      <input type="color" id="card-bg-pick" value="#111111" oninput="applyCardBg(this.value)">
    </div>
    <div class="edit-control">
      <span>Preset:</span>
      <button type="button" onclick="applyPreset('dark')">🌑 Dark</button>
      <button type="button" onclick="applyPreset('green')">💚 Green</button>
      <button type="button" onclick="applyPreset('blue')">💙 Blue</button>
      <button type="button" onclick="applyPreset('amber')">🟡 Amber</button>
    </div>
    <button type="button" onclick="saveLayout()">💾 Save</button>
    <button type="button" onclick="resetLayout()">↺ Reset</button>
    <button type="button" onclick="exitEditMode()">🔒 Lock</button>
  </div>
  <div id="card-toolbar" style="display:none;position:fixed;z-index:9999;
    background:#1a1a1a;border:1px solid #444;border-radius:8px;
    padding:8px 14px;gap:10px;align-items:center;font-size:12px;
    color:#fff;flex-wrap:wrap;box-shadow:0 4px 16px #0008">
    <span id="card-toolbar-label" style="color:#00ff88;font-weight:bold;margin-right:4px"></span>
    <button type="button" onclick="cardFs(-1)">A-</button>
    <span id="card-fs-display" style="min-width:32px;text-align:center">12px</span>
    <button type="button" onclick="cardFs(1)">A+</button>
    <span style="color:#444">|</span>
    <label style="display:flex;align-items:center;gap:4px">
      ข้อความ<input type="color" id="card-text-pick" style="width:28px;height:22px;border:none;background:none;cursor:pointer" oninput="cardColor(this.value)">
    </label>
    <label style="display:flex;align-items:center;gap:4px">
      พื้นหลัง<input type="color" id="card-box-bg-pick" style="width:28px;height:22px;border:none;background:none;cursor:pointer" oninput="cardBg(this.value)">
    </label>
    <button type="button" onclick="resetCard()" style="color:#ff6b6b">↺ Reset</button>
    <button type="button" onclick="closeCardToolbar()" style="color:#888">✕</button>
  </div>

  <main class="wrap">
    <div id="dashboard-container" class="dashboard-container">
      <section id="card-stats" class="dashboard-card dashboard-card-stats">
        <div class="stats-row">{stats_html}</div>
      </section>
      <div id="card-model" class="dashboard-card column">{left_cards_html}</div>
      <div id="card-timeline" class="dashboard-card column">{timeline_html}</div>
      <div id="card-server" class="dashboard-card column">{right_html}</div>
    </div>

    {errors_html}

    <div id="api-status-widget" style="position:fixed;bottom:20px;left:20px;width:560px;min-width:280px;
         background:#0a0a0a;border:1px solid #333;border-radius:8px;z-index:998;
         box-shadow:0 4px 20px rgba(0,136,255,0.08);overflow:hidden">
      <div id="api-status-drag-handle" style="display:flex;justify-content:space-between;align-items:center;
           padding:6px 10px;background:#111;border-bottom:1px solid #222;
           cursor:grab;user-select:none;font-size:11px;color:#888">
        <span>📡 API Status</span>
        <div style="display:flex;gap:6px;align-items:center">
          <span id="api-status-time" style="font-size:10px;color:#555"></span>
          <button onclick="refreshApiStatus()" style="background:#222;border:1px solid #333;color:#888;
                  padding:2px 6px;border-radius:4px;cursor:pointer;font-size:10px">↻</button>
          <button onclick="toggleApiStatus()" id="api-status-toggle-btn" style="background:#222;border:1px solid #333;
                  color:#888;padding:2px 6px;border-radius:4px;cursor:pointer;font-size:10px">_</button>
        </div>
      </div>
      <div id="api-status-body" style="overflow:hidden">
        <div id="api-status-grid" style="display:flex;flex-direction:row;flex-wrap:nowrap;gap:10px;
             padding:12px;overflow-x:auto;-webkit-overflow-scrolling:touch;
             scrollbar-width:thin;scrollbar-color:#444 #111">
          <span style="color:#555;font-size:0.85rem">Loading...</span>
        </div>
      </div>
      <div id="api-status-resize" style="text-align:right;padding:0 4px 2px;color:#444;font-size:10px;cursor:se-resize;user-select:none">⠿</div>
    </div>
  </main>
  {live_log_tail_html}

  <script>
    function escapeHtml(text) {{
      return String(text ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    const LAYOUT_KEY = 'ener-admin-layout-v1';
    const STYLE_KEY = 'ener-admin-style-v1';
    const PRESETS = {{
      dark: {{ text: '#ffffff', accent: '#00ff88', cardBg: '#111111' }},
      green: {{ text: '#ccffcc', accent: '#00ff88', cardBg: '#0a1a0a' }},
      blue: {{ text: '#cce0ff', accent: '#4488ff', cardBg: '#0a0a1a' }},
      amber: {{ text: '#fff8cc', accent: '#ffaa00', cardBg: '#1a1500' }},
    }};
    let editMode = false;
    let _selectedCard = null;
    const CARD_STYLE_KEY = 'ener_card_styles_v1';
    function _getCardStyles() {{
      try {{ return JSON.parse(localStorage.getItem(CARD_STYLE_KEY) || '{{}}'); }}
      catch {{ return {{}}; }}
    }}
    function _saveCardStyle(id, patch) {{
      const all = _getCardStyles();
      all[id] = Object.assign(all[id] || {{}}, patch);
      localStorage.setItem(CARD_STYLE_KEY, JSON.stringify(all));
    }}
    function _applyCardStyle(card, s) {{
      if (s.fontSize) card.style.fontSize = s.fontSize + 'px';
      if (s.color) card.style.color = s.color;
      if (s.background) {{
        card.style.background = s.background;
        card.querySelectorAll('.card').forEach(el => el.style.background = s.background);
      }}
    }}
    const dashboardContainer = document.getElementById('dashboard-container');
    const editBar = document.getElementById('edit-bar');
    const editButton = document.getElementById('edit-btn');
    const fontSizeDisplay = document.getElementById('font-size-display');
    const textColorPicker = document.getElementById('text-color-pick');
    const accentColorPicker = document.getElementById('accent-color-pick');
    const cardBgPicker = document.getElementById('card-bg-pick');
    const cardToolbar = document.getElementById('card-toolbar');
    const widget = document.getElementById('log-tail-widget');
    const handle = document.getElementById('log-drag-handle');
    const content = document.getElementById('log-tail-content');
    const resizeHandle = document.getElementById('log-resize-handle');
    const controlButtons = document.querySelectorAll('.log-controls button');
    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let startLeft = 0;
    let startTop = 0;
    let isResizing = false;
    let resizeStartX = 0;
    let resizeStartY = 0;
    let resizeStartW = 0;
    let resizeStartH = 0;
    let fontSize = 11;
    let currentFontSize = 12;
    let collapsed = false;
    let expandedHeight = '160px';

    function readSavedLayout() {{
      try {{
        return JSON.parse(localStorage.getItem(LAYOUT_KEY) || '{{}}');
      }} catch (error) {{
        return {{}};
      }}
    }}

    function writeSavedLayout(layout) {{
      localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    }}

    function updateSavedLayoutForCard(cardId, nextState) {{
      const layout = readSavedLayout();
      layout[cardId] = {{
        ...(layout[cardId] || {{}}),
        ...nextState,
      }};
      writeSavedLayout(layout);
    }}

    function hasSavedDashboardCards(layout) {{
      return Object.keys(layout).some((id) => id !== 'log-tail-widget');
    }}

    function showToast(message) {{
      const toast = document.createElement('div');
      toast.className = 'toast';
      toast.textContent = message;
      document.body.appendChild(toast);
      setTimeout(() => toast.remove(), 2000);
    }}

    function getDashboardStyleTargets() {{
      return document.querySelectorAll('.dashboard-card, .dashboard-card .card, .card[data-card-id], .log-widget');
    }}

    function getAccentTargets() {{
      return document.querySelectorAll('.stat-number, .row-cost, .log-status, .agent-bar-fill');
    }}

    function _loadSavedCardStyles() {{
      const all = _getCardStyles();
      Object.entries(all).forEach(([id, s]) => {{
        const card = document.getElementById(id);
        if (card) _applyCardStyle(card, s);
      }});
    }}

    function saveStyle() {{
      const style = {{
        fontSize: currentFontSize,
        textColor: textColorPicker?.value || '#ffffff',
        accentColor: accentColorPicker?.value || '#00ff88',
        cardBg: cardBgPicker?.value || '#111111',
      }};
      localStorage.setItem(STYLE_KEY, JSON.stringify(style));
    }}

    function globalFontSize(delta, shouldPersist = true) {{
      currentFontSize = Math.max(9, Math.min(20, currentFontSize + delta));
      getDashboardStyleTargets().forEach((el) => {{
        el.style.fontSize = `${{currentFontSize}}px`;
      }});
      if (fontSizeDisplay) fontSizeDisplay.textContent = `${{currentFontSize}}px`;
      if (content) content.style.fontSize = `${{currentFontSize}}px`;
      fontSize = currentFontSize;
      if (shouldPersist) saveStyle();
      _loadSavedCardStyles();
    }}

    function applyTextColor(color, shouldPersist = true) {{
      getDashboardStyleTargets().forEach((el) => {{
        el.style.color = color;
      }});
      document.querySelectorAll('.row-meta, .stat-meta, .timeline-message, .timeline-time, .card-subtitle, .subheading').forEach((el) => {{
        el.style.color = color;
      }});
      if (textColorPicker && textColorPicker.value !== color) textColorPicker.value = color;
      if (shouldPersist) saveStyle();
      _loadSavedCardStyles();
    }}

    function applyAccentColor(color, shouldPersist = true) {{
      document.documentElement.style.setProperty('--green', color);
      getAccentTargets().forEach((el) => {{
        if (el.classList.contains('agent-bar-fill')) {{
          if (!el.classList.contains('warning') && !el.classList.contains('danger')) {{
            el.style.background = color;
          }}
        }} else {{
          el.style.color = color;
        }}
      }});
      if (accentColorPicker && accentColorPicker.value !== color) accentColorPicker.value = color;
      if (shouldPersist) saveStyle();
    }}

    function applyCardBg(color, shouldPersist = true) {{
      document.querySelectorAll('.dashboard-card, .dashboard-card .card, .card[data-card-id]').forEach((el) => {{
        el.style.background = color;
      }});
      if (widget) widget.style.background = color;
      if (cardBgPicker && cardBgPicker.value !== color) cardBgPicker.value = color;
      if (shouldPersist) saveStyle();
      _loadSavedCardStyles();
    }}

    function applyPreset(name) {{
      const preset = PRESETS[name];
      if (!preset) return;
      applyTextColor(preset.text, false);
      applyAccentColor(preset.accent, false);
      applyCardBg(preset.cardBg, false);
      saveStyle();
    }}

    function loadStyle() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(STYLE_KEY) || 'null');
        if (!saved) return;
        if (saved.fontSize) {{
          currentFontSize = Number(saved.fontSize) || 12;
          globalFontSize(0, false);
        }} else if (fontSizeDisplay) {{
          fontSizeDisplay.textContent = `${{currentFontSize}}px`;
        }}
        if (saved.textColor) applyTextColor(saved.textColor, false);
        if (saved.accentColor) applyAccentColor(saved.accentColor, false);
        if (saved.cardBg) applyCardBg(saved.cardBg, false);
      }} catch (error) {{
        // ignore corrupted saved style
      }}
    }}

    function updateDashboardContainerHeight() {{
      if (!dashboardContainer || !dashboardContainer.classList.contains('layout-freeform')) return;
      let maxBottom = 0;
      document.querySelectorAll('.dashboard-card:not(#log-tail-widget)').forEach((card) => {{
        const top = parseFloat(card.style.top || '0');
        maxBottom = Math.max(maxBottom, top + card.offsetHeight);
      }});
      dashboardContainer.style.height = `${{Math.max(720, maxBottom + 24)}}px`;
    }}

    function snapshotDashboardCards() {{
      if (!dashboardContainer || dashboardContainer.classList.contains('layout-freeform')) return;
      const parentRect = dashboardContainer.getBoundingClientRect();
      const cards = Array.from(document.querySelectorAll('.dashboard-card:not(#log-tail-widget)')).map((card) => ({{
        card,
        rect: card.getBoundingClientRect(),
      }}));

      dashboardContainer.classList.add('layout-freeform');
      cards.forEach(({{ card, rect }}) => {{
        card.style.position = 'absolute';
        card.style.left = `${{rect.left - parentRect.left}}px`;
        card.style.top = `${{rect.top - parentRect.top}}px`;
        card.style.width = `${{rect.width}}px`;
        card.style.height = `${{rect.height}}px`;
      }});
      updateDashboardContainerHeight();
    }}

    function makeDraggable(el, dragHandle) {{
      let dragActive = false;
      let dragStartX = 0;
      let dragStartY = 0;
      let dragLeft = 0;
      let dragTop = 0;

      dragHandle.addEventListener('mousedown', (event) => {{
        if (event.target.closest('button')) return;
        if (!dashboardContainer) return;
        if (!dashboardContainer.classList.contains('layout-freeform')) {{
          snapshotDashboardCards();
        }}
        dragActive = true;
        dragStartX = event.clientX;
        dragStartY = event.clientY;
        const rect = el.getBoundingClientRect();
        const parentRect = dashboardContainer.getBoundingClientRect();
        dragLeft = rect.left - parentRect.left;
        dragTop = rect.top - parentRect.top;
        el.style.position = 'absolute';
        event.preventDefault();
      }});

      document.addEventListener('mousemove', (event) => {{
        if (!dragActive) return;
        el.style.left = `${{dragLeft + event.clientX - dragStartX}}px`;
        el.style.top = `${{dragTop + event.clientY - dragStartY}}px`;
        updateDashboardContainerHeight();
      }});

      document.addEventListener('mouseup', () => {{
        if (dragActive) _autoSaveLayout();
        dragActive = false;
      }});
    }}

    function makeResizable(el, resizeGrip) {{
      let resizeActive = false;
      let startResizeX = 0;
      let startResizeY = 0;
      let startWidth = 0;
      let startHeight = 0;

      resizeGrip.addEventListener('mousedown', (event) => {{
        resizeActive = true;
        startResizeX = event.clientX;
        startResizeY = event.clientY;
        startWidth = el.offsetWidth;
        startHeight = el.offsetHeight;
        event.preventDefault();
        event.stopPropagation();
      }});

      document.addEventListener('mousemove', (event) => {{
        if (!resizeActive) return;
        el.style.width = `${{Math.max(200, startWidth + event.clientX - startResizeX)}}px`;
        el.style.height = `${{Math.max(100, startHeight + event.clientY - startResizeY)}}px`;
        updateDashboardContainerHeight();
      }});

      document.addEventListener('mouseup', () => {{
        if (resizeActive) _autoSaveLayout();
        resizeActive = false;
      }});
    }}

    function _autoSaveLayout() {{
      const layout = readSavedLayout();
      document.querySelectorAll('.dashboard-card[id]').forEach((card) => {{
        layout[card.id] = {{
          left: card.style.left || '',
          top: card.style.top || '',
          width: card.style.width || '',
          height: card.style.height || '',
          collapsed: card.dataset.collapsed || '0',
        }};
      }});
      writeSavedLayout(layout);
    }}

    function toggleCard(cardId) {{
      const card = document.getElementById(cardId);
      if (!card) return;
      const isCollapsed = card.dataset.collapsed === '1';
      const btn = card.querySelector('.card-collapse-btn');
      Array.from(card.children).forEach(child => {{
        if (child.classList.contains('card-drag-handle') ||
            child.classList.contains('card-resize-handle')) return;
        child.style.display = isCollapsed ? '' : 'none';
      }});
      card.dataset.collapsed = isCollapsed ? '0' : '1';
      if (btn) btn.textContent = isCollapsed ? '−' : '▲';
      _autoSaveLayout();
    }}
    window.toggleCard = toggleCard;

    function initWidgets() {{
      const SKIP = new Set(['log-tail-widget', 'api-status-widget']);
      document.querySelectorAll('.dashboard-card[id]').forEach((card) => {{
        if (SKIP.has(card.id)) return;
        card.style.position = 'relative';

        if (!card.querySelector('.card-drag-handle')) {{
          const h = document.createElement('div');
          h.className = 'card-drag-handle';
          h.innerHTML = `<span>⠿ ${{card.id.replace('card-','')}}</span>
            <button class="card-collapse-btn" onclick="toggleCard('${{card.id}}')">−</button>`;
          card.prepend(h);
          makeDraggable(card, h);
        }}
        if (!card.querySelector('.card-resize-handle')) {{
          const r = document.createElement('div');
          r.className = 'card-resize-handle';
          r.innerHTML = '⠿';
          card.appendChild(r);
          makeResizable(card, r);
        }}
      }});

      // Restore collapsed state from saved layout
      const layout = readSavedLayout();
      Object.entries(layout).forEach(([id, pos]) => {{
        if (pos && pos.collapsed === '1') {{
          const card = document.getElementById(id);
          if (card && !['log-tail-widget','api-status-widget'].includes(id)) {{
            card.dataset.collapsed = '0';
            toggleCard(id);
          }}
        }}
      }});
    }}

    function enterEditMode() {{
      editMode = true;
      if (editBar) editBar.style.display = 'flex';
      if (editButton) editButton.style.display = 'none';
      snapshotDashboardCards();

      document.querySelectorAll('.dashboard-card').forEach((card) => {{
        card.classList.add('draggable');
        if (card.id === 'log-tail-widget') return;

        if (!card.querySelector('.card-drag-handle')) {{
          const dragHandle = document.createElement('div');
          dragHandle.className = 'card-drag-handle';
          dragHandle.innerHTML = '⠿ drag';
          card.prepend(dragHandle);
          makeDraggable(card, dragHandle);
        }}

        if (!card.querySelector('.card-resize-handle')) {{
          const resizeGrip = document.createElement('div');
          resizeGrip.className = 'card-resize-handle';
          resizeGrip.innerHTML = '⠿';
          card.appendChild(resizeGrip);
          makeResizable(card, resizeGrip);
        }}
        if (card.id !== 'log-tail-widget') {{
          card.addEventListener('click', _onCardClick);
        }}
      }});

      updateDashboardContainerHeight();
    }}

    function exitEditMode() {{
      editMode = false;
      if (editBar) editBar.style.display = 'none';
      if (editButton) editButton.style.display = 'block';
      closeCardToolbar();
      document.querySelectorAll('.dashboard-card').forEach((card) => {{
        card.removeEventListener('click', _onCardClick);
        card.classList.remove('draggable');
      }});
    }}

    function _onCardClick(e) {{
      if (!editMode) return;
      if (e.target.closest('#card-toolbar,.card-drag-handle,.card-resize-handle')) return;
      e.stopPropagation();
      _selectedCard = e.currentTarget;
      const toolbar = document.getElementById('card-toolbar');
      const rect = _selectedCard.getBoundingClientRect();
      toolbar.style.display = 'flex';
      toolbar.style.top = (rect.top + 6) + 'px';
      toolbar.style.left = (rect.left + 6) + 'px';
      const LABELS = {{
        'card-stats':'Stats', 'card-model':'Model',
        'card-timeline':'Timeline', 'card-server':'Server'
      }};
      document.getElementById('card-toolbar-label').textContent =
        '✏️ ' + (LABELS[_selectedCard.id] || _selectedCard.id);
      const saved = (_getCardStyles()[_selectedCard.id] || {{}});
      const fs = parseInt(_selectedCard.style.fontSize) || 12;
      document.getElementById('card-fs-display').textContent = fs + 'px';
      document.getElementById('card-text-pick').value = saved.color || '#ffffff';
      document.getElementById('card-box-bg-pick').value = saved.background || '#111111';
    }}

    function closeCardToolbar() {{
      if (cardToolbar) cardToolbar.style.display = 'none';
      _selectedCard = null;
    }}

    function cardFs(delta) {{
      if (!_selectedCard) return;
      const cur = parseInt(_selectedCard.style.fontSize) || 12;
      const next = Math.max(9, Math.min(22, cur + delta));
      _selectedCard.style.fontSize = next + 'px';
      document.getElementById('card-fs-display').textContent = next + 'px';
      _saveCardStyle(_selectedCard.id, {{ fontSize: next }});
    }}

    function cardColor(color) {{
      if (!_selectedCard) return;
      _selectedCard.style.color = color;
      _saveCardStyle(_selectedCard.id, {{ color }});
    }}

    function cardBg(color) {{
      if (!_selectedCard) return;
      _selectedCard.style.background = color;
      _selectedCard.querySelectorAll('.card').forEach(el => el.style.background = color);
      _saveCardStyle(_selectedCard.id, {{ background: color }});
    }}

    function resetCard() {{
      if (!_selectedCard) return;
      _selectedCard.style.fontSize = '';
      _selectedCard.style.color = '';
      _selectedCard.style.background = '';
      _selectedCard.querySelectorAll('.card').forEach(el => el.style.background = '');
      const all = _getCardStyles();
      delete all[_selectedCard.id];
      localStorage.setItem(CARD_STYLE_KEY, JSON.stringify(all));
      loadStyle();
      _loadSavedCardStyles();
      closeCardToolbar();
    }}

    function saveLayout() {{
      const layout = readSavedLayout();
      document.querySelectorAll('.dashboard-card[id]').forEach((card) => {{
        layout[card.id] = {{
          left: card.style.left || '',
          top: card.style.top || '',
          width: card.style.width || '',
          height: card.style.height || '',
        }};
      }});
      writeSavedLayout(layout);
      saveStyle();
      exitEditMode();
      showToast('💾 บันทึก layout แล้ว');
    }}

    function resetLayout() {{
      localStorage.removeItem(LAYOUT_KEY);
      localStorage.removeItem(STYLE_KEY);
      localStorage.removeItem(CARD_STYLE_KEY);
      localStorage.removeItem('log-pos');
      localStorage.removeItem('log-font-size');
      localStorage.removeItem('log-widget-collapsed');
      localStorage.removeItem('api-status-pos-v1');
      location.reload();
    }}

    function loadLayout() {{
      const layout = readSavedLayout();
      if (!Object.keys(layout).length) return;

      if (hasSavedDashboardCards(layout) && dashboardContainer) {{
        dashboardContainer.classList.add('layout-freeform');
      }}

      Object.entries(layout).forEach(([id, pos]) => {{
        const card = document.getElementById(id);
        if (!card || !pos) return;

        if (id === 'log-tail-widget') {{
          if (pos.left || pos.top) {{
            card.style.right = 'auto';
            card.style.bottom = 'auto';
          }}
          if (pos.left) card.style.left = pos.left;
          if (pos.top) card.style.top = pos.top;
          if (pos.width) card.style.width = pos.width;
          if (pos.height) {{
            card.style.height = pos.height;
            expandedHeight = pos.height;
          }}
          return;
        }}

        if (!dashboardContainer || !dashboardContainer.classList.contains('layout-freeform')) return;
        card.style.position = 'absolute';
        if (pos.left) card.style.left = pos.left;
        if (pos.top) card.style.top = pos.top;
        if (pos.width) card.style.width = pos.width;
        if (pos.height) card.style.height = pos.height;
      }});

      updateDashboardContainerHeight();
    }}

    window.enterEditMode = enterEditMode;
    window.exitEditMode = exitEditMode;
    window.saveLayout = saveLayout;
    window.resetLayout = resetLayout;
    window.globalFontSize = globalFontSize;
    window.applyTextColor = applyTextColor;
    window.applyAccentColor = applyAccentColor;
    window.applyCardBg = applyCardBg;
    window.applyPreset = applyPreset;
    window.cardFs = cardFs;
    window.cardColor = cardColor;
    window.cardBg = cardBg;
    window.resetCard = resetCard;
    window.closeCardToolbar = closeCardToolbar;

    function updateLogContentHeight(totalHeight) {{
      if (!widget || !content) return;
      const header = handle ? handle.offsetHeight : 40;
      const resizeGrip = 16;
      const minimumContentHeight = collapsed ? 0 : 40;
      const nextHeight = Math.max(minimumContentHeight, totalHeight - header - resizeGrip);
      content.style.height = collapsed ? '0px' : `${{nextHeight}}px`;
    }}

    function persistWidgetState() {{
      if (!widget) return;
      updateSavedLayoutForCard('log-tail-widget', {{
        left: widget.style.left || '',
        top: widget.style.top || '',
        width: widget.style.width || '',
        height: widget.style.height || '',
      }});
      localStorage.setItem('log-widget-collapsed', collapsed ? '1' : '0');
    }}

    function changeFontSize(delta) {{
      globalFontSize(delta);
    }}

    function toggleLog() {{
      if (!content || !widget) return;
      collapsed = !collapsed;
      if (collapsed) {{
        expandedHeight = widget.style.height || `${{widget.offsetHeight}}px` || expandedHeight;
        content.style.display = 'none';
        widget.style.minHeight = '0px';
        widget.style.height = `${{(handle ? handle.offsetHeight : 32) + 16}}px`;
      }} else {{
        content.style.display = 'block';
        widget.style.minHeight = '80px';
        widget.style.height = expandedHeight || '160px';
        updateLogContentHeight(widget.offsetHeight);
      }}
      persistWidgetState();
    }}

    window.changeFontSize = changeFontSize;
    window.toggleLog = toggleLog;

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

    const costCanvas = document.getElementById("costChart");
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
          }},
          scales: {{
            x: {{
              grid: {{ display: false }},
              ticks: {{ color: "#888", font: {{ size: 10 }} }},
              border: {{ color: "#222" }},
            }},
            y: {{
              beginAtZero: true,
              grid: {{ color: "#222" }},
              ticks: {{
                color: "#888",
                font: {{ size: 10 }},
              }},
              border: {{ color: "#222" }},
            }},
          }},
        }},
      }});
    }}

    controlButtons.forEach((button) => {{
      button.addEventListener('mousedown', (event) => event.stopPropagation());
    }});

    if (handle && widget) {{
      handle.addEventListener('mousedown', (event) => {{
        if (event.target.closest('button')) return;
        isDragging = true;
        startX = event.clientX;
        startY = event.clientY;
        const rect = widget.getBoundingClientRect();
        startLeft = rect.left;
        startTop = rect.top;
        widget.style.right = 'auto';
        widget.style.bottom = 'auto';
        event.preventDefault();
      }});
    }}

    if (resizeHandle && widget) {{
      resizeHandle.addEventListener('mousedown', (event) => {{
        isResizing = true;
        resizeStartX = event.clientX;
        resizeStartY = event.clientY;
        resizeStartW = widget.offsetWidth;
        resizeStartH = widget.offsetHeight;
        event.preventDefault();
        event.stopPropagation();
      }});
    }}

    document.addEventListener('mousemove', (event) => {{
      if (isDragging && widget) {{
        widget.style.left = `${{startLeft + event.clientX - startX}}px`;
        widget.style.top = `${{startTop + event.clientY - startY}}px`;
      }}
      if (isResizing && widget) {{
        const newW = Math.max(250, resizeStartW + event.clientX - resizeStartX);
        const newH = Math.max(80, resizeStartH + event.clientY - resizeStartY);
        widget.style.width = `${{newW}}px`;
        widget.style.height = `${{newH}}px`;
        updateLogContentHeight(newH);
      }}
    }});

    document.addEventListener('mouseup', () => {{
      if (isDragging || isResizing) persistWidgetState();
      isDragging = false;
      isResizing = false;
    }});

    initWidgets();
    loadLayout();
    loadStyle();
    (function loadCardStyles() {{
      const all = _getCardStyles();
      Object.entries(all).forEach(([id, s]) => {{
        const card = document.getElementById(id);
        if (card) _applyCardStyle(card, s);
      }});
    }})();

    collapsed = localStorage.getItem('log-widget-collapsed') === '1';
    if (collapsed && content && widget) {{
      content.style.display = 'none';
      widget.style.minHeight = '0px';
      widget.style.height = `${{(handle ? handle.offsetHeight : 32) + 16}}px`;
    }}

    if (widget) {{
      if (!widget.style.height) widget.style.height = '160px';
      updateLogContentHeight(widget.offsetHeight);
    }}

    async function fetchLogs() {{
      try {{
        const res = await fetch('/admin/api/logs?filter=ALL&lines=15', {{ cache: "no-store" }});
        if (!res.ok) return;
        const data = await res.json();
        if (!content) return;

        const entries = Array.isArray(data.logs)
          ? data.logs.slice(-15)
          : Array.isArray(data.lines)
            ? data.lines.slice(-15).map((line) => `[${{line.time}}] ${{line.level}} ${{line.message}}`)
            : [];

        content.innerHTML = entries.map((line) => {{
          let cls = 'log-line-info';
          const lowered = String(line).toLowerCase();
          if (lowered.includes('error')) cls = 'log-line-error';
          else if (lowered.includes('warning') || lowered.includes('warn')) cls = 'log-line-warn';
          else if (lowered.includes('200 ok') || lowered.includes('complete')) cls = 'log-line-ok';
          return `<div class="${{cls}}">${{escapeHtml(line)}}</div>`;
        }}).join('');

        content.scrollTop = content.scrollHeight;
      }} catch (error) {{
        // keep dashboard usable even when log fetch fails
      }}
    }}

    fetchLogs();
    setInterval(fetchLogs, 10000);

    async function refreshApiStatus() {{
      try {{
        const res = await fetch('/admin/api/provider-status');
        const d = await res.json();
        const grid = document.getElementById('api-status-grid');
        const STATUS_COLOR = {{ok:'#22c55e', error:'#ef4444', no_key:'#555'}};
        const STATUS_ICON  = {{ok:'●', error:'✕', no_key:'○'}};
        grid.innerHTML = d.providers.map(p => `
          <div style="background:#1a1a1a;border:1px solid ${{STATUS_COLOR[p.status]}}40;
                      border-left:3px solid ${{STATUS_COLOR[p.status]}};
                      border-radius:8px;padding:12px;min-width:150px;flex-shrink:0">
            <div style="font-weight:600;font-size:12px;margin-bottom:4px">${{p.name}}</div>
            <div style="color:${{STATUS_COLOR[p.status]}};font-size:11px">
              ${{STATUS_ICON[p.status]}}
              ${{p.status==='ok' ? 'Online' : p.status==='no_key' ? 'No Key' : 'Error'}}
            </div>
            <div style="color:#666;font-size:10px;margin-top:2px">
              ${{p.latency_ms > 0 ? p.latency_ms+'ms' : '-'}}
              ${{p.error ? '<br><span style="color:#ef444488">'+p.error+'</span>' : ''}}
            </div>
          </div>
        `).join('');
        document.getElementById('api-status-time').textContent = d.checked_at;
      }} catch(e) {{
        const grid = document.getElementById('api-status-grid');
        if (grid) grid.textContent = 'Load failed';
      }}
    }}
    refreshApiStatus();
    setInterval(refreshApiStatus, 60000);

    // ── API Status widget drag + resize + collapse ────────────────────────
    (function initApiStatusWidget() {{
      const apiWidget  = document.getElementById('api-status-widget');
      const apiHandle  = document.getElementById('api-status-drag-handle');
      const apiResize  = document.getElementById('api-status-resize');
      const apiBody    = document.getElementById('api-status-body');
      const API_POS_KEY = 'api-status-pos-v1';
      let isDrag = false, isResize = false;
      let sx = 0, sy = 0, sl = 0, st = 0;
      let rsw = 0, rsh = 0, rsx = 0, rsy = 0;

      function savePos() {{
        localStorage.setItem(API_POS_KEY, JSON.stringify({{
          left: apiWidget.style.left,
          top:  apiWidget.style.top,
          width: apiWidget.style.width,
        }}));
      }}
      function loadPos() {{
        try {{
          const p = JSON.parse(localStorage.getItem(API_POS_KEY) || '{{}}');
          if (p.left) {{ apiWidget.style.left = p.left; apiWidget.style.right = 'auto'; }}
          if (p.top)  {{ apiWidget.style.top  = p.top;  apiWidget.style.bottom = 'auto'; }}
          if (p.width) apiWidget.style.width = p.width;
        }} catch(e) {{}}
      }}
      loadPos();

      apiHandle.addEventListener('mousedown', e => {{
        if (e.target.closest('button')) return;
        isDrag = true;
        sx = e.clientX; sy = e.clientY;
        const rect = apiWidget.getBoundingClientRect();
        sl = rect.left; st = rect.top;
        apiWidget.style.right = 'auto'; apiWidget.style.bottom = 'auto';
        e.preventDefault();
      }});
      apiResize.addEventListener('mousedown', e => {{
        isResize = true;
        rsx = e.clientX; rsy = e.clientY;
        rsw = apiWidget.offsetWidth; rsh = apiWidget.offsetHeight;
        e.preventDefault(); e.stopPropagation();
      }});
      document.addEventListener('mousemove', e => {{
        if (isDrag) {{
          apiWidget.style.left = (sl + e.clientX - sx) + 'px';
          apiWidget.style.top  = (st + e.clientY - sy) + 'px';
        }}
        if (isResize) {{
          apiWidget.style.width = Math.max(280, rsw + e.clientX - rsx) + 'px';
        }}
      }});
      document.addEventListener('mouseup', () => {{
        if (isDrag || isResize) savePos();
        isDrag = false; isResize = false;
      }});
    }})();

    function toggleApiStatus() {{
      const body = document.getElementById('api-status-body');
      const btn  = document.getElementById('api-status-toggle-btn');
      const resize = document.getElementById('api-status-resize');
      const collapsed = body.style.display === 'none';
      body.style.display = collapsed ? '' : 'none';
      resize.style.display = collapsed ? '' : 'none';
      btn.textContent = collapsed ? '_' : '▲';
    }}

    (function initAutoRefresh() {{
      const sel = document.getElementById('auto-refresh-select');
      const STORAGE_KEY = 'admin_auto_refresh';
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved !== null) {{
        const opt = sel.querySelector(`option[value="${{saved}}"]`);
        if (opt) opt.selected = true;
      }}
      let handle = null;
      function apply() {{
        if (handle) clearInterval(handle);
        const ms = Number(sel.value);
        localStorage.setItem(STORAGE_KEY, sel.value);
        if (ms > 0) handle = setInterval(() => location.reload(), ms);
      }}
      sel.addEventListener('change', apply);
      apply();
    }})();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def build_admin_config_html(configs: list[dict]) -> HTMLResponse:
    rows = ""
    for config in configs:
        key = str(config.get("key", ""))
        value = str(config.get("value", ""))
        description = str(config.get("description", ""))
        is_secret = bool(config.get("is_secret"))
        rows += f"""
        <tr>
          <td class="cfg-key">{escape(key)}</td>
          <td class="cfg-desc">{escape(description)}</td>
          <td>
            <input
              type="{'password' if is_secret else 'text'}"
              class="cfg-input"
              id="cfg-{escape(key, quote=True)}"
              value="{escape(value, quote=True)}"
              placeholder="{'(ไม่ได้ตั้งค่า)' if not value else ''}">
          </td>
          <td>
            <button class="cfg-save-btn" onclick='saveConfig({json.dumps(key, ensure_ascii=False)})'>Save</button>
          </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Config</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    body {{ font-family: Inter, sans-serif; background: #0d0d0d; color: #e5e5e5; margin: 0; padding: 24px; }}
    h1 {{ font-size: 22px; margin-bottom: 4px; }}
    .subtitle {{ color: #888; font-size: 14px; margin-bottom: 24px; }}
    .back-btn {{ display:inline-block; margin-bottom:20px; padding:8px 16px; background:#1a1a1a; color:#e5e5e5; border-radius:8px; text-decoration:none; font-size:14px; }}
    table {{ width: 100%; border-collapse: collapse; background: #141414; border-radius: 12px; overflow: hidden; }}
    th {{ background: #1a1a1a; padding: 12px 16px; text-align: left; font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }}
    td {{ padding: 12px 16px; border-bottom: 1px solid #222; vertical-align: middle; }}
    .cfg-key {{ font-family: monospace; color: #7c3aed; font-size: 13px; min-width: 200px; }}
    .cfg-desc {{ color: #888; font-size: 13px; max-width: 250px; }}
    .cfg-input {{ background: #222; border: 1px solid #333; border-radius: 6px; padding: 8px 12px; color: #e5e5e5; font-size: 14px; width: 100%; box-sizing: border-box; font-family: inherit; }}
    .cfg-save-btn {{ background: #7c3aed; color: white; border: none; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-size: 13px; font-weight: 500; white-space: nowrap; }}
    .cfg-save-btn:hover {{ background: #6d28d9; }}
    .test-section {{ margin-top: 24px; background: #141414; border-radius: 12px; padding: 20px; }}
    .test-btn {{ background: #059669; color: white; border: none; border-radius: 8px; padding: 10px 20px; cursor: pointer; font-size: 14px; font-weight: 500; margin-right: 8px; }}
    #test-result {{ margin-top: 12px; padding: 10px; border-radius: 6px; font-size: 14px; display: none; }}
    .toast {{ position:fixed; bottom:24px; right:24px; background:#333; color:white; padding:10px 20px; border-radius:8px; font-size:14px; display:none; z-index:999; }}
  </style>
</head>
<body>
  <a href="/admin" class="back-btn">← กลับ Admin</a>
  <h1>⚙️ Config Manager</h1>
  <p class="subtitle">แก้ไข API keys และ settings ทั้งหมดได้จากที่นี่</p>

  <table>
    <thead>
      <tr>
        <th>Key</th>
        <th>Description</th>
        <th>Value</th>
        <th>Action</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>

  <div class="test-section">
    <h3 style="margin:0 0 12px">🧪 Test Connections</h3>
    <button class="test-btn" onclick="testLine()">📱 ทดสอบ LINE</button>
    <div id="test-result"></div>
  </div>

  <div id="toast" class="toast"></div>

  <script>
    async function saveConfig(key) {{
      const input = document.getElementById('cfg-' + key);
      const value = input.value;
      const resp = await fetch('/admin/config/update', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        credentials: 'same-origin',
        body: JSON.stringify({{key, value}})
      }});
      const data = await resp.json();
      showToast(data.ok ? '✅ บันทึกแล้ว: ' + key : '❌ บันทึกไม่ได้');
    }}

    async function testLine() {{
      const result = document.getElementById('test-result');
      result.style.display = 'block';
      result.style.background = '#1a1a1a';
      result.textContent = 'กำลังทดสอบ...';
      const resp = await fetch('/admin/config/test-line', {{method: 'POST', credentials: 'same-origin'}});
      const data = await resp.json();
      result.style.background = data.ok ? '#052e16' : '#2d0000';
      result.textContent = data.ok ? '✅ ' + data.message : '❌ ' + data.message;
    }}

    function showToast(msg) {{
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.style.display = 'block';
      setTimeout(() => t.style.display = 'none', 3000);
    }}

    document.querySelectorAll('.cfg-input').forEach((input) => {{
      input.addEventListener('keydown', (e) => {{
        if (e.key === 'Enter') {{
          saveConfig(input.id.replace('cfg-', ''));
        }}
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

    const USD_TO_THB = 33;

    function toBaht(value, suffix = "") {{
      const amount = Number(value || 0);
      if (suffix === "$" || suffix === "USD") return amount * USD_TO_THB;
      return amount;
    }}

    function formatMetricValue(value, suffix = "") {{
      if (suffix === "฿" || suffix === "$" || suffix === "USD") {{
        return `฿${{toBaht(value, suffix).toFixed(2)}}`;
      }}
      const amount = Number(value || 0);
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

    function makeBarChart(el, labels, datasets, options = {{}}) {{
      return new Chart(document.getElementById(el), {{
        type: "bar",
        data: {{ labels, datasets }},
        options: {{ ...graphOptions(), ...options }}
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
        }}], {{
          plugins: {{
            ...graphOptions().plugins,
            tooltip: {{
              callbacks: {{
                label: (ctx) => `฿${{Number(ctx.raw || 0).toFixed(2)}}`,
              }},
            }},
          }},
          scales: {{
            ...graphOptions().scales,
            y: {{
              ...graphOptions().scales.y,
              ticks: {{
                ...graphOptions().scales.y.ticks,
                callback: (value) => `฿${{Number(value).toFixed(0)}}`,
              }},
            }},
          }},
        }});
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


def build_pipeline_html() -> HTMLResponse:
    html = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Pipeline</title>
  <style>
    :root {
      --bg: #0f0f1a;
      --panel: #111224;
      --panel-2: #17172a;
      --border: #2b2d42;
      --text: #f2f3f7;
      --muted: #9aa0b8;
      --green: #22c55e;
      --yellow: #f59e0b;
      --red: #ef4444;
      --blue: #60a5fa;
      --purple: #a78bfa;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .wrap { max-width: 1440px; margin: 0 auto; padding: 18px 14px 40px; }
    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin-bottom: 16px;
    }
    .title-block h1 { margin: 0; font-size: 24px; }
    .title-block p { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
    .controls { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    button, a {
      font: inherit;
      text-decoration: none;
    }
    .link-btn, .btn {
      background: #22243a;
      border: 1px solid #35385a;
      color: var(--text);
      border-radius: 10px;
      padding: 10px 12px;
      cursor: pointer;
    }
    .meta-pill {
      background: #17172a;
      border: 1px solid #35385a;
      color: var(--muted);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 13px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 16px;
    }
    .card h3 {
      margin: 0 0 6px;
      font-size: 13px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .stat-value {
      font-size: 32px;
      font-weight: 700;
      line-height: 1.1;
    }
    .stat-meta {
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
    }
    .section-title {
      margin: 22px 0 12px;
      font-size: 18px;
    }
    .chart-card { padding: 18px; }
    .chart-legend {
      display: flex;
      gap: 14px;
      flex-wrap: wrap;
      margin-bottom: 14px;
      color: var(--muted);
      font-size: 13px;
    }
    .legend-dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 999px;
      margin-right: 6px;
    }
    .chart-list {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .chart-row {
      display: grid;
      grid-template-columns: 120px 1fr 92px;
      gap: 12px;
      align-items: center;
    }
    .chart-model {
      font-weight: 600;
      color: #dbe2ff;
    }
    .chart-track {
      width: 100%;
      height: 22px;
      background: var(--panel-2);
      border: 1px solid #35385a;
      border-radius: 999px;
      overflow: hidden;
      display: flex;
    }
    .bar-router { background: var(--blue); }
    .bar-reasoner { background: var(--purple); }
    .bar-checker { background: var(--green); }
    .chart-total {
      text-align: right;
      color: var(--muted);
      font-size: 13px;
    }
    .table-card { overflow: hidden; }
    .table-wrap { overflow: auto; }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
    }
    th, td {
      padding: 10px 8px;
      border-bottom: 1px solid #23253a;
      text-align: left;
      font-size: 13px;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    td.num, th.num { text-align: right; }
    td.center, th.center { text-align: center; }
    .question {
      max-width: 360px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .model-pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #22243a;
      border: 1px solid #35385a;
      font-size: 11px;
    }
    .ok { color: var(--green); font-weight: 700; }
    .warn { color: var(--yellow); font-weight: 700; }
    .bad { color: var(--red); font-weight: 700; }
    .empty {
      color: var(--muted);
      text-align: center;
      padding: 20px;
    }
    @media (max-width: 900px) {
      .chart-row { grid-template-columns: 1fr; }
      .chart-total { text-align: left; }
      .question { max-width: 220px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title-block">
        <h1>⚡ Ener-AI Pipeline</h1>
        <p>Reasoning latency monitor by stage: Router, Reasoner, Checker</p>
      </div>
      <div class="controls">
        <div id="last-updated" class="meta-pill">Last updated: -</div>
        <button id="refresh-btn" class="btn" type="button">Refresh</button>
        <a class="link-btn" href="/admin">← กลับหน้า Admin</a>
      </div>
    </div>

    <div id="summary-grid" class="grid">
      <div class="card"><h3>Loading</h3><div class="stat-value">...</div></div>
    </div>

    <div class="card chart-card">
      <div class="section-title">Stage Breakdown by Model</div>
      <div class="chart-legend">
        <span><span class="legend-dot bar-router"></span>Router</span>
        <span><span class="legend-dot bar-reasoner"></span>Reasoner</span>
        <span><span class="legend-dot bar-checker"></span>Checker</span>
      </div>
      <div id="chart-list" class="chart-list">
        <div class="empty">Loading chart...</div>
      </div>
    </div>

    <div class="card table-card" style="margin-top:18px;">
      <div class="section-title">Recent Requests</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Question</th>
              <th>Complexity</th>
              <th>Model</th>
              <th class="num">Router</th>
              <th class="num">Reasoner</th>
              <th class="num">Checker</th>
              <th class="num">Total</th>
              <th class="center">Fixed</th>
            </tr>
          </thead>
          <tbody id="pipeline-tbody">
            <tr><td colspan="9" class="empty">Loading requests...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const MODEL_ORDER = ["groq", "deepseek-r1", "haiku"];

    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function modelRows(payload) {
      const map = new Map((payload.averages || []).map((item) => [String(item.model_used || ""), item]));
      return MODEL_ORDER.map((model) => map.get(model) || {
        model_used: model,
        count: 0,
        avg_total: 0,
        avg_router: 0,
        avg_reasoner: 0,
        avg_checker: 0,
      });
    }

    function totalClass(totalMs) {
      if (totalMs > 3000) return "bad";
      if (totalMs >= 1500) return "warn";
      return "ok";
    }

    function renderSummary(rows) {
      document.getElementById("summary-grid").innerHTML = rows.map((item) => `
        <div class="card">
          <h3>${escapeHtml(item.model_used)}</h3>
          <div class="stat-value">${Math.round(Number(item.avg_total || 0))}ms</div>
          <div class="stat-meta">
            Router ${Math.round(Number(item.avg_router || 0))}ms |
            Reasoner ${Math.round(Number(item.avg_reasoner || 0))}ms |
            Checker ${Math.round(Number(item.avg_checker || 0))}ms
          </div>
          <div class="stat-meta">${Number(item.count || 0).toLocaleString()} requests / 24h</div>
        </div>
      `).join("");
    }

    function renderChart(rows) {
      const maxTotal = Math.max(...rows.map((item) => Number(item.avg_total || 0)), 1);
      document.getElementById("chart-list").innerHTML = rows.map((item) => {
        const router = Number(item.avg_router || 0);
        const reasoner = Number(item.avg_reasoner || 0);
        const checker = Number(item.avg_checker || 0);
        const total = Number(item.avg_total || 0);
        return `
          <div class="chart-row">
            <div class="chart-model">${escapeHtml(item.model_used)}</div>
            <div class="chart-track" title="Router ${Math.round(router)}ms | Reasoner ${Math.round(reasoner)}ms | Checker ${Math.round(checker)}ms">
              <div class="bar-router" style="width:${(router / maxTotal) * 100}%"></div>
              <div class="bar-reasoner" style="width:${(reasoner / maxTotal) * 100}%"></div>
              <div class="bar-checker" style="width:${(checker / maxTotal) * 100}%"></div>
            </div>
            <div class="chart-total">${Math.round(total)}ms avg</div>
          </div>
        `;
      }).join("");
    }

    function renderTable(rows) {
      const tbody = document.getElementById("pipeline-tbody");
      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">ยังไม่มี pipeline requests</td></tr>';
        return;
      }
      tbody.innerHTML = rows.map((item) => {
        const totalMs = Number(item.total_ms || 0);
        const createdAt = item.created_at ? new Date(String(item.created_at).replace(" ", "T")) : null;
        const timeText = createdAt && !Number.isNaN(createdAt.getTime())
          ? createdAt.toLocaleTimeString("th-TH")
          : escapeHtml(item.created_at || "-");
        return `
          <tr>
            <td>${timeText}</td>
            <td class="question" title="${escapeHtml(item.question_preview || "-")}">${escapeHtml(item.question_preview || "-")}</td>
            <td>${escapeHtml(item.complexity || "-")}</td>
            <td><span class="model-pill">${escapeHtml(item.model_used || "-")}</span></td>
            <td class="num">${Number(item.router_ms || 0)}ms</td>
            <td class="num">${Number(item.reasoner_ms || 0)}ms</td>
            <td class="num">${Number(item.checker_ms || 0)}ms</td>
            <td class="num ${totalClass(totalMs)}">${totalMs}ms</td>
            <td class="center">${item.was_fixed ? "🔧" : "✅"}</td>
          </tr>
        `;
      }).join("");
    }

    async function loadPipeline() {
      const response = await fetch("/admin/pipeline-metrics", { cache: "no-store" });
      if (!response.ok) throw new Error("load failed");
      const payload = await response.json();
      const rows = modelRows(payload);
      renderSummary(rows);
      renderChart(rows);
      renderTable(payload.recent || []);
      document.getElementById("last-updated").textContent =
        `Last updated: ${new Date().toLocaleTimeString("th-TH")}`;
    }

    async function refreshNow() {
      try {
        await loadPipeline();
      } catch (error) {
        document.getElementById("chart-list").innerHTML = '<div class="empty">โหลด pipeline metrics ไม่สำเร็จ</div>';
        document.getElementById("pipeline-tbody").innerHTML = '<tr><td colspan="9" class="empty">โหลด recent requests ไม่สำเร็จ</td></tr>';
      }
    }

    document.getElementById("refresh-btn").addEventListener("click", refreshNow);
    setInterval(refreshNow, 30000);
    refreshNow();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


def _workspace_user_id() -> str:
    return str(settings.telegram_chat_id)


def _workspace_upload_dir() -> Path:
    upload_dir = _data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _normalize_project_id(value: object) -> int | None:
    if value in {None, "", "all", "null"}:
        return None
    try:
        project_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return project_id if project_id > 0 else None


async def _workspace_history_rows(project_id: int | None = None, limit: int = 200) -> list[dict]:
    limit_value = max(1, min(limit, 500))
    async with get_db() as db:
        if project_id is None:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    role,
                    content,
                    COALESCE(source, 'telegram') AS source,
                    project_id,
                    datetime(created_at, '+7 hours') AS local_created_at
                FROM messages
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (_workspace_user_id(), limit_value),
            )
        else:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    role,
                    content,
                    COALESCE(source, 'telegram') AS source,
                    project_id,
                    datetime(created_at, '+7 hours') AS local_created_at
                FROM messages
                WHERE chat_id = ? AND project_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (_workspace_user_id(), project_id, limit_value),
            )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "source": str(row["source"] or "telegram"),
            "project_id": row["project_id"],
            "created_at": str(row["local_created_at"] or ""),
        }
        for row in reversed(rows)
    ]


async def _workspace_save_chat_messages(project_id: int | None, user_text: str, reply_text: str) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO messages (chat_id, role, content, project_id, source)
            VALUES (?, ?, ?, ?, 'web')
            """,
            (_workspace_user_id(), "user", user_text, project_id),
        )
        await db.execute(
            """
            INSERT INTO messages (chat_id, role, content, project_id, source)
            VALUES (?, ?, ?, ?, 'web')
            """,
            (_workspace_user_id(), "assistant", reply_text, project_id),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("workspace_chat_saved", f"project_id={project_id or 'all'}"),
        )
        await db.commit()


async def _workspace_generate_reply(
    text: str,
    project_id: int | None,
    preferred_model: str | None = None,
) -> str:
    from app.agents.chat import _build_system_prompt
    from app.core.ai import _call_anthropic_with_tools, _call_groq_with_tools, chat, get_model_availability
    from app.core.memory import extract_and_store_long_term_memories
    from app.core.tools import TOOLS, execute_tool

    history = [
        {"role": row["role"], "content": row["content"]}
        for row in await _workspace_history_rows(project_id=project_id, limit=40)
    ]
    system_prompt = await _build_system_prompt()
    availability = get_model_availability()
    selected_model = str(preferred_model or "").strip().lower() or None
    response: dict[str, object]

    if selected_model == "haiku" and availability.get("haiku"):
        response = await _call_anthropic_with_tools(text, system_prompt, history, TOOLS, "MainChatAgent")
    elif selected_model == "groq" and availability.get("groq"):
        response = await _call_groq_with_tools(text, system_prompt, history, TOOLS, "MainChatAgent")
    else:
        reply_text = await chat(
            text,
            system=system_prompt,
            agent="MainChatAgent",
            messages=history,
            preferred_model=selected_model,
        )
        response = {"text": reply_text, "tool_calls": []}

    reply = str(response.get("text", "") or "").strip() or "ยังไม่มีคำตอบตอนนี้"
    tool_calls = response.get("tool_calls", []) or []
    tool_results: list[str] = []

    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        tool_name = str(tool_call.get("name", "")).strip()
        tool_input = tool_call.get("input", {}) or {}
        if not tool_name:
            continue
        try:
            result = await execute_tool(tool_name, tool_input)
            tool_results.append(f"✅ {result}")
        except Exception as exc:
            tool_results.append(f"⚠️ tool {tool_name} ทำงานไม่สำเร็จ: {exc}")

    final_reply = reply if not tool_results else reply + "\n\n" + "\n".join(tool_results)
    await _workspace_save_chat_messages(project_id, text, final_reply)

    try:
        async with get_db() as db:
            await db.execute("DELETE FROM memories WHERE key = 'model_handoff_context'")
            await db.commit()
    except Exception:
        pass

    try:
        await extract_and_store_long_term_memories(text, final_reply)
    except Exception:
        pass

    return final_reply


def _read_uploaded_file_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        from PyPDF2 import PdfReader

        reader = PdfReader(str(file_path))
        return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    if suffix == ".docx":
        from docx import Document

        document = Document(str(file_path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
    if suffix in {".txt", ".md"}:
        return file_path.read_text(encoding="utf-8", errors="ignore").strip()
    raise HTTPException(status_code=400, detail="รองรับเฉพาะ PDF, DOCX, TXT, MD")


def _parse_brainstorm_blocks(raw_text: str) -> dict:
    rounds = []
    current = None
    verdict = ""
    reason = ""
    for line in str(raw_text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("รอบ "):
            if current:
                rounds.append(current)
            current = {"round": stripped, "ai_a": "", "ai_b": "", "ai_c": ""}
            continue
        if stripped.startswith("AI_A:") and current is not None:
            current["ai_a"] = stripped.replace("AI_A:", "", 1).strip()
            continue
        if stripped.startswith("AI_B:") and current is not None:
            current["ai_b"] = stripped.replace("AI_B:", "", 1).strip()
            continue
        if stripped.startswith("AI_C:") and current is not None:
            current["ai_c"] = stripped.replace("AI_C:", "", 1).strip()
            continue
        if stripped.startswith("🎯 คำตัดสิน:"):
            verdict = stripped.replace("🎯 คำตัดสิน:", "", 1).strip()
            continue
        if stripped.startswith("เหตุผล:"):
            reason = stripped.replace("เหตุผล:", "", 1).strip()
    if current:
        rounds.append(current)
    return {"rounds": rounds, "verdict": verdict, "reason": reason, "raw": raw_text}


def build_workspace_html() -> HTMLResponse:
    html = """<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Workspace</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #212121;
      --sidebar: #171717;
      --card: #2f2f2f;
      --accent: #7c3aed;
      --accent-hover: #6d28d9;
      --text: #ececec;
      --subtext: #8e8ea0;
      --border: #383838;
      --user-bubble: #2f2f2f;
      --ai-bubble: transparent;
    }

    * { box-sizing: border-box; }

    body {
      font-family: 'Inter', sans-serif;
      font-size: 16px;
      line-height: 1.7;
      background: var(--bg);
      color: var(--text);
      margin: 0;
    }

    input, textarea, button, select {
      font-size: 16px;
      font-family: inherit;
    }

    a {
      color: inherit;
      text-decoration: none;
    }

    #app {
      display: flex;
      height: 100vh;
      overflow: hidden;
      width: 100%;
    }

    #sidebar {
      width: 240px;
      min-width: 240px;
      max-width: 240px;
      height: 100vh;
      overflow-y: auto;
      flex-shrink: 0;
      background: var(--sidebar);
      display: flex;
      flex-direction: column;
      padding: 12px 8px;
      border-right: 1px solid var(--border);
      box-sizing: border-box;
      gap: 4px;
    }

    .sidebar-logo {
      font-size: 22px;
      font-weight: 700;
      padding: 8px 12px 16px;
      letter-spacing: -0.01em;
    }

    .new-chat-btn {
      width: 100%;
      padding: 10px 16px;
      background: var(--accent);
      color: white;
      border: none;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
      margin-bottom: 16px;
    }

    .new-chat-btn:hover,
    .panel-action:hover,
    #send-btn:hover,
    .primary-btn:hover,
    .file-action:hover {
      background: var(--accent-hover);
    }

    .sidebar-section {
      font-size: 12px;
      font-weight: 600;
      color: var(--subtext);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 12px 12px 4px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .new-proj-btn {
      width: 24px;
      height: 24px;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--text);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }

    .nav-item {
      display: block;
      padding: 8px 12px;
      color: var(--text);
      font-size: 15px;
      border-radius: 6px;
      cursor: pointer;
      margin: 1px 0;
    }

    .nav-item:hover {
      background: var(--card);
    }

    .nav-item.active {
      background: var(--card);
      font-weight: 500;
    }

    .project-link.active {
      outline: 1px solid rgba(124, 58, 237, 0.55);
    }

    .project-meta {
      display: block;
      font-size: 11px;
      color: var(--subtext);
      margin-top: 2px;
    }

    .sidebar-footer {
      margin-top: auto;
      padding: 12px;
      font-size: 12px;
      color: var(--subtext);
    }

    #active-model-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: #1f1f1f;
    }

    #content {
      flex: 1;
      min-width: 0;
      height: 100vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    .panel {
      display: none;
      flex-direction: column;
      flex: 1;
      height: 100%;
      overflow: hidden;
    }

    .panel.active-panel {
      display: flex;
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 20px 32px 12px;
      border-bottom: 1px solid var(--border);
      gap: 16px;
    }

    .panel-header h2 {
      margin: 0;
      font-size: 22px;
      font-weight: 600;
      letter-spacing: -0.01em;
    }

    .panel-action,
    .primary-btn,
    .file-action {
      border: none;
      background: var(--accent);
      color: white;
      border-radius: 8px;
      padding: 9px 14px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
    }

    .secondary-btn {
      border: 1px solid var(--border);
      background: transparent;
      color: var(--text);
      border-radius: 8px;
      padding: 9px 14px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
    }

    #panel-chat {
      display: none;
      flex-direction: column;
      height: 100%;
      overflow: hidden;
    }

    #panel-chat.active-panel {
      display: flex;
    }

    #chat-messages {
      flex: 1;
      overflow-y: auto;
      overflow-x: hidden;
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      width: 100%;
      box-sizing: border-box;
      max-width: 100%;
    }

    .msg-row {
      display: flex;
      width: 100%;
      padding: 4px 0;
      box-sizing: border-box;
    }

    .user-row {
      justify-content: flex-end;
    }

    .ai-row {
      justify-content: flex-start;
    }

    .msg-bubble {
      max-width: 70%;
      padding: 12px 16px;
      border-radius: 18px;
      font-size: 16px;
      line-height: 1.75;
      word-break: break-word;
      box-sizing: border-box;
    }

    .user-bubble {
      background: var(--user-bubble);
      border-radius: 18px 18px 4px 18px;
      min-width: 60px;
      max-width: 65%;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .ai-bubble {
      background: transparent;
      border-radius: 0;
      padding-left: 0;
      max-width: 80%;
    }

    .msg-text {
      font-size: 16px;
      line-height: 1.7;
    }

    .msg-meta {
      display: block;
      font-size: 11px;
      color: #666;
      margin-top: 4px;
      text-align: right;
    }

    .ai-row .msg-meta {
      text-align: left;
    }

    #chat-input-wrap {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      padding: 12px 16px 20px;
      width: 100%;
      max-width: 800px;
      margin: 0 auto;
      box-sizing: border-box;
      flex-shrink: 0;
    }

    #chat-input {
      flex: 1;
      min-width: 0;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px 18px;
      color: var(--text);
      font-size: 16px;
      font-family: inherit;
      resize: none;
      outline: none;
      min-height: 52px;
      max-height: 200px;
      overflow-y: auto;
      line-height: 1.6;
      box-sizing: border-box;
    }

    .tool-card-title {
      font-size: 16px;
      font-weight: 600;
    }

    .tool-card-desc {
      font-size: 14px;
    }

    #send-btn {
      width: 44px;
      height: 44px;
      min-width: 44px;
      border-radius: 50%;
      background: var(--accent);
      color: white;
      border: none;
      font-size: 20px;
      cursor: pointer;
      flex-shrink: 0;
      display: flex;
      align-items: center;
      justify-content: center;
    }

    #send-btn:disabled {
      background: var(--border);
      cursor: not-allowed;
    }

    #slash-menu {
      position: fixed;
      bottom: 80px;
      left: 50%;
      transform: translateX(-50%);
      max-width: 700px;
      width: calc(100% - 240px - 48px);
      background: #2f2f2f;
      border: 1px solid #444;
      border-radius: 12px;
      overflow: hidden;
      z-index: 100;
      box-shadow: 0 8px 32px rgba(0,0,0,0.5);
    }

    .slash-item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 16px;
      cursor: pointer;
      font-size: 15px;
      transition: background 0.1s;
    }

    .slash-item:hover,
    .slash-item.selected {
      background: #3f3f3f;
    }

    .slash-cmd {
      color: #7c3aed;
      font-weight: 600;
      min-width: 120px;
    }

    .slash-desc {
      color: #999;
      font-size: 14px;
    }

    .standup-panel-body {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
      gap: 16px;
      align-items: start;
    }

    .standup-preview {
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      background: #1a1a1a;
      padding: 20px;
      border-radius: 8px;
      overflow-y: auto;
      min-height: 520px;
      border: 1px solid var(--border);
    }

    .standup-project-card {
      background: var(--card);
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 12px;
      border: 1px solid var(--border);
    }

    .sp-name { font-weight: 600; font-size: 15px; margin-bottom: 10px; color: #7c3aed; }

    .sp-row { display: flex; gap: 12px; align-items: center; margin-bottom: 8px; }

    .sp-row label { font-size: 12px; color: #888; min-width: 70px; }

    .sp-row input,
    .sp-row textarea {
      flex: 1;
      background: #2a2a2a;
      border: 1px solid #444;
      border-radius: 6px;
      padding: 6px 10px;
      color: var(--text);
      font-size: 14px;
      font-family: inherit;
    }

    .thinking {
      display: flex;
      gap: 5px;
      align-items: center;
      padding: 16px 0;
    }

    .dot {
      width: 8px;
      height: 8px;
      background: var(--subtext);
      border-radius: 50%;
      animation: bounce 1.2s infinite;
    }

    .dot:nth-child(2) { animation-delay: 0.2s; }
    .dot:nth-child(3) { animation-delay: 0.4s; }

    @keyframes bounce {
      0%,60%,100% { transform: translateY(0); }
      30% { transform: translateY(-6px); }
    }

    .panel-body {
      flex: 1;
      overflow-y: auto;
      padding: 20px 32px 32px;
    }

    .stack {
      display: grid;
      gap: 16px;
    }

    .surface {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
    }

    .note-input-wrap,
    .task-input-wrap,
    .brainstorm-input-wrap {
      display: grid;
      gap: 12px;
      grid-template-columns: 1fr;
      margin-bottom: 20px;
    }

    .task-input-wrap {
      grid-template-columns: minmax(0, 1fr) 160px 96px;
      align-items: center;
    }

    .panel textarea,
    .panel input,
    .panel select {
      width: 100%;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #262626;
      color: var(--text);
      padding: 12px 14px;
      outline: none;
    }

    .notes-group {
      display: grid;
      gap: 10px;
    }

    .notes-group h3,
    .task-group h3 {
      margin: 0 0 10px;
      font-size: 15px;
      color: var(--subtext);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .note-card,
    .task-card,
    .memory-card,
    .news-card,
    .file-card,
    .brain-card,
    .verdict-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
    }

    .note-card summary {
      cursor: pointer;
      font-weight: 500;
    }

    .note-meta,
    .task-meta,
    .memory-meta,
    .news-meta,
    .file-meta {
      font-size: 12px;
      color: var(--subtext);
      margin-top: 6px;
    }

    .task-groups {
      display: grid;
      gap: 16px;
    }

    .task-item {
      display: flex;
      gap: 12px;
      align-items: flex-start;
      padding: 12px 0;
      border-bottom: 1px solid rgba(255,255,255,0.06);
    }

    .task-item:last-child {
      border-bottom: none;
      padding-bottom: 0;
    }

    .task-item input[type="checkbox"] {
      width: 18px;
      height: 18px;
      margin-top: 3px;
      accent-color: var(--accent);
      flex: 0 0 auto;
    }

    .priority-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
      margin-top: 8px;
      background: rgba(255,255,255,0.08);
      color: var(--subtext);
    }

    .priority-high { color: #fca5a5; }
    .priority-medium { color: #fde68a; }
    .priority-low { color: #86efac; }

    .brain-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin-top: 20px;
    }

    .brain-card h4,
    .verdict-card h4 {
      margin: 0 0 10px;
      font-size: 15px;
    }

    .verdict-card {
      margin-top: 16px;
      background: rgba(124, 58, 237, 0.12);
    }

    .news-card + .news-card,
    .memory-card + .memory-card,
    .file-card + .file-card {
      margin-top: 12px;
    }

    .file-actions,
    .row-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 12px;
    }

    .sys-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }

    .sys-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
    }

    .sys-label {
      font-size: 12px;
      color: var(--subtext);
      margin-bottom: 6px;
    }

    .sys-value {
      font-size: 22px;
      font-weight: 700;
      color: var(--text);
    }

    .sched-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .sched-item {
      display: flex;
      gap: 16px;
      background: var(--card);
      border: 1px solid var(--border);
      padding: 10px 16px;
      border-radius: 10px;
      align-items: center;
    }

    .sched-time {
      color: var(--accent);
      font-weight: 600;
      min-width: 120px;
      font-size: 13px;
    }

    .sched-job {
      color: var(--text);
      font-size: 14px;
    }

    .agent-chips {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }

    .agent-chip {
      background: #2a2a2a;
      border: 1px solid #444;
      border-radius: 20px;
      padding: 4px 12px;
      font-size: 12px;
      color: #aaa;
    }

    #drop-zone {
      border: 1px dashed var(--border);
      border-radius: 16px;
      padding: 24px;
      text-align: center;
      color: var(--subtext);
      cursor: pointer;
      margin-bottom: 20px;
      background: rgba(255,255,255,0.02);
    }

    #drop-zone.dragover {
      border-color: var(--accent);
      background: rgba(124,58,237,0.08);
      color: var(--text);
    }

    #toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #333;
      color: white;
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 14px;
      display: none;
      z-index: 999;
      max-width: 320px;
    }

    #modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.6);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 998;
    }

    #modal-box {
      background: var(--card);
      padding: 24px;
      border-radius: 16px;
      min-width: 320px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      border: 1px solid var(--border);
    }

    #modal-box input {
      padding: 10px 14px;
      border-radius: 8px;
      border: 1px solid var(--border);
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
    }

    #modal-box button {
      padding: 10px;
      background: var(--accent);
      color: white;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      font-size: 15px;
    }

    .empty-state {
      color: var(--subtext);
      font-size: 14px;
      padding: 18px 0;
    }

    @media (max-width: 1024px) {
      .brain-grid {
        grid-template-columns: 1fr;
      }

      .sys-grid {
        grid-template-columns: 1fr;
      }

      .task-input-wrap {
        grid-template-columns: 1fr;
      }

      .standup-panel-body {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 768px) {
      #sidebar { display: none; }
      #chat-messages { padding-bottom: 132px; }
      #chat-input-wrap,
      #chat-messages {
        max-width: 100%;
      }
      .panel-body,
      .panel-header {
        padding-left: 18px;
        padding-right: 18px;
      }
      #slash-menu { width: calc(100% - 32px); }
      .msg-row { padding-left: 18px; padding-right: 18px; }
      .msg-bubble { max-width: 88%; }
      .sp-row { align-items: flex-start; flex-direction: column; }
      .sp-row label { min-width: 0; }
    }
  </style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="sidebar-logo">⚡ Ener-AI</div>

    <button class="new-chat-btn" onclick="newChat()">＋ New Chat</button>

    <div class="sidebar-section">TOOLS</div>
    <nav id="tool-nav">
      <a class="nav-item active" onclick="showPanel('chat')" data-panel="chat">💬 Chat</a>
      <a class="nav-item" onclick="showPanel('notes')" data-panel="notes">📝 Notes</a>
      <a class="nav-item" onclick="showPanel('tasks')" data-panel="tasks">✅ Tasks</a>
      <a class="nav-item" onclick="showPanel('standup')" data-panel="standup">📋 Standup</a>
      <a class="nav-item" onclick="showPanel('brainstorm')" data-panel="brainstorm">🔥 Brainstorm</a>
      <a class="nav-item" onclick="showPanel('news')" data-panel="news">📰 News</a>
      <a class="nav-item" onclick="showPanel('memory')" data-panel="memory">🧠 Memory</a>
      <a class="nav-item" onclick="showPanel('files')" data-panel="files">📁 Files</a>
      <a class="nav-item" onclick="showPanel('benchmark')" data-panel="benchmark">🏆 Benchmark</a>
      <a class="nav-item" onclick="showPanel('code')" data-panel="code">💻 Code</a>
      <a class="nav-item" onclick="showPanel('system')" data-panel="system">⚙️ System</a>
    </nav>

    <div class="sidebar-section">
      PROJECTS
      <button class="new-proj-btn" onclick="showNewProjectModal()">＋</button>
    </div>
    <nav id="project-nav"></nav>

    <div class="sidebar-footer">
      <span id="active-model-badge">Auto / Active</span>
    </div>
  </aside>

  <main id="content">
    <div id="panel-chat" class="panel active-panel">
      <div id="chat-messages"></div>
      <div id="chat-input-wrap">
        <textarea id="chat-input" rows="1" placeholder="Message Ener-AI..."></textarea>
        <button id="send-btn" onclick="sendMessage()">↑</button>
      </div>
    </div>

    <div id="panel-notes" class="panel">
      <div class="panel-header"><h2>📝 Notes</h2></div>
      <div class="panel-body">
        <div class="note-input-wrap surface">
          <textarea id="note-input" rows="3" placeholder="Drop a thought..."></textarea>
          <div class="row-actions">
            <button class="primary-btn" onclick="saveNote()">Save Note</button>
          </div>
        </div>
        <div id="notes-list" class="stack"></div>
      </div>
    </div>

    <div id="panel-tasks" class="panel">
      <div class="panel-header"><h2>✅ Tasks</h2></div>
      <div class="panel-body">
        <div class="task-input-wrap surface">
          <input id="task-input" type="text" placeholder="Add a task...">
          <select id="task-priority">
            <option value="medium">Medium</option>
            <option value="high">High</option>
            <option value="low">Low</option>
          </select>
          <button class="primary-btn" onclick="createTask()">Add</button>
        </div>
        <div id="tasks-list"></div>
      </div>
    </div>

    <div id="panel-standup" class="panel">
      <div class="panel-header">
        <h2>📋 Daily Standup</h2>
        <div class="row-actions" style="margin-top:0">
          <button class="secondary-btn" onclick="copyStandupReport()">Copy</button>
          <button class="panel-action" onclick="generateStandup()">Generate Report</button>
        </div>
      </div>
      <div class="panel-body standup-panel-body">
        <div id="standup-preview" class="standup-preview">กด Generate Report เพื่อสร้าง standup ล่าสุด</div>
        <div id="standup-projects"></div>
      </div>
    </div>

    <div id="panel-brainstorm" class="panel">
      <div class="panel-header"><h2>🔥 Brainstorm</h2></div>
      <div class="panel-body">
        <div class="brainstorm-input-wrap surface">
          <input id="brainstorm-input" type="text" placeholder="Enter topic to debate...">
          <button class="primary-btn" onclick="runBrainstorm()">Start Debate</button>
        </div>
        <div id="brainstorm-result"></div>
      </div>
    </div>

    <div id="panel-news" class="panel">
      <div class="panel-header">
        <h2>📰 News</h2>
        <button class="panel-action" onclick="fetchNews()">Fetch Latest</button>
      </div>
      <div class="panel-body">
        <div id="news-list"></div>
      </div>
    </div>

    <div id="panel-memory" class="panel">
      <div class="panel-header"><h2>🧠 Memory</h2></div>
      <div class="panel-body">
        <div id="memory-list"></div>
      </div>
    </div>

    <div id="panel-files" class="panel">
      <div class="panel-header"><h2>📁 Files</h2></div>
      <div class="panel-body">
        <div id="drop-zone">Drag & drop PDF, DOCX, TXT or click to upload</div>
        <input type="file" id="file-input" accept=".pdf,.docx,.txt,.md" style="display:none">
        <div id="files-list"></div>
      </div>
    </div>

    <div id="panel-benchmark" class="panel">
      <div class="panel-header" style="padding:20px 24px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
        <h2 style="margin:0;font-size:20px;font-weight:600">🏆 Model Benchmark</h2>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="bench-category" style="background:#2a2a2a;border:1px solid #444;border-radius:6px;padding:6px 12px;color:#e5e5e5;font-size:14px">
            <option value="">All Questions (12)</option>
            <option value="it">🏥 Hospital IT (3)</option>
            <option value="en">⚡ Ener Scan (3)</option>
            <option value="hal">🔍 Hallucination (3)</option>
            <option value="ch">💬 Simple Chat (3)</option>
          </select>
          <button id="bench-run-btn" onclick="runBenchmark()" style="background:#7c3aed;color:white;border:none;border-radius:8px;padding:8px 20px;cursor:pointer;font-size:14px;font-weight:500">
            ▶ Run Benchmark
          </button>
        </div>
      </div>
      <div id="bench-summary" style="display:flex;gap:12px;flex-wrap:wrap;padding:16px 24px"></div>
      <div id="bench-progress" style="display:none;padding:8px 24px;color:#888;font-size:14px"></div>
      <div style="flex:1;overflow-y:auto;padding:0 24px 24px">
        <div id="bench-results">
          <p style="color:#888;padding:16px">
            ยังไม่มีข้อมูล — กด ▶ Run Benchmark เพื่อเริ่ม
          </p>
        </div>
      </div>
    </div>

    <div id="panel-code" class="panel" style="display:none">
      <div class="panel-header" style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
        <h2 style="margin:0;font-size:20px;font-weight:600">💻 Code Assistant</h2>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select id="code-folder-select" style="background:#2a2a2a;border:1px solid #444;border-radius:6px;padding:6px 12px;color:#e5e5e5;font-size:13px" onchange="loadFolder(this.value)">
            <option value="">-- หรือเลือก folder --</option>
            <option value="app/agents">📦 app/agents</option>
            <option value="app/core">⚙️ app/core</option>
            <option value="app/bot">🤖 app/bot</option>
            <option value="app">🏠 app (ทั้งหมด)</option>
          </select>
          <select id="code-file-select" style="background:#2a2a2a;border:1px solid #444;border-radius:6px;padding:6px 12px;color:#e5e5e5;font-size:13px;max-width:280px" onchange="loadCodeFile(this.value)">
            <option value="">-- หรือเลือก file --</option>
          </select>
          <select id="code-model-select" style="background:#2a2a2a;border:1px solid #7c3aed;border-radius:6px;padding:6px 12px;color:#e5e5e5;font-size:13px;font-weight:500">
            <option value="sonnet">🔴 Claude Sonnet — เก่งสุด (code)</option>
            <option value="haiku" selected>🟠 Claude Haiku — เร็ว ดี</option>
            <option value="deepseek-direct">💛 DeepSeek — reasoning ดี</option>
            <option value="gpt-4o">💛 GPT-4o — code ดีมาก</option>
            <option value="gpt-4o-mini">💚 GPT-4o-mini — เร็ว ถูก</option>
            <option value="groq">💚 Groq — เร็วสุด ฟรี</option>
            <option value="grok">⚡ Grok 3 — reasoning</option>
          </select>
          <button onclick="loadGitLog()" style="background:#1a1a1a;border:1px solid #444;border-radius:6px;padding:6px 12px;color:#aaa;font-size:13px;cursor:pointer">📋 Git Log</button>
        </div>
      </div>

      <div style="display:flex;flex:1;overflow:hidden;gap:0">

        <!-- Left: File viewer -->
        <div style="flex:1;display:flex;flex-direction:column;border-right:1px solid #2a2a2a;min-width:0">
          <div id="code-file-info" style="padding:8px 16px;font-size:12px;color:#888;border-bottom:1px solid #2a2a2a;min-height:32px"></div>
          <pre id="code-viewer" style="flex:1;overflow:auto;margin:0;padding:16px;font-family:'Fira Code',monospace;font-size:13px;line-height:1.6;color:#e5e5e5;white-space:pre;background:#0d0d0d">Select a file to view its content</pre>
        </div>

        <!-- Right: AI Chat + Git Log -->
        <div style="width:420px;display:flex;flex-direction:column;flex-shrink:0">

          <!-- Git Log -->
          <div id="git-log-panel" style="display:none;flex:0 0 200px;overflow-y:auto;border-bottom:1px solid #2a2a2a;padding:12px">
            <div style="font-size:12px;color:#888;margin-bottom:8px">📋 Recent Commits</div>
            <div id="git-log-list"></div>
          </div>

          <!-- AI Chat -->
          <div id="code-chat-messages" style="flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px"></div>

          <!-- Input -->
          <div style="padding:12px;border-top:1px solid #2a2a2a">
            <div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">
              <button onclick="askCodeAI('อธิบาย file นี้')" style="font-size:11px;padding:4px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#aaa;cursor:pointer">📖 อธิบาย</button>
              <button onclick="askCodeAI('หา bug ใน code นี้')" style="font-size:11px;padding:4px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#aaa;cursor:pointer">🐛 หา Bug</button>
              <button onclick="askCodeAI('สร้าง Cursor prompt เพื่อปรับปรุง code นี้')" style="font-size:11px;padding:4px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#aaa;cursor:pointer">⚡ Cursor Prompt</button>
            </div>
            <div style="display:flex;gap:8px">
              <input id="code-question-input" type="text" placeholder="ถามเกี่ยวกับ code..."
                     style="flex:1;background:#2a2a2a;border:1px solid #444;border-radius:8px;padding:10px 14px;color:#e5e5e5;font-size:14px;font-family:inherit"
                     onkeydown="if(event.key==='Enter')askCodeAI()">
              <button onclick="askCodeAI()" style="background:#7c3aed;color:white;border:none;border-radius:8px;padding:10px 16px;cursor:pointer;font-size:14px">↑</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div id="panel-system" class="panel">
      <div class="panel-header"><h2>⚙️ System Info</h2></div>
      <div id="system-content" class="panel-body"></div>
    </div>
  </main>
</div>

<div id="modal-overlay" style="display:none" onclick="closeModal()">
  <div id="modal-box" onclick="event.stopPropagation()">
    <h3>New Project</h3>
    <input id="proj-name-input" type="text" placeholder="Project name...">
    <button onclick="createProject()">Create</button>
  </div>
</div>

<div id="toast"></div>
<div id="slash-menu" style="display:none"></div>

<script>
document.addEventListener('DOMContentLoaded', function() {
  try {
  const state = {
    streaming: false,
    currentProject: null,
    projectName: 'All Chats',
    toastTimer: null,
  };

  const chatMessages = document.getElementById('chat-messages');
  const chatInput = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  const projectNav = document.getElementById('project-nav');
  const activeModelBadge = document.getElementById('active-model-badge');
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');
  const slashMenu = document.getElementById('slash-menu');
  const SLASH_COMMANDS = [
    { cmd: '/note', desc: 'บันทึกความคิด → BrainAgent' },
    { cmd: '/task', desc: 'สร้าง task ใหม่' },
    { cmd: '/tasks', desc: 'ดู task ทั้งหมด' },
    { cmd: '/standup', desc: 'สร้าง daily standup report' },
    { cmd: '/remember', desc: 'บันทึก long-term memory' },
    { cmd: '/memory', desc: 'ดู memory ทั้งหมด' },
    { cmd: '/think', desc: 'ถกไอเดีย 3 รอบ (brainstorm)' },
    { cmd: '/news', desc: 'ดูข่าว AI/Tech วันนี้' },
    { cmd: '/today', desc: 'สรุปวันนี้' },
    { cmd: '/tarot', desc: 'ดูดวงไพ่ทาโรต์' },
    { cmd: '/code', desc: 'เขียน/review code' },
    { cmd: '/content', desc: 'สร้าง caption/script' },
    { cmd: '/ener', desc: 'วิเคราะห์พระเครื่อง' },
    { cmd: '/learn', desc: 'บันทึกบทเรียน' },
    { cmd: '/help', desc: 'ดูคำสั่งทั้งหมด' },
  ];

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderMarkdown(text) {
    let html = escapeHtml(text || '');
    html = html.replace(/```([\\s\\S]*?)```/g, '<pre class="surface"><code>$1</code></pre>');
    html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
    html = html.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
    html = html.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
    html = html.replace(/(^|<br>)- (.+?)(?=(<br>|$))/g, '$1<li>$2</li>');
    html = html.replace(/(<li>.*?<\\/li>)/gs, '<ul>$1</ul>');
    html = html.replace(/(^|<br>)(\\d+)\\. (.+?)(?=(<br>|$))/g, '$1<li>$3</li>');
    html = html.replace(/(<li>.*?<\\/li>)/gs, (match) => match.includes('<ul>') ? match : '<ol>' + match + '</ol>');
    html = html.replace(/\\n/g, '<br>');
    return html;
  }

  function showToast(msg) {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.style.display = 'block';
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      toast.style.display = 'none';
    }, 3000);
  }

  function scrollToBottom() {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function setSendButtonState(loading) {
    sendBtn.disabled = loading;
    sendBtn.textContent = loading ? '...' : '↑';
  }

  function currentTimeLabel() {
    return new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  }

  function updateSlashMenu(value) {
    if (!value.startsWith('/')) {
      slashMenu.style.display = 'none';
      return;
    }
    const q = value.toLowerCase();
    const matches = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(q));
    if (matches.length === 0) {
      slashMenu.style.display = 'none';
      return;
    }
    slashMenu.innerHTML = matches.map((c, i) => `
      <div class="slash-item ${i === 0 ? 'selected' : ''}" onclick="selectSlash('${c.cmd}')">
        <span class="slash-cmd">${c.cmd}</span>
        <span class="slash-desc">${c.desc}</span>
      </div>
    `).join('');
    slashMenu.style.display = 'block';
    window._slashIndex = 0;
  }

  function selectSlash(cmd) {
    chatInput.value = cmd + ' ';
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + 'px';
    chatInput.focus();
    slashMenu.style.display = 'none';
  }

  function appendUserBubble(text, meta=`Web • ${currentTimeLabel()}`) {
    const row = document.createElement('div');
    row.className = 'msg-row user-row';
    row.innerHTML = `
      <div class="msg-bubble user-bubble">
        <div class="msg-text">${escapeHtml(text)}</div>
        <div class="msg-meta">${escapeHtml(meta)}</div>
      </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  function appendAiBubble(text, meta='Ener-AI') {
    const row = document.createElement('div');
    row.className = 'msg-row ai-row';
    row.innerHTML = `
      <div class="msg-bubble ai-bubble">
        <div class="msg-text">${renderMarkdown(text)}</div>
        <div class="msg-meta">${escapeHtml(meta)}</div>
      </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  function appendThinkingBubble(id) {
    const row = document.createElement('div');
    row.id = id;
    row.className = 'msg-row ai-row';
    row.innerHTML = `
      <div class="msg-bubble ai-bubble thinking">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
      </div>
    `;
    chatMessages.appendChild(row);
    scrollToBottom();
    return row;
  }

  async function api(url, options={}) {
    const response = await fetch(url, Object.assign({
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin'
    }, options));

    if (response.status === 307 || response.redirected) {
      window.location.href = '/admin/otp';
      throw new Error('Session expired');
    }

    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const data = await response.json();
        detail = data.detail || detail;
      } catch (error) {}
      throw new Error(detail);
    }

    const contentType = response.headers.get('content-type') || '';
    return contentType.includes('application/json') ? response.json() : response.text();
  }

  async function loadActiveModelBadge() {
    try {
      const data = await api('/admin/api/status');
      activeModelBadge.textContent = data.active_model_label || 'Auto / Active';
    } catch (error) {
      activeModelBadge.textContent = 'Auto / Active';
    }
  }

  function showPanel(name) {
    document.querySelectorAll('.panel').forEach((panel) => {
      panel.classList.remove('active-panel');
      panel.style.display = 'none';
    });
    const target = document.getElementById('panel-' + name);
    if (target) {
      target.classList.add('active-panel');
      target.style.display = 'flex';
    }

    document.querySelectorAll('#tool-nav .nav-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.panel === name);
    });

    if (name === 'chat') loadChatHistory();
    if (name === 'notes') loadNotes();
    if (name === 'tasks') loadTasks();
    if (name === 'standup') {
      loadStandupProjects();
      generateStandup();
    }
    if (name === 'news') loadNews();
    if (name === 'memory') loadMemory();
    if (name === 'files') loadFiles();
    if (name === 'system') loadSystem();
    if (name === 'benchmark') loadBenchmark();
    if (name === 'code') loadCodePanel();
  }

  function newChat() {
    chatMessages.innerHTML = '';
    state.currentProject = null;
    state.projectName = 'All Chats';
    window._currentProject = null;
    highlightProjectLink();
    showPanel('chat');
  }

  async function sendMessage() {
    const msg = chatInput.value.trim();
    if (!msg || state.streaming) return;

    chatInput.value = '';
    chatInput.style.height = 'auto';
    slashMenu.style.display = 'none';
    appendUserBubble(msg, `Web • ${currentTimeLabel()}`);

    const thinkingId = 'thinking-' + Date.now();
    appendThinkingBubble(thinkingId);

    state.streaming = true;
    setSendButtonState(true);

    let aiBubble = null;
    let fullText = '';

    try {
      const response = await fetch('/workspace/chat/stream', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({
          message: msg,
          project_id: window._currentProject || null,
          model: 'auto'
        })
      });

      if (!response.ok || !response.body) {
        throw new Error(`Request failed (${response.status})`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const {done, value} = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, {stream: true});
        const chunks = buffer.split('\\n\\n');
        buffer = chunks.pop() || '';

        for (const chunk of chunks) {
          const line = chunk.split('\\n').find((item) => item.startsWith('data: '));
          if (!line) continue;
          const data = JSON.parse(line.slice(6));

          if (data.type === 'start') {
            document.getElementById(thinkingId)?.remove();
            aiBubble = appendAiBubble('', 'Ener-AI');
          }
          if (data.type === 'token') {
            if (!aiBubble) aiBubble = appendAiBubble('', 'Ener-AI');
            fullText += data.text || '';
            aiBubble.querySelector('.msg-text').innerHTML = renderMarkdown(fullText);
            scrollToBottom();
          }
          if (data.type === 'done') {
            await loadProjects();
          }
          if (data.type === 'error') {
            document.getElementById(thinkingId)?.remove();
            showToast(data.text || 'Streaming error');
          }
        }
      }
    } catch (error) {
      document.getElementById(thinkingId)?.remove();
      appendAiBubble('Connection error. Please retry.', 'Ener-AI');
      showToast(error.message || 'Send failed');
    } finally {
      state.streaming = false;
      setSendButtonState(false);
      chatInput.focus();
    }
  }

  async function loadChatHistory() {
    const query = window._currentProject ? `?project_id=${window._currentProject}` : '';
    const data = await api(`/workspace/chat/history${query}`);
    chatMessages.innerHTML = '';
    const messages = data.messages || [];
    if (!messages.length) {
      chatMessages.innerHTML = '<div class="msg-row"><div class="empty-state">Start a conversation with Ener-AI.</div></div>';
      return;
    }
    messages.forEach((msg) => {
      const meta = `${msg.source === 'web' ? 'Web' : 'Telegram'} • ${msg.created_at || ''}`;
      if (msg.role === 'user') {
        appendUserBubble(msg.content || '', meta);
      } else {
        appendAiBubble(msg.content || '', meta);
      }
    });
    scrollToBottom();
  }

  function highlightProjectLink() {
    document.querySelectorAll('#project-nav .project-link').forEach((link) => {
      const projectId = link.dataset.projectId ? Number(link.dataset.projectId) : null;
      const active = projectId === state.currentProject;
      link.classList.toggle('active', active);
      link.classList.toggle('active-project', active);
    });
  }

  async function loadProjects() {
    const data = await api('/workspace/projects');
    const projects = data.projects || [];
    const items = [
      {
        id: null,
        name: 'All Chats',
        count: data.total_messages || 0,
        lastActive: ''
      },
      ...projects.map((project) => ({
        id: project.id,
        name: project.name,
        count: project.message_count || 0,
        lastActive: project.last_active || ''
      }))
    ];

    projectNav.innerHTML = items.map((project) => `
      <a class="nav-item project-link" data-project-id="${project.id ?? ''}">
        ${escapeHtml(project.name)}
        <span class="project-meta">${project.count} messages${project.lastActive ? ' • ' + escapeHtml(project.lastActive) : ''}</span>
      </a>
    `).join('');

    projectNav.querySelectorAll('.project-link').forEach((link) => {
      link.addEventListener('click', () => {
        const rawId = link.dataset.projectId;
        const projectId = rawId ? Number(rawId) : null;
        const projectName = link.childNodes[0]?.textContent?.trim() || 'All Chats';
        selectProject(projectId, projectName);
      });
    });

    highlightProjectLink();
  }

  function selectProject(id, name) {
    state.currentProject = id;
    state.projectName = name || 'All Chats';
    window._currentProject = id;
    highlightProjectLink();
    chatMessages.innerHTML = '';
    loadChatHistory();
    showPanel('chat');
  }

  function showNewProjectModal() {
    document.getElementById('modal-overlay').style.display = 'flex';
    document.getElementById('proj-name-input').focus();
  }

  function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    document.getElementById('proj-name-input').value = '';
  }

  async function createProject() {
    const input = document.getElementById('proj-name-input');
    const name = input.value.trim();
    if (!name) {
      showToast('Project name required');
      return;
    }

    try {
      await api('/workspace/projects/create', {
        method: 'POST',
        body: JSON.stringify({name})
      });
      await loadProjects();
      closeModal();
      showToast('Project created');
    } catch (error) {
      showToast(error.message || 'Create project failed');
    }
  }

  async function loadNotes() {
    const data = await api('/workspace/notes');
    const notes = data.notes || [];
    const grouped = {};
    notes.forEach((note) => {
      const category = note.category || 'note';
      if (!grouped[category]) grouped[category] = [];
      grouped[category].push(note);
    });

    const list = document.getElementById('notes-list');
    if (!notes.length) {
      list.innerHTML = '<div class="empty-state">No notes yet.</div>';
      return;
    }

    list.innerHTML = Object.entries(grouped).map(([category, items]) => `
      <div class="notes-group">
        <h3>${escapeHtml(category)}</h3>
        ${items.map((note) => `
          <details class="note-card">
            <summary>${escapeHtml(note.ai_summary || (note.content || '').slice(0, 120))}</summary>
            <div class="note-meta">${escapeHtml(note.created_at || '')}</div>
            <div style="margin-top:10px;">${renderMarkdown(note.content || '')}</div>
          </details>
        `).join('')}
      </div>
    `).join('');
  }

  async function saveNote() {
    const input = document.getElementById('note-input');
    const value = input.value.trim();
    if (!value) return;

    try {
      await api('/workspace/notes/save', {
        method: 'POST',
        body: JSON.stringify({text: value})
      });
      input.value = '';
      await loadNotes();
      showToast('Note saved');
    } catch (error) {
      showToast(error.message || 'Save note failed');
    }
  }

  async function loadTasks() {
    const data = await api('/workspace/tasks');
    const tasks = data.tasks || [];
    const grouped = {open: [], in_progress: [], done: []};
    tasks.forEach((task) => {
      const status = task.status || 'open';
      if (!grouped[status]) grouped[status] = [];
      grouped[status].push(task);
    });

    const labels = {
      open: 'Open',
      in_progress: 'In Progress',
      done: 'Done'
    };

    const list = document.getElementById('tasks-list');
    list.innerHTML = Object.entries(grouped).map(([status, items]) => `
      <div class="task-group">
        <h3>${labels[status] || status}</h3>
        <div class="surface">
          ${items.length ? items.map((task) => `
            <label class="task-item">
              <input type="checkbox" ${task.status === 'done' ? 'checked' : ''} data-task-id="${task.id}">
              <div>
                <div>${escapeHtml(task.title || '')}</div>
                <div class="task-meta">${escapeHtml(task.deadline_hint || '')}</div>
                <div class="priority-badge priority-${escapeHtml(task.priority || 'medium')}">${escapeHtml(task.priority_badge || '')} ${escapeHtml(task.priority || 'medium')}</div>
              </div>
            </label>
          `).join('') : '<div class="empty-state">No tasks in this group.</div>'}
        </div>
      </div>
    `).join('');

    list.querySelectorAll('input[type="checkbox"][data-task-id]').forEach((checkbox) => {
      checkbox.addEventListener('change', async () => {
        const taskId = checkbox.dataset.taskId;
        try {
          await api(`/workspace/tasks/${taskId}/done`, {method: 'POST'});
          await loadTasks();
        } catch (error) {
          showToast(error.message || 'Update task failed');
        }
      });
    });
  }

  async function createTask() {
    const input = document.getElementById('task-input');
    const priority = document.getElementById('task-priority');
    const title = input.value.trim();
    if (!title) return;

    try {
      await api('/workspace/tasks/create', {
        method: 'POST',
        body: JSON.stringify({
          title,
          priority: priority.value || 'medium'
        })
      });
      input.value = '';
      priority.value = 'medium';
      await loadTasks();
      showToast('Task created');
    } catch (error) {
      showToast(error.message || 'Create task failed');
    }
  }

  async function generateStandup() {
    const preview = document.getElementById('standup-preview');
    if (!preview) return;
    try {
      const data = await api('/workspace/standup/preview');
      preview.textContent = data.report || '-';
      showToast('Report generated! ✅');
    } catch (error) {
      showToast(error.message || 'Generate standup failed');
    }
  }

  async function copyStandupReport() {
    const preview = document.getElementById('standup-preview');
    const text = (preview?.textContent || '').trim();
    if (!text) {
      showToast('ยังไม่มี report ให้ copy');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      showToast('Copied ✅');
    } catch (error) {
      showToast('Copy failed');
    }
  }

  async function loadStandupProjects() {
    const data = await api('/workspace/standup/projects');
    const projects = data.projects || data || [];
    const el = document.getElementById('standup-projects');
    el.innerHTML = '<h3 style="margin:0 0 12px">📊 Projects</h3>' + projects.map((p) => `
      <div class="standup-project-card">
        <div class="sp-name">${escapeHtml(p.name || '')}</div>
        <div class="sp-row">
          <label>% เสร็จ</label>
          <input type="number" value="${Number(p.percent_complete || 0)}" min="0" max="100"
            onchange="updateProject(${Number(p.id)}, 'percent_complete', this.value)">
        </div>
        <div class="sp-row">
          <label>Status</label>
          <input type="text" value="${escapeHtml(p.current_status || '')}"
            onblur="updateProject(${Number(p.id)}, 'current_status', this.value)">
        </div>
        <div class="sp-row">
          <label>Due</label>
          <input type="text" value="${escapeHtml(p.due_date || '')}"
            onblur="updateProject(${Number(p.id)}, 'due_date', this.value)">
        </div>
        <div class="sp-row">
          <label>วันนี้ทำ</label>
          <textarea rows="2"
            onblur="updateProject(${Number(p.id)}, 'today_tasks', this.value)">${escapeHtml(p.today_tasks || '')}</textarea>
        </div>
      </div>
    `).join('');
  }

  async function updateProject(id, field, value) {
    try {
      await api('/workspace/standup/projects/' + id + '/update', {
        method: 'POST',
        body: JSON.stringify({field, value})
      });
      showToast('Saved ✅');
    } catch (error) {
      showToast(error.message || 'Save failed');
    }
  }

  async function runBrainstorm() {
    const input = document.getElementById('brainstorm-input');
    const topic = input.value.trim();
    if (!topic) return;

    const result = document.getElementById('brainstorm-result');
    result.innerHTML = '<div class="surface">Thinking...</div>';

    try {
      const data = await api('/workspace/brainstorm', {
        method: 'POST',
        body: JSON.stringify({topic})
      });

      const rounds = data.rounds || [];
      const latest = rounds[rounds.length - 1] || {};
      result.innerHTML = `
        <div class="brain-grid">
          <div class="brain-card"><h4>AI_A</h4><div>${renderMarkdown(latest.ai_a || '')}</div></div>
          <div class="brain-card"><h4>AI_B</h4><div>${renderMarkdown(latest.ai_b || '')}</div></div>
          <div class="brain-card"><h4>AI_C</h4><div>${renderMarkdown(latest.ai_c || '')}</div></div>
        </div>
        <div class="verdict-card">
          <h4>Verdict</h4>
          <div>${escapeHtml(data.verdict || '-')}</div>
          <div class="task-meta">${escapeHtml(data.reason || '')}</div>
          ${data.raw ? `<details style="margin-top:12px;"><summary>Raw output</summary><div style="margin-top:10px;">${renderMarkdown(data.raw)}</div></details>` : ''}
        </div>
      `;
    } catch (error) {
      result.innerHTML = '<div class="surface">Brainstorm failed.</div>';
      showToast(error.message || 'Brainstorm failed');
    }
  }

  async function loadNews() {
    const data = await api('/workspace/news');
    const items = data.news || [];
    const list = document.getElementById('news-list');
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">No news loaded yet.</div>';
      return;
    }
    list.innerHTML = items.map((item) => `
      <div class="news-card">
        <div><strong>${escapeHtml(item.title || '')}</strong></div>
        <div class="news-meta">${escapeHtml(item.source || '')} ${item.fetched_at ? '• ' + escapeHtml(item.fetched_at) : ''}</div>
        <div style="margin-top:10px;">${escapeHtml(item.summary || '')}</div>
        ${item.url ? `<div class="row-actions"><a class="secondary-btn" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">Open</a></div>` : ''}
      </div>
    `).join('');
  }

  async function fetchNews() {
    try {
      showToast('Fetching latest news...');
      await api('/workspace/news/fetch', {method: 'POST'});
      await loadNews();
      showToast('News updated');
    } catch (error) {
      showToast(error.message || 'Fetch news failed');
    }
  }

  async function loadMemory() {
    const data = await api('/workspace/memory');
    const items = data.memories || [];
    const list = document.getElementById('memory-list');
    if (!items.length) {
      list.innerHTML = '<div class="empty-state">No long-term memories yet.</div>';
      return;
    }
    list.innerHTML = items.map((item) => `
      <div class="memory-card">
        <div>${renderMarkdown(item.content || '')}</div>
        <div class="memory-meta">${escapeHtml(item.memory_type || 'general')} ${item.created_at ? '• ' + escapeHtml(item.created_at) : ''}</div>
      </div>
    `).join('');
  }

  async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file);
    await fetch('/workspace/files/upload', {
      method: 'POST',
      body: formData,
      credentials: 'same-origin'
    }).then((response) => {
      if (!response.ok) throw new Error('Upload failed');
      return response.json();
    });
  }

  async function summarizeFile(fileId) {
    try {
      await api(`/workspace/files/${fileId}/summarize`, {method: 'POST'});
      await loadFiles();
      showToast('Summary ready');
    } catch (error) {
      showToast(error.message || 'Summarize failed');
    }
  }

  async function askFile(fileId) {
    const question = window.prompt('Ask about this file');
    if (!question) return;
    try {
      const data = await api(`/workspace/files/${fileId}/ask`, {
        method: 'POST',
        body: JSON.stringify({question})
      });
      showPanel('chat');
      appendAiBubble(data.answer || '', 'Ener-AI • file answer');
      showToast('Answer added to chat');
    } catch (error) {
      showToast(error.message || 'Ask file failed');
    }
  }

  async function loadFiles() {
    const data = await api('/workspace/files');
    const files = data.files || [];
    const list = document.getElementById('files-list');
    if (!files.length) {
      list.innerHTML = '<div class="empty-state">No uploaded files yet.</div>';
      return;
    }
    list.innerHTML = files.map((file) => `
      <div class="file-card">
        <div><strong>${escapeHtml(file.filename || '')}</strong></div>
        <div class="file-meta">${escapeHtml(String(file.size_bytes || 0))} bytes ${file.created_at ? '• ' + escapeHtml(file.created_at) : ''}</div>
        ${file.summary ? `<div style="margin-top:10px;">${renderMarkdown(file.summary)}</div>` : ''}
        <div class="file-actions">
          <button class="file-action" onclick="summarizeFile(${file.id})">Summarize</button>
          <button class="secondary-btn" onclick="askFile(${file.id})">Ask</button>
        </div>
      </div>
    `).join('');
  }

  async function loadSystem() {
    const container = document.getElementById('system-content');
    if (!container) return;
    container.innerHTML = '<div class="empty-state">Loading system info...</div>';
    try {
      const data = await api('/workspace/system/info');
      const pipelineData = await api('/admin/pipeline-metrics');
      const stats = data.stats || {};
      const agents = data.agents || [];
      const scheduler = data.scheduler || [];
      const averages = pipelineData.averages || [];
      const recent = pipelineData.recent || [];
      const statsHtml = averages.length
        ? averages.map((item) => `
            <div class="sys-card">
              <div class="sys-label">🤖 ${escapeHtml(item.model_used || '-')}</div>
              <div class="sys-value">${Math.round(Number(item.avg_total || 0))}ms</div>
              <div style="font-size:12px;color:#666;margin-top:6px">
                Router: ${Math.round(Number(item.avg_router || 0))}ms |
                Reason: ${Math.round(Number(item.avg_reasoner || 0))}ms |
                Check: ${Math.round(Number(item.avg_checker || 0))}ms
              </div>
              <div style="font-size:11px;color:#888">${Number(item.count || 0).toLocaleString()} requests</div>
            </div>
          `).join('')
        : '<div class="empty-state">ยังไม่มี pipeline metrics ใน 24 ชั่วโมงล่าสุด</div>';
      const rows = recent.length
        ? recent.map((item) => {
            const totalMs = Number(item.total_ms || 0);
            const totalColor = totalMs > 3000 ? '#ef4444' : totalMs > 1500 ? '#f59e0b' : '#22c55e';
            const timeLabel = item.created_at ? escapeHtml(String(item.created_at).split(' ')[1] || String(item.created_at)) : '-';
            return `<tr style="border-bottom:1px solid #222;font-size:13px">
              <td style="padding:8px;color:#888">${timeLabel}</td>
              <td style="padding:8px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                ${escapeHtml(item.question_preview || '-')}
              </td>
              <td style="padding:8px">
                <span style="background:#2a2a2a;padding:2px 8px;border-radius:12px;font-size:11px">
                  ${escapeHtml(item.model_used || '-')}
                </span>
              </td>
              <td style="padding:8px;text-align:right;color:#888">${Number(item.router_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right">${Number(item.reasoner_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right;color:#888">${Number(item.checker_ms || 0)}ms</td>
              <td style="padding:8px;text-align:right;font-weight:600;color:${totalColor}">
                ${totalMs}ms
              </td>
              <td style="padding:8px;text-align:center">
                ${item.was_fixed ? '🔧' : '✅'}
              </td>
            </tr>`;
          }).join('')
        : '<tr><td colspan="8" class="empty-state" style="padding:12px 8px;">ยังไม่มี recent requests</td></tr>';
      container.innerHTML = `
        <div class="sys-grid">
          <div class="sys-card">
            <div class="sys-label">🤖 Active Model</div>
            <div class="sys-value">${escapeHtml(data.model || '-')}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">📦 Agents</div>
            <div class="sys-value">${Number(data.agent_count || 0).toLocaleString()} ตัว</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">💬 Messages</div>
            <div class="sys-value">${Number(stats.messages || 0).toLocaleString()}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">✅ Tasks (open)</div>
            <div class="sys-value">${Number(stats.open_tasks || 0).toLocaleString()} / ${Number(stats.tasks || 0).toLocaleString()}</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">🧠 Memories</div>
            <div class="sys-value">${Number(stats.memories || 0).toLocaleString()} + ${Number(stats.long_term_memories || 0).toLocaleString()} LT</div>
          </div>
          <div class="sys-card">
            <div class="sys-label">📝 Notes</div>
            <div class="sys-value">${Number(stats.notes || 0).toLocaleString()}</div>
          </div>
        </div>
        <h3 style="margin:24px 0 12px">⏰ Scheduler</h3>
        <div class="sched-list">
          ${scheduler.map((item) => `
            <div class="sched-item">
              <span class="sched-time">${escapeHtml(item.time || '')}</span>
              <span class="sched-job">${escapeHtml(item.job || '')}</span>
            </div>
          `).join('')}
        </div>
        <h3 style="margin:24px 0 12px">⚡ Pipeline Response Times (24h)</h3>
        <div id="pipeline-stats">${statsHtml}</div>
        <h3 style="margin:24px 0 12px">📋 Recent Requests</h3>
        <table id="pipeline-table" style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="font-size:11px;color:#888;text-transform:uppercase">
              <th style="padding:8px;text-align:left">Time</th>
              <th style="padding:8px;text-align:left">Question</th>
              <th style="padding:8px;text-align:left">Model</th>
              <th style="padding:8px;text-align:right">Router</th>
              <th style="padding:8px;text-align:right">Reasoner</th>
              <th style="padding:8px;text-align:right">Checker</th>
              <th style="padding:8px;text-align:right">Total</th>
              <th style="padding:8px;text-align:center">Fixed?</th>
            </tr>
          </thead>
          <tbody id="pipeline-tbody">${rows}</tbody>
        </table>
        <h3 style="margin:24px 0 12px">📦 Agents (${Number(data.agent_count || 0).toLocaleString()})</h3>
        <div class="agent-chips">
          ${agents.map((agent) => `<span class="agent-chip">${escapeHtml(agent || '')}</span>`).join('')}
        </div>
      `;
    } catch (error) {
      container.innerHTML = '<div class="empty-state">โหลด system info ไม่สำเร็จ</div>';
      showToast(error.message || 'Load system info failed');
    }
  }

  function dropZoneClick() {
    fileInput.click();
  }

  dropZone.addEventListener('click', dropZoneClick);
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    try {
      await uploadFile(file);
      fileInput.value = '';
      await loadFiles();
      showToast('File uploaded');
    } catch (error) {
      showToast(error.message || 'Upload failed');
    }
  });

  dropZone.addEventListener('dragover', (event) => {
    event.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
  });

  dropZone.addEventListener('drop', async (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragover');
    const file = event.dataTransfer.files[0];
    if (!file) return;
    try {
      await uploadFile(file);
      await loadFiles();
      showToast('File uploaded');
    } catch (error) {
      showToast(error.message || 'Upload failed');
    }
  });

  chatInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 200) + 'px';
    updateSlashMenu(this.value);
  });

  chatInput.addEventListener('keydown', function(e) {
    const items = slashMenu.querySelectorAll('.slash-item');
    if (slashMenu.style.display !== 'none' && items.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        window._slashIndex = Math.min((window._slashIndex || 0) + 1, items.length - 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        window._slashIndex = Math.max((window._slashIndex || 0) - 1, 0);
      } else if (e.key === 'Tab' || (e.key === 'Enter' && slashMenu.style.display !== 'none' && this.value.startsWith('/'))) {
        e.preventDefault();
        const selected = items[window._slashIndex || 0];
        if (selected) selected.click();
        return;
      } else if (e.key === 'Escape') {
        slashMenu.style.display = 'none';
        return;
      }
      items.forEach((el, i) => el.classList.toggle('selected', i === (window._slashIndex || 0)));
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  document.addEventListener('click', function(e) {
    if (!e.target.closest('#slash-menu') && !e.target.closest('#chat-input')) {
      slashMenu.style.display = 'none';
    }
  });

  document.getElementById('proj-name-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      createProject();
    }
  });

  // ── Code Assistant ───────────────────────────────────────────────────────────
  let _codeCurrentFile = "";
  let _codeCurrentContent = "";
  let _codeFolderData = null;

  async function loadCodePanel() {
    try {
      const res = await fetch('/workspace/code/files');
      const d = await res.json();
      const sel = document.getElementById('code-file-select');
      if (sel) {
        sel.innerHTML = '<option value="">-- Select file --</option>' +
          d.files.map(f => `<option value="${f}">${f}</option>`).join('');
      }
    } catch(e) { console.log('loadCodePanel error:', e); }
  }

  async function loadCodeFile(path) {
    if (!path) return;
    _codeFolderData = null;  // clear folder context when switching to file
    const folderSel = document.getElementById('code-folder-select');
    if (folderSel) folderSel.value = '';
    _codeCurrentFile = path;
    const viewer = document.getElementById('code-viewer');
    const info = document.getElementById('code-file-info');
    if (viewer) viewer.textContent = 'Loading...';
    try {
      const res = await fetch('/workspace/code/file?path=' + encodeURIComponent(path));
      const d = await res.json();
      _codeCurrentContent = d.content || '';
      if (viewer) viewer.textContent = d.content;
      if (info) info.textContent = `${path} · ${d.lines} lines · ${d.size} bytes`;
    } catch(e) {
      if (viewer) viewer.textContent = 'Error loading file: ' + e.message;
    }
  }

  async function loadFolder(folderPath) {
    if (!folderPath) return;
    _codeCurrentFile = "";
    _codeCurrentContent = "";
    _codeFolderData = null;
    const viewer = document.getElementById('code-viewer');
    const info = document.getElementById('code-file-info');
    // Reset file select
    const fileSel = document.getElementById('code-file-select');
    if (fileSel) fileSel.value = '';
    if (viewer) viewer.textContent = 'Loading folder...';
    try {
      const res = await fetch('/workspace/code/folder?path=' + encodeURIComponent(folderPath));
      const d = await res.json();
      _codeFolderData = d;
      const summary = d.files.map(f =>
        '# ' + f.path + ' (' + f.lines + ' lines)\\n' + f.preview + '\\n' + '='.repeat(60)
      ).join('\\n\\n');
      if (viewer) viewer.textContent = summary || '(no .py files found)';
      if (info) info.textContent = `📁 ${folderPath} · ${d.file_count} files · ${d.total_lines} lines total`;
      const msgs = document.getElementById('code-chat-messages');
      if (msgs) {
        msgs.innerHTML += `<div style="color:#888;font-size:12px;padding:4px;text-align:center">📁 โหลด ${d.file_count} files จาก ${folderPath} แล้ว — ถามได้เลย</div>`;
        msgs.scrollTop = msgs.scrollHeight;
      }
    } catch(e) {
      if (viewer) viewer.textContent = 'Error: ' + e.message;
    }
  }

  async function loadGitLog() {
    const panel = document.getElementById('git-log-panel');
    const list = document.getElementById('git-log-list');
    if (!panel || !list) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
    if (panel.style.display === 'none') return;
    list.innerHTML = 'Loading...';
    try {
      const res = await fetch('/workspace/code/git-log');
      const d = await res.json();
      list.innerHTML = d.commits.map(c => `
        <div style="padding:6px 0;border-bottom:1px solid #222;font-size:12px">
          <span style="color:#7c3aed;font-family:monospace">${c.hash}</span>
          <span style="color:#e5e5e5;margin-left:8px">${c.message}</span>
          <div style="color:#666;margin-top:2px">${c.time} · ${c.author}</div>
        </div>
      `).join('') || 'No commits found';
    } catch(e) {
      list.textContent = 'Error: ' + e.message;
    }
  }

  async function askCodeAI(preset) {
    const input = document.getElementById('code-question-input');
    const question = preset || (input ? input.value.trim() : '');
    if (!question) return;
    if (input && !preset) input.value = '';
    const modelSelect = document.getElementById('code-model-select');
    const selectedModel = modelSelect ? modelSelect.value : 'haiku';
    const msgs = document.getElementById('code-chat-messages');
    if (!msgs) return;
    msgs.innerHTML += `<div style="align-self:flex-end;background:#2f2f2f;padding:8px 12px;border-radius:12px;font-size:14px;max-width:85%">${question}</div>`;
    const thinkId = 'think-' + Date.now();
    msgs.innerHTML += `<div id="${thinkId}" style="color:#888;font-size:13px;padding:4px">💭 กำลังวิเคราะห์ด้วย ${selectedModel}...</div>`;
    msgs.scrollTop = msgs.scrollHeight;
    try {
      const res = await fetch('/workspace/code/chat', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          question,
          file_path: _codeCurrentFile,
          file_content: _codeCurrentContent.substring(0, 8000),
          folder_data: _codeFolderData,
          model: selectedModel
        })
      });
      const d = await res.json();
      const el = document.getElementById(thinkId);
      if (el) el.remove();
      const answerId = 'ans-' + Date.now();
      msgs.innerHTML += `
        <div id="${answerId}" style="align-self:flex-start;background:#1a1a1a;padding:12px;border-radius:12px;font-size:14px;max-width:90%;border-left:3px solid #7c3aed">
          <div style="font-size:11px;color:#7c3aed;margin-bottom:6px">🤖 ${selectedModel.toUpperCase()}</div>
          <div style="line-height:1.7">${(d.answer||'').replace(/\\n/g,'<br>')}</div>
          <button onclick="saveCodeMemory('${answerId}')" style="margin-top:8px;font-size:11px;padding:3px 10px;background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#888;cursor:pointer">💾 บันทึกใน Memory</button>
        </div>`;
      msgs.scrollTop = msgs.scrollHeight;
    } catch(e) {
      const el = document.getElementById(thinkId);
      if (el) el.outerHTML = `<div style="color:#ef4444;font-size:13px">Error: ${e.message}</div>`;
    }
  }

  async function saveCodeMemory(answerId) {
    const el = document.getElementById(answerId);
    if (!el) return;
    const text = el.querySelector('div').textContent;
    const content = (_codeCurrentFile ? '[' + _codeCurrentFile + '] ' : '') + text.substring(0, 500);
    try {
      await fetch('/workspace/code/remember', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({content})
      });
      const btn = el.querySelector('button');
      if (btn) { btn.textContent = '✅ บันทึกแล้ว'; btn.disabled = true; }
    } catch(e) {
      if (typeof showToast === 'function') showToast('❌ Save failed');
    }
  }

  window.loadCodePanel = loadCodePanel;
  window.loadCodeFile = loadCodeFile;
  window.loadFolder = loadFolder;
  window.loadGitLog = loadGitLog;
  window.askCodeAI = askCodeAI;
  window.saveCodeMemory = saveCodeMemory;

  window.showPanel = showPanel;
  window.newChat = newChat;
  window.sendMessage = sendMessage;
  window.showNewProjectModal = showNewProjectModal;
  window.closeModal = closeModal;
  window.createProject = createProject;
  window.selectProject = selectProject;
  window.saveNote = saveNote;
  window.createTask = createTask;
  window.generateStandup = generateStandup;
  window.copyStandupReport = copyStandupReport;
  window.updateProject = updateProject;
  window.runBrainstorm = runBrainstorm;
  window.fetchNews = fetchNews;
  window.summarizeFile = summarizeFile;
  window.askFile = askFile;
  window.dropZoneClick = dropZoneClick;
  window.selectSlash = selectSlash;
  window.showToast = showToast;
  window.api = api;
  window.escapeHtml = escapeHtml;
  window.renderMarkdown = renderMarkdown;

  window._currentProject = null;
  loadActiveModelBadge();
  showPanel('chat');
  loadProjects();
  loadChatHistory();
  } catch(err) {
    console.error('WORKSPACE JS ERROR:', err);
    const contentEl = document.getElementById('content');
    if (contentEl) {
      contentEl.innerHTML =
        '<div style="color:#ef4444;padding:32px;font-family:monospace">' +
        '<h2>⚠️ JavaScript Error</h2>' +
        '<pre style="background:#1a1a1a;padding:16px;border-radius:8px;' +
        'overflow:auto;font-size:13px">' +
        (err.stack || String(err)).replace(/</g, '&lt;') + '</pre>' +
        '<p style="color:#888">แจ้ง admin เพื่อแก้ไข</p></div>';
    }
  }
});

const TEST_QUESTION_IDS = {
  it: ['it_01', 'it_02', 'it_03'],
  en: ['en_01', 'en_02', 'en_03'],
  hal: ['hal_01', 'hal_02', 'hal_03'],
  ch: ['ch_01', 'ch_02', 'ch_03'],
};

function _benchEscapeHtml(text) {
  if (typeof window.escapeHtml === 'function') return window.escapeHtml(text);
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function _benchRenderMarkdown(text) {
  if (typeof window.renderMarkdown === 'function') return window.renderMarkdown(text);
  return _benchEscapeHtml(text || '').replace(/\\n/g, '<br>');
}

function renderBenchSummary(stats) {
  const COLORS = {groq: '#22c55e', gemini: '#3b82f6', haiku: '#a855f7', 'deepseek-r1': '#f59e0b'};
  const container = document.getElementById('bench-summary');
  if (!container) return;
  if (!stats.length) {
    container.innerHTML = '<div class="empty-state">No benchmark data yet.</div>';
    return;
  }
  const html = stats.map((s) => `
    <div class="sys-card" style="min-width:160px">
      <div class="sys-label" style="color:${COLORS[s.model] || '#888'}">
        ${_benchEscapeHtml(String(s.model || '').toUpperCase())}
      </div>
      <div class="sys-value">${Math.round(Number(s.avg_ms || 0))}ms</div>
      <div style="font-size:12px;color:#888;margin-top:4px">
        ${Number(s.runs || 0)} runs
        ${s.avg_rating ? ` · ⭐ ${parseFloat(s.avg_rating).toFixed(1)}` : ''}
      </div>
    </div>
  `).join('');
  container.innerHTML = html;
}

function groupByQuestion(rows) {
  const groups = {};
  for (const row of rows) {
    if (!groups[row.question_id]) {
      groups[row.question_id] = {
        question_id: row.question_id,
        category: row.category,
        question: row.question,
        models: {},
      };
    }
    const current = groups[row.question_id].models[row.model];
    if (!current || Number(row.id || row.db_id || 0) > Number(current.id || current.db_id || 0)) {
      groups[row.question_id].models[row.model] = row;
    }
  }
  return Object.values(groups);
}

function renderBenchResults(groups) {
  const MODELS = ['groq', 'gemini', 'haiku'];
  const container = document.getElementById('bench-results');
  if (!container) return;
  const html = groups.map((g) => `
    <div style="background:#1a1a1a;border-radius:10px;margin-bottom:16px;overflow:hidden">
      <div style="padding:12px 16px;background:#222;border-bottom:1px solid #333">
        <span style="font-size:11px;color:#888;margin-right:8px">
          ${_benchEscapeHtml(g.category || '')}
        </span>
        <span style="font-size:14px;font-weight:500">${_benchEscapeHtml(g.question || '')}</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(${MODELS.length},1fr);gap:1px;background:#333">
        ${MODELS.map((m) => {
          const r = g.models[m];
          if (!r) return `<div style="padding:12px;background:#1a1a1a;color:#555;font-size:13px">-</div>`;
          const latencyMs = Number(r.latency_ms || 0);
          const color = latencyMs > 3000 ? '#ef4444' : latencyMs > 1500 ? '#f59e0b' : '#22c55e';
          const resultId = Number(r.id || r.db_id || 0);
          const stars = [1, 2, 3, 4, 5].map((n) =>
            `<span onclick="rateBenchmark(${resultId}, ${n})"
                   style="cursor:pointer;font-size:16px;color:${(Number(r.rating || 0) >= n) ? '#f59e0b' : '#444'}">★</span>`
          ).join('');
          return `
            <div style="padding:12px 16px;background:#1a1a1a">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:12px;font-weight:600;color:${color}">${_benchEscapeHtml(m)}</span>
                <span style="font-size:11px;color:${color}">
                  ${r.error ? '❌ error' : latencyMs + 'ms'}
                </span>
              </div>
              <div style="font-size:13px;line-height:1.6;color:#ccc;max-height:120px;overflow-y:auto">
                ${_benchRenderMarkdown(r.error || r.answer || '-')}
              </div>
              <div style="margin-top:8px">${stars}</div>
            </div>`;
        }).join('')}
      </div>
    </div>
  `).join('');
  container.innerHTML = html || '<p style="color:#888;padding:16px">No results yet. Click Run Benchmark.</p>';
}

async function loadBenchmark() {
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 5000);
    const res = await fetch('/workspace/benchmark/summary', {
      signal: ctrl.signal,
      credentials: 'same-origin',
    });
    clearTimeout(timeout);
    if (!res.ok) return;
    const data = await res.json();
    if (data.model_stats && data.model_stats.length > 0) {
      renderBenchSummary(data.model_stats);
    }
    if (data.recent && data.recent.length > 0) {
      renderBenchResults(groupByQuestion(data.recent));
    }
  } catch (error) {
    console.log('benchmark summary load failed:', error.message);
  }
}

async function runBenchmark() {
  const btn = document.getElementById('bench-run-btn');
  if (!btn || btn.disabled) return;

  btn.disabled = true;
  btn.textContent = '⏳ Running...';

  const prog = document.getElementById('bench-progress');
  if (prog) prog.style.display = 'block';

  const resultsEl = document.getElementById('bench-results');
  if (resultsEl) {
    resultsEl.innerHTML = '<p style="color:#888;padding:24px">⏳ Running benchmark — may take 60-120 seconds...</p>';
  }

  try {
    const cat = document.getElementById('bench-category')?.value || '';
    const ids = cat ? TEST_QUESTION_IDS[cat] : null;

    const res = await fetch('/workspace/benchmark/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({question_ids: ids}),
    });

    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();

    const flat = [];
    for (const q of data.results || []) {
      for (const r of q.results || []) {
        flat.push({
          question_id: q.question_id,
          category: q.category,
          question: q.question,
          id: r.db_id || r.id || (Date.now() + Math.random()),
          db_id: r.db_id || null,
          model: r.model,
          answer: r.answer,
          latency_ms: r.latency_ms,
          rating: r.rating || 0,
          error: r.error,
        });
      }
    }
    renderBenchResults(groupByQuestion(flat));
    await loadBenchmark();
    if (typeof window.showToast === 'function') window.showToast('✅ Benchmark complete!');
  } catch (error) {
    if (typeof window.showToast === 'function') window.showToast('❌ Error: ' + error.message);
    if (resultsEl) {
      resultsEl.innerHTML = '<p style="color:#ef4444;padding:24px">❌ ' + _benchEscapeHtml(error.message) + '</p>';
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '▶ Run Benchmark';
    if (prog) prog.style.display = 'none';
  }
}

async function rateBenchmark(id, rating) {
  const apiFn = typeof window.api === 'function' ? window.api : null;
  if (apiFn) {
    await apiFn('/workspace/benchmark/rate', {
      method: 'POST',
      body: JSON.stringify({id, rating}),
    });
  } else {
    await fetch('/workspace/benchmark/rate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({id, rating}),
    });
  }
  if (typeof window.showToast === 'function') window.showToast(`⭐ Rated ${rating}/5`);
  await loadBenchmark();
}
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


@app.get("/workspace")
async def workspace_page(request: Request):
    await _require_admin(request)
    return build_workspace_html()


@app.post("/workspace/chat/send")
async def workspace_chat_send(request: Request):
    await _require_admin(request)
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="กรุณาพิมพ์ข้อความ")
    project_id = _normalize_project_id(payload.get("project_id"))
    model = str(payload.get("model", "")).strip().lower() or None
    reply = await _workspace_generate_reply(text, project_id, model)
    return JSONResponse({"ok": True, "reply": reply})


@app.post("/workspace/chat/stream")
async def workspace_chat_stream(request: Request):
    await _require_admin(request)
    body = await request.json()
    message = str(body.get("message", body.get("text", ""))).strip()
    project_id = _normalize_project_id(body.get("project_id"))
    model = str(body.get("model", "auto")).strip().lower() or "auto"

    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    from app.agents.chat import _build_system_prompt
    from app.core.ai import stream_chat_response
    from app.core.memory import extract_and_store_long_term_memories

    user_id = _workspace_user_id()
    history = [
        {"role": row["role"], "content": row["content"]}
        for row in await _workspace_history_rows(project_id=project_id, limit=20)
    ]

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO messages (chat_id, role, content, source, project_id)
            VALUES (?, ?, ?, 'web', ?)
            """,
            (user_id, "user", message, project_id),
        )
        await db.commit()

    system_prompt = await _build_system_prompt()
    full_reply: list[str] = []

    async def generate():
        try:
            yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"
            async for token in stream_chat_response(
                message=message,
                history=history,
                system_prompt=system_prompt,
                model=model,
                agent="MainChatAgent",
            ):
                full_reply.append(token)
                yield f"data: {json.dumps({'type': 'token', 'text': token}, ensure_ascii=False)}\n\n"

            reply_text = "".join(full_reply).strip() or "ยังไม่มีคำตอบตอนนี้"
            async with get_db() as db:
                await db.execute(
                    """
                    INSERT INTO messages (chat_id, role, content, source, project_id)
                    VALUES (?, ?, ?, 'web', ?)
                    """,
                    (user_id, "assistant", reply_text, project_id),
                )
                await db.execute(
                    "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                    ("workspace_chat_stream_saved", f"project_id={project_id or 'all'}"),
                )
                await db.commit()

            try:
                async with get_db() as db:
                    await db.execute("DELETE FROM memories WHERE key = 'model_handoff_context'")
                    await db.commit()
            except Exception:
                pass

            try:
                await extract_and_store_long_term_memories(message, reply_text)
            except Exception:
                pass

            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/workspace/chat/history")
async def workspace_chat_history(request: Request):
    await _require_admin(request)
    project_id = _normalize_project_id(request.query_params.get("project_id"))
    limit = int(request.query_params.get("limit", "200") or 200)
    return JSONResponse({"messages": await _workspace_history_rows(project_id=project_id, limit=limit)})


@app.post("/workspace/notes/save")
async def workspace_notes_save(request: Request):
    await _require_admin(request)
    payload = await request.json()
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(status_code=400, detail="กรุณาระบุข้อความ")
    from app.agents.brain import process_note

    result = await process_note(text, _workspace_user_id())
    return JSONResponse({"ok": True, "message": result})


@app.get("/workspace/notes")
async def workspace_notes(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, content, category, ai_summary, datetime(created_at, '+7 hours') AS local_created_at
            FROM notes
            ORDER BY id DESC
            """
        )
        rows = await cursor.fetchall()
    return JSONResponse(
        {
            "notes": [
                {
                    "id": int(row["id"]),
                    "content": str(row["content"] or ""),
                    "category": str(row["category"] or "note"),
                    "ai_summary": str(row["ai_summary"] or ""),
                    "created_at": str(row["local_created_at"] or ""),
                }
                for row in rows
            ]
        }
    )


@app.post("/workspace/tasks/create")
async def workspace_tasks_create(request: Request):
    await _require_admin(request)
    payload = await request.json()
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="กรุณาระบุชื่อ task")
    priority = str(payload.get("priority", "medium")).strip().lower() or "medium"
    deadline_hint = str(payload.get("deadline_hint", "")).strip()
    target_status = str(payload.get("status", "open")).strip().lower() or "open"
    from app.agents.task import create_task

    result = await create_task(title, priority=priority, deadline_hint=deadline_hint)
    if target_status != "open":
        async with get_db() as db:
            cursor = await db.execute("SELECT MAX(id) AS latest_id FROM tasks")
            row = await cursor.fetchone()
            latest_id = int(row["latest_id"]) if row and row["latest_id"] else 0
            if latest_id:
                await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (target_status, latest_id))
                await db.commit()
    return JSONResponse({"ok": True, "message": result})


@app.post("/workspace/tasks/{task_id}/done")
async def workspace_tasks_done(task_id: int, request: Request):
    await _require_admin(request)
    from app.agents.task import complete_task

    result = await complete_task(task_id)
    return JSONResponse({"ok": True, "message": result})


@app.post("/workspace/tasks/{task_id}/status")
async def workspace_tasks_status(task_id: int, request: Request):
    await _require_admin(request)
    payload = await request.json()
    status = str(payload.get("status", "open")).strip().lower()
    if status not in {"open", "in_progress", "done"}:
        raise HTTPException(status_code=400, detail="สถานะไม่ถูกต้อง")
    async with get_db() as db:
        await db.execute(
            """
            UPDATE tasks
            SET status = ?, done_at = CASE WHEN ? = 'done' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id = ?
            """,
            (status, status, task_id),
        )
        await db.commit()
    return JSONResponse({"ok": True, "status": status})


@app.get("/workspace/tasks")
async def workspace_tasks(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, title, priority, deadline_hint, status, datetime(created_at, '+7 hours') AS local_created_at
            FROM tasks
            ORDER BY
              CASE status
                WHEN 'open' THEN 0
                WHEN 'in_progress' THEN 1
                ELSE 2
              END,
              id DESC
            """
        )
        rows = await cursor.fetchall()
    priority_badges = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    return JSONResponse(
        {
            "tasks": [
                {
                    "id": int(row["id"]),
                    "title": str(row["title"] or ""),
                    "priority": str(row["priority"] or "medium"),
                    "priority_badge": priority_badges.get(str(row["priority"] or "medium"), "🟡"),
                    "deadline_hint": str(row["deadline_hint"] or ""),
                    "status": str(row["status"] or "open"),
                    "created_at": str(row["local_created_at"] or ""),
                }
                for row in rows
            ]
        }
    )


@app.get("/workspace/standup/preview")
async def workspace_standup_preview(request: Request):
    await _require_admin(request)
    from app.agents.standup_agent import generate_standup

    report = await generate_standup()
    return JSONResponse({"report": report})


@app.get("/workspace/standup/projects")
async def workspace_standup_projects(request: Request):
    await _require_admin(request)
    from app.agents.standup_agent import list_projects

    return JSONResponse({"projects": await list_projects()})


@app.post("/workspace/standup/projects/{project_id}/update")
async def workspace_standup_projects_update(project_id: int, request: Request):
    await _require_admin(request)
    from app.agents.standup_agent import update_project_field

    payload = await request.json()
    field = str(payload.get("field", "")).strip()
    value = payload.get("value", "")
    ok = await update_project_field(project_id, field, value)
    if not ok:
        raise HTTPException(status_code=400, detail="อัปเดตโปรเจ็กต์ไม่สำเร็จ")
    return JSONResponse({"ok": True})


@app.post("/workspace/brainstorm")
async def workspace_brainstorm(request: Request):
    await _require_admin(request)
    payload = await request.json()
    topic = str(payload.get("topic", "")).strip()
    if not topic:
        raise HTTPException(status_code=400, detail="กรุณาระบุหัวข้อ")
    from app.agents.brainstorm import run_brainstorm

    result = await run_brainstorm(topic)
    return JSONResponse(_parse_brainstorm_blocks(result))


@app.get("/workspace/news")
async def workspace_news(request: Request):
    await _require_admin(request)
    from app.agents.news import _detect_category

    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, title, url, source, summary, relevance, datetime(fetched_at, '+7 hours') AS local_fetched_at
            FROM news_items
            ORDER BY id DESC
            LIMIT 60
            """
        )
        rows = await cursor.fetchall()
    items = []
    for row in rows:
        topic_text = f"{row['title']} {row['summary']}".lower()
        items.append(
            {
                "id": int(row["id"]),
                "title": str(row["title"] or ""),
                "url": str(row["url"] or ""),
                "source": str(row["source"] or ""),
                "summary": str(row["summary"] or ""),
                "relevance": str(row["relevance"] or ""),
                "fetched_at": str(row["local_fetched_at"] or ""),
                "category": _detect_category(topic_text),
            }
        )
    return JSONResponse({"news": items})


@app.post("/workspace/news/fetch")
async def workspace_news_fetch(request: Request):
    await _require_admin(request)
    from app.agents.news import fetch_and_summarize

    result = await fetch_and_summarize()
    return JSONResponse({"ok": True, "message": result})


@app.get("/workspace/memory")
async def workspace_memory(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, content, memory_type, datetime(created_at, '+7 hours') AS local_created_at
            FROM long_term_memories
            ORDER BY id DESC
            LIMIT 100
            """
        )
        rows = await cursor.fetchall()
    return JSONResponse(
        {
            "memories": [
                {
                    "id": int(row["id"]),
                    "content": str(row["content"] or ""),
                    "memory_type": str(row["memory_type"] or "general"),
                    "created_at": str(row["local_created_at"] or ""),
                }
                for row in rows
            ]
        }
    )


@app.get("/workspace/files")
async def workspace_files(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, filename, filepath, size_bytes, summary, datetime(created_at, '+7 hours') AS local_created_at
            FROM uploads
            ORDER BY id DESC
            """
        )
        rows = await cursor.fetchall()
    return JSONResponse(
        {
            "files": [
                {
                    "id": int(row["id"]),
                    "filename": str(row["filename"] or ""),
                    "filepath": str(row["filepath"] or ""),
                    "size_bytes": int(row["size_bytes"] or 0),
                    "summary": str(row["summary"] or ""),
                    "created_at": str(row["local_created_at"] or ""),
                }
                for row in rows
            ]
        }
    )


@app.get("/workspace/system/info")
async def workspace_system_info(request: Request):
    await _require_admin(request)
    from app.core.database import get_system_stats

    stats = await get_system_stats()
    active_model = await get_active_model()
    agents_dir = Path(__file__).resolve().parent / "agents"
    try:
        agent_files = sorted(
            file_path.stem
            for file_path in agents_dir.glob("*.py")
            if file_path.name != "__init__.py"
        )
    except Exception:
        agent_files = []
    return JSONResponse(
        {
            "model": get_model_label(active_model or ""),
            "stats": stats,
            "agents": agent_files,
            "agent_count": len(agent_files),
            "scheduler": [
                {"time": "07:30 จ-ศ", "job": "Daily Standup -> Telegram"},
                {"time": "08:00 ทุกวัน", "job": "News + Morning Briefing"},
                {"time": "21:00 ทุกวัน", "job": "Daily Digest + Session Log"},
                {"time": "จันทร์ 09:00", "job": "Weekly Review"},
            ],
        }
    )


@app.post("/workspace/benchmark/run")
async def workspace_benchmark_run(request: Request):
    await _require_admin(request)
    body = await request.json()
    ids = body.get("question_ids") or None
    from app.agents.benchmark_agent import run_benchmark
    import asyncio

    try:
        results = await asyncio.wait_for(run_benchmark(ids), timeout=180.0)
        return JSONResponse({"results": results})
    except asyncio.TimeoutError:
        return JSONResponse({"error": "timeout", "results": []}, status_code=408)


@app.get("/workspace/benchmark/summary")
async def workspace_benchmark_summary(request: Request):
    await _require_admin(request)
    from app.agents.benchmark_agent import get_benchmark_summary

    data = await get_benchmark_summary()
    return JSONResponse(data)


@app.post("/workspace/benchmark/rate")
async def workspace_benchmark_rate(request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.agents.benchmark_agent import save_rating

    await save_rating(int(body["id"]), int(body["rating"]))
    return JSONResponse({"ok": True})


# ── Code Assistant routes ─────────────────────────────────────────────────────

@app.get("/workspace/code/files")
async def workspace_code_files(request: Request):
    await _require_admin(request)
    import os
    base = "/app"
    file_list = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "__pycache__", ".venv", "node_modules", "backups", "data"}]
        for f in files:
            if f.endswith((".py", ".yml", ".yaml", ".txt", ".md", ".env.example", ".json")):
                rel = os.path.relpath(os.path.join(root, f), base)
                file_list.append(rel.replace("\\", "/"))
    return JSONResponse({"files": sorted(file_list)})


@app.get("/workspace/code/file")
async def workspace_code_file(request: Request, path: str = ""):
    await _require_admin(request)
    import os
    base = "/app"
    full = os.path.normpath(os.path.join(base, path))
    if not full.startswith(base):
        raise HTTPException(400, "invalid path")
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
        lines = content.splitlines()
        return JSONResponse({"path": path, "content": content,
                             "lines": len(lines), "size": len(content)})
    except FileNotFoundError:
        raise HTTPException(404, "file not found")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/workspace/code/chat")
async def workspace_code_chat(request: Request):
    await _require_admin(request)
    body = await request.json()
    question = body.get("question", "").strip()
    file_path = body.get("file_path", "")
    file_content = body.get("file_content", "")
    folder_data = body.get("folder_data", None)
    model = body.get("model", "haiku")
    if not question:
        raise HTTPException(400, "question required")
    from app.core.ai import chat as ai_chat, _VALID_MODELS
    if model not in _VALID_MODELS:
        model = "haiku"
    file_context = ""
    if folder_data:
        file_list = "\n".join([
            f"- {f['path']} ({f['lines']} lines)"
            for f in folder_data.get("files", [])
        ])
        previews = "\n\n".join([
            f"### {f['path']}\n```python\n{f['preview']}\n```"
            for f in folder_data.get("files", [])[:10]
        ])
        file_context = (
            f"\n\n=== Folder: {folder_data.get('folder')} ===\n"
            f"{folder_data.get('file_count')} files, {folder_data.get('total_lines')} total lines\n\n"
            f"Files:\n{file_list}\n\n"
            f"Code Previews (first 50 lines each):\n{previews}\n"
        )
    elif file_path and file_content:
        lines = file_content.splitlines()
        preview = "\n".join(lines[:200])
        file_context = (
            f"\n\n=== Current File: {file_path} ({len(lines)} lines) ===\n"
            f"```python\n{preview}\n```\n"
        )
    system = (
        f"คุณเป็น Ener-AI Code Assistant ผู้เชี่ยวชาญ Python/FastAPI\n"
        f"ระบบ Ener-AI: FastAPI + SQLite + Telegram + agents + Web Workspace\n"
        f"Stack: Python 3.11, FastAPI, aiosqlite, python-telegram-bot, Anthropic/Groq/Gemini APIs\n"
        f"Server: Hetzner CPX22, Docker, domain my-ener.uk{file_context}\n\n"
        f"เมื่อตอบ:\n"
        f"- ถ้าต้องแก้ code → สร้าง Cursor prompt ให้พร้อมวาง\n"
        f"- ถ้าอธิบาย → กระชับ ตรงประเด็น\n"
        f"- ใช้ภาษาไทยผสม technical terms"
    )
    answer = await ai_chat(
        question, system=system, agent="CodeAssistant",
        messages=[], preferred_model=model, strict_model=False,
    )
    return JSONResponse({"answer": str(answer)})


@app.post("/workspace/code/remember")
async def workspace_code_remember(request: Request):
    await _require_admin(request)
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        raise HTTPException(400, "content required")
    from app.core.config import settings as _s
    async with get_db() as db:
        await db.execute(
            "INSERT INTO long_term_memories (content, memory_type, chat_id) VALUES (?,?,?)",
            (f"[Code] {content}", "code_decision", str(_s.telegram_chat_id)),
        )
        await db.commit()
    return JSONResponse({"ok": True})


@app.get("/workspace/code/git-log")
async def workspace_code_git_log(request: Request):
    await _require_admin(request)
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--format=%h|%s|%ar|%an"],
            capture_output=True, text=True, cwd="/app",
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "message": parts[1],
                                "time": parts[2], "author": parts[3]})
        return JSONResponse({"commits": commits})
    except Exception as exc:
        return JSONResponse({"commits": [], "error": str(exc)})


@app.get("/workspace/code/folder")
async def workspace_code_folder(request: Request, path: str = "app"):
    await _require_admin(request)
    import os
    base = "/app"
    folder = os.path.normpath(os.path.join(base, path))
    if not folder.startswith(base):
        raise HTTPException(400, "invalid path")
    files_content = []
    total_lines = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".git"}]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            full_path = os.path.join(root, f)
            rel = os.path.relpath(full_path, base)
            try:
                with open(full_path, "r", encoding="utf-8") as fh:
                    content = fh.read()
                lines = content.splitlines()
                total_lines += len(lines)
                files_content.append({
                    "path": rel.replace("\\", "/"),
                    "lines": len(lines),
                    "preview": "\n".join(lines[:50]),
                    "content": content[:3000],
                })
            except Exception:
                pass
    return JSONResponse({
        "folder": path,
        "file_count": len(files_content),
        "total_lines": total_lines,
        "files": files_content,
    })


@app.post("/workspace/files/upload")
async def workspace_files_upload(request: Request, file: UploadFile = File(...)):
    await _require_admin(request)
    safe_name = Path(file.filename or "upload.bin").name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in {".pdf", ".docx", ".txt", ".md"}:
        raise HTTPException(status_code=400, detail="รองรับเฉพาะ PDF, DOCX, TXT, MD")
    upload_dir = _workspace_upload_dir()
    filename = f"{int(time.time())}_{safe_name}"
    destination = upload_dir / filename
    try:
        with destination.open("wb") as output:
            shutil.copyfileobj(file.file, output)
    finally:
        await file.close()
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO uploads (filename, filepath, size_bytes)
            VALUES (?, ?, ?)
            """,
            (safe_name, str(destination), destination.stat().st_size),
        )
        upload_id = cursor.lastrowid
        await db.commit()
    return JSONResponse({"ok": True, "id": upload_id, "filename": safe_name})


@app.post("/workspace/files/{file_id}/summarize")
async def workspace_files_summarize(file_id: int, request: Request):
    await _require_admin(request)
    from app.core.ai import chat

    async with get_db() as db:
        cursor = await db.execute("SELECT filename, filepath FROM uploads WHERE id = ?", (file_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    file_path = Path(str(row["filepath"]))
    content = _read_uploaded_file_text(file_path)[:12000]
    summary = await chat(
        f"สรุปไฟล์นี้แบบอ่านเร็วให้กบ:\n\nชื่อไฟล์: {row['filename']}\n\nเนื้อหา:\n{content}",
        system="ตอบเป็นภาษาไทยแบบกระชับ แบ่งเป็น bullet ที่อ่านง่าย",
        agent="workspace_file",
        preferred_model="gemini",
    )
    async with get_db() as db:
        await db.execute("UPDATE uploads SET summary = ? WHERE id = ?", (summary, file_id))
        await db.commit()
    return JSONResponse({"ok": True, "summary": summary})


@app.post("/workspace/files/{file_id}/ask")
async def workspace_files_ask(file_id: int, request: Request):
    await _require_admin(request)
    from app.core.ai import chat

    payload = await request.json()
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(status_code=400, detail="กรุณาระบุคำถาม")
    async with get_db() as db:
        cursor = await db.execute("SELECT filename, filepath FROM uploads WHERE id = ?", (file_id,))
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์")
    file_path = Path(str(row["filepath"]))
    content = _read_uploaded_file_text(file_path)[:12000]
    answer = await chat(
        f"ไฟล์: {row['filename']}\n\nคำถาม: {question}\n\nเนื้อหาไฟล์:\n{content}",
        system="ตอบคำถามจากเอกสารให้กบเป็นภาษาไทย กระชับ และอ้างอิงเฉพาะสิ่งที่อยู่ในไฟล์",
        agent="workspace_file_qa",
        preferred_model="gemini",
    )
    return JSONResponse({"ok": True, "answer": answer})


@app.get("/workspace/projects")
async def workspace_projects(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        total_cursor = await db.execute(
            "SELECT COUNT(*) AS total FROM messages WHERE chat_id = ?",
            (_workspace_user_id(),),
        )
        total_row = await total_cursor.fetchone()
        cursor = await db.execute(
            """
            SELECT
                p.id,
                p.name,
                datetime(p.created_at, '+7 hours') AS local_created_at,
                COUNT(m.id) AS message_count,
                MAX(datetime(m.created_at, '+7 hours')) AS last_active
            FROM projects p
            LEFT JOIN messages m ON m.project_id = p.id AND m.chat_id = ?
            WHERE p.deleted_at IS NULL
            GROUP BY p.id, p.name, p.created_at
            ORDER BY COALESCE(MAX(m.created_at), p.created_at) DESC, p.id DESC
            """
            ,
            (_workspace_user_id(),),
        )
        rows = await cursor.fetchall()
    return JSONResponse(
        {
            "total_messages": int(total_row["total"] or 0) if total_row else 0,
            "projects": [
                {
                    "id": int(row["id"]),
                    "name": str(row["name"] or ""),
                    "created_at": str(row["local_created_at"] or ""),
                    "message_count": int(row["message_count"] or 0),
                    "last_active": str(row["last_active"] or ""),
                }
                for row in rows
            ],
        }
    )


@app.post("/workspace/projects/create")
async def workspace_projects_create(request: Request):
    await _require_admin(request)
    payload = await request.json()
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="กรุณาระบุชื่อโปรเจ็กต์")
    async with get_db() as db:
        cursor = await db.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        await db.commit()
    return JSONResponse({"ok": True, "id": cursor.lastrowid, "name": name})


@app.delete("/workspace/projects/{project_id}")
async def workspace_projects_delete(project_id: int, request: Request):
    await _require_admin(request)
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_id,),
        )
        await db.commit()
    return JSONResponse({"ok": True})


@app.get("/admin")
async def admin_dashboard(request: Request):
    await _require_admin(request)
    return build_admin_html(await _load_admin_overview())


@app.get("/admin/config")
async def admin_config_page(request: Request):
    await _verify_admin_session(request)
    configs = await get_all_config()
    return build_admin_config_html(configs)


@app.get("/admin/pipeline-metrics")
async def admin_pipeline_metrics(request: Request):
    await _verify_admin_session(request)
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT complexity, domain, model_used,
                   router_ms, reasoner_ms, checker_ms, total_ms,
                   was_fixed, question_preview,
                   datetime(created_at, '+7 hours') AS created_at
            FROM pipeline_metrics
            ORDER BY id DESC
            LIMIT 100
            """
        )
        rows = await cur.fetchall()
        cur2 = await db.execute(
            """
            SELECT model_used,
                   COUNT(*) AS count,
                   AVG(total_ms) AS avg_total,
                   AVG(router_ms) AS avg_router,
                   AVG(reasoner_ms) AS avg_reasoner,
                   AVG(checker_ms) AS avg_checker
            FROM pipeline_metrics
            WHERE created_at > datetime('now', '-24 hours')
            GROUP BY model_used
            """
        )
        avgs = await cur2.fetchall()
        cur3 = await db.execute(
            """
            SELECT complexity, COUNT(*) AS count
            FROM pipeline_metrics
            WHERE created_at > datetime('now', '-24 hours')
            GROUP BY complexity
            """
        )
        dist = await cur3.fetchall()

    return JSONResponse(
        {
            "recent": [dict(row) for row in rows],
            "averages": [dict(row) for row in avgs],
            "distribution": [dict(row) for row in dist],
        }
    )


@app.get("/admin/pipeline")
async def admin_pipeline_page(request: Request):
    await _verify_admin_session(request)
    return build_pipeline_html()


# ── Routing Editor ────────────────────────────────────────────────────────────

def build_routing_html() -> HTMLResponse:
    _MODEL_INFO = {
        "groq":            {"badge": "🟢", "label": "Groq",          "note": "ฟรี ~450ms",    "cost": "Free"},
        "gemini":          {"badge": "🟢", "label": "Gemini",        "note": "ฟรี ~4000ms",   "cost": "Free"},
        "deepseek-direct": {"badge": "💛", "label": "DeepSeek",      "note": "$0.14 ~2000ms", "cost": "$0.14/1M"},
        "gpt-4o-mini":     {"badge": "💛", "label": "GPT-4o Mini",   "note": "$0.15 ~2700ms", "cost": "$0.15/1M"},
        "haiku":           {"badge": "🟠", "label": "Haiku",         "note": "$0.80 ~4700ms", "cost": "$0.80/1M"},
        "sonnet":          {"badge": "🔴", "label": "Sonnet",        "note": "$3.00 ~7000ms", "cost": "$3.00/1M"},
        "llama4":          {"badge": "🟢", "label": "Llama 4 Scout", "note": "ฟรี ~800ms",    "cost": "Free"},
        "kimi":            {"badge": "💛", "label": "Kimi K2",       "note": "$0.14 ~3000ms", "cost": "$0.14/1M"},
    }
    options_html = "\n".join(
        f'<option value="{k}">{v["badge"]} {v["label"]} ({v["note"]})</option>'
        for k, v in _MODEL_INFO.items()
    )
    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>Routing Editor — Ener-AI Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0a0a;color:#e5e7eb;font-family:system-ui,sans-serif;min-height:100vh}}
  .header{{background:#111;border-bottom:1px solid #222;padding:16px 24px;display:flex;align-items:center;gap:16px}}
  .header h1{{font-size:1.2rem;font-weight:700;color:#f9fafb}}
  .back-btn{{background:#1e293b;color:#94a3b8;border:1px solid #334;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:0.85rem}}
  .back-btn:hover{{background:#273449;color:#e2e8f0}}
  .container{{max-width:1100px;margin:32px auto;padding:0 24px}}
  .card{{background:#111;border:1px solid #1f2937;border-radius:12px;padding:24px;margin-bottom:24px}}
  .card h2{{font-size:1rem;font-weight:600;color:#f9fafb;margin-bottom:4px}}
  .card p{{font-size:0.8rem;color:#6b7280;margin-bottom:20px}}
  table{{width:100%;border-collapse:collapse}}
  th{{text-align:left;padding:10px 12px;font-size:0.75rem;color:#6b7280;text-transform:uppercase;border-bottom:1px solid #1f2937}}
  td{{padding:12px;border-bottom:1px solid #111827;vertical-align:middle}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#0d1117}}
  .intent-tag{{font-family:monospace;font-size:0.8rem;background:#1f2937;color:#60a5fa;padding:3px 8px;border-radius:4px}}
  .label-text{{color:#d1d5db;font-size:0.9rem}}
  select{{background:#1f2937;color:#e5e7eb;border:1px solid #374151;padding:6px 10px;border-radius:6px;font-size:0.85rem;cursor:pointer;width:100%}}
  select:focus{{outline:none;border-color:#6366f1}}
  .saved-flash{{color:#22c55e;font-size:0.8rem;margin-left:8px;opacity:0;transition:opacity 0.3s}}
  .toast{{position:fixed;bottom:24px;right:24px;background:#1e293b;border:1px solid #334;color:#e2e8f0;padding:12px 20px;border-radius:8px;font-size:0.9rem;display:none;z-index:9999}}
</style>
</head>
<body>
<div class="header">
  <a class="back-btn" href="/admin">← Admin</a>
  <h1>🔀 Routing Editor</h1>
</div>
<div class="container">
  <div class="card">
    <h2>Intent → Model Mapping</h2>
    <p>เปลี่ยน model ต่อ intent ได้เลย — มีผลทันทีโดยไม่ต้อง restart</p>
    <table>
      <thead>
        <tr>
          <th>Intent</th>
          <th>Label</th>
          <th>Model</th>
          <th>Cost</th>
        </tr>
      </thead>
      <tbody id="routing-table">
        <tr><td colspan="4" style="color:#6b7280;text-align:center;padding:32px">กำลังโหลด...</td></tr>
      </tbody>
    </table>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const MODEL_INFO = {json.dumps(_MODEL_INFO)};
const MODEL_OPTIONS = `{options_html}`;

function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  setTimeout(() => {{ t.style.display = 'none'; }}, 2500);
}}

async function updateRouting(intent, model) {{
  try {{
    const res = await fetch('/admin/api/routing/' + intent, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{model}})
    }});
    if (res.ok) showToast('✅ Saved: ' + intent + ' → ' + model);
    else showToast('❌ Save failed');
  }} catch(e) {{ showToast('❌ ' + e.message); }}
}}

async function loadRouting() {{
  try {{
    const res = await fetch('/admin/api/routing');
    const rows = await res.json();
    const tbody = document.getElementById('routing-table');
    tbody.innerHTML = rows.map(row => {{
      const info = MODEL_INFO[row.model] || {{}};
      const opts = Object.entries(MODEL_INFO).map(([k, v]) =>
        `<option value="${{k}}" ${{k === row.model ? 'selected' : ''}}>${{v.badge}} ${{v.label}} (${{v.note}})</option>`
      ).join('');
      return `<tr>
        <td><span class="intent-tag">${{row.intent}}</span></td>
        <td><span class="label-text">${{row.label || ''}}</span></td>
        <td>
          <select onchange="updateRouting('${{row.intent}}', this.value)">${{opts}}</select>
        </td>
        <td style="color:#9ca3af;font-size:0.8rem">${{info.cost || '-'}}</td>
      </tr>`;
    }}).join('');
  }} catch(e) {{
    document.getElementById('routing-table').innerHTML =
      '<tr><td colspan="4" style="color:#ef4444;text-align:center">โหลดไม่สำเร็จ: ' + e.message + '</td></tr>';
  }}
}}

loadRouting();
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/admin/routing")
async def admin_routing_page(request: Request):
    await _verify_admin_session(request)
    return build_routing_html()


@app.get("/admin/api/routing")
async def admin_routing_get(request: Request):
    await _verify_admin_session(request)
    from app.core.database import get_db
    async with get_db() as db:
        cur = await db.execute(
            "SELECT intent, model, label FROM routing_config ORDER BY intent"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return JSONResponse(rows)


@app.post("/admin/api/routing/{intent}")
async def admin_routing_update(intent: str, request: Request):
    await _verify_admin_session(request)
    body = await request.json()
    model = body.get("model", "").strip()
    if not model:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)
    from app.core.database import get_db
    async with get_db() as db:
        await db.execute(
            "UPDATE routing_config SET model=?, updated_at=datetime('now') WHERE intent=?",
            (model, intent),
        )
        await db.commit()
    return JSONResponse({"ok": True})


# ── API Status Monitor ────────────────────────────────────────────────────────

def build_api_status_html() -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>API Status — Ener-AI Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a0a;color:#e5e7eb;font-family:system-ui,sans-serif;min-height:100vh}
  .header{background:#111;border-bottom:1px solid #222;padding:16px 24px;display:flex;align-items:center;gap:16px}
  .header h1{font-size:1.2rem;font-weight:700;color:#f9fafb}
  .back-btn{background:#1e293b;color:#94a3b8;border:1px solid #334;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:0.85rem}
  .back-btn:hover{background:#273449;color:#e2e8f0}
  .container{max-width:1100px;margin:32px auto;padding:0 24px}
  .toolbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
  .toolbar span{color:#6b7280;font-size:0.85rem}
  .refresh-btn{background:#1e293b;color:#94a3b8;border:1px solid #334;padding:7px 16px;border-radius:6px;cursor:pointer;font-size:0.85rem}
  .refresh-btn:hover{background:#273449;color:#e2e8f0}
  .status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}
  .api-card{background:#111;border:1px solid #1f2937;border-radius:12px;padding:20px}
  .api-card.ok{border-left:3px solid #22c55e}
  .api-card.error{border-left:3px solid #ef4444}
  .api-card.no_key{border-left:3px solid #4b5563}
  .api-name{font-size:1rem;font-weight:600;color:#f9fafb;margin-bottom:10px}
  .api-status{font-size:0.9rem;font-weight:600;margin-bottom:6px}
  .api-latency{font-size:0.8rem;color:#9ca3af;margin-bottom:4px}
  .api-error{font-size:0.75rem;color:#f87171;margin-top:6px;word-break:break-word;background:#1a0a0a;padding:6px 8px;border-radius:4px}
  .loading{color:#6b7280;text-align:center;padding:48px;grid-column:1/-1}
</style>
</head>
<body>
<div class="header">
  <a class="back-btn" href="/admin">← Admin</a>
  <h1>📡 API Status Monitor</h1>
</div>
<div class="container">
  <div class="toolbar">
    <span id="checked-at">กำลังตรวจสอบ...</span>
    <button class="refresh-btn" onclick="loadStatus()">🔄 Refresh</button>
  </div>
  <div class="status-grid" id="status-grid">
    <div class="loading">กำลังโหลด...</div>
  </div>
</div>
<script>
const STATUS_COLOR = {ok:'#22c55e', error:'#ef4444', no_key:'#9ca3af'};
const STATUS_ICON  = {ok:'✅ Online', error:'❌ Error', no_key:'⚪ No Key'};

async function loadStatus() {
  document.getElementById('checked-at').textContent = 'กำลังตรวจสอบ...';
  document.getElementById('status-grid').innerHTML = '<div class="loading">กำลังตรวจสอบ — อาจใช้เวลา 10-15 วินาที...</div>';
  try {
    const res = await fetch('/admin/api/provider-status');
    const d = await res.json();
    const grid = document.getElementById('status-grid');
    grid.innerHTML = d.providers.map(p => `
      <div class="api-card ${p.status}">
        <div class="api-name">${p.name}</div>
        <div class="api-status" style="color:${STATUS_COLOR[p.status]}">${STATUS_ICON[p.status]}</div>
        <div class="api-latency">${p.latency_ms > 0 ? p.latency_ms + 'ms' : '-'}</div>
        ${p.error ? '<div class="api-error">' + p.error + '</div>' : ''}
      </div>
    `).join('');
    document.getElementById('checked-at').textContent = 'Updated: ' + d.checked_at;
  } catch(e) {
    document.getElementById('status-grid').innerHTML =
      '<div class="loading" style="color:#ef4444">โหลดไม่สำเร็จ: ' + e.message + '</div>';
  }
}

setInterval(loadStatus, 60000);
loadStatus();
</script>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/admin/api-status")
async def admin_api_status_page(request: Request):
    await _verify_admin_session(request)
    return build_api_status_html()


@app.get("/admin/api/provider-status")
async def admin_provider_status(request: Request):
    import asyncio as _asyncio
    import time as _time
    await _verify_admin_session(request)
    from app.core.config import settings as _s
    from app.core.database import get_config

    deepseek_key = _s.deepseek_api_key or await get_config("deepseek_api_key", "")
    openai_key   = _s.openai_api_key   or await get_config("openai_api_key",   "")
    xai_key      = _s.xai_api_key      or await get_config("xai_api_key",      "")
    moonshot_key = _s.moonshot_api_key or await get_config("moonshot_api_key", "")

    async def ping(name: str, url: str, headers_fn, payload: dict, key: str):
        if not key:
            return {"name": name, "status": "no_key", "latency_ms": -1, "error": "API key not configured"}
        start = _time.time()
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient() as client:
                resp = await client.post(url, headers=headers_fn(key), json=payload, timeout=10.0)
            latency = int((_time.time() - start) * 1000)
            if resp.status_code in (200, 201):
                return {"name": name, "status": "ok", "latency_ms": latency, "error": None}
            return {"name": name, "status": "error", "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"name": name, "status": "error", "latency_ms": int((_time.time() - start) * 1000), "error": str(exc)[:120]}

    base = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    tasks = [
        ping("Groq", "https://api.groq.com/openai/v1/chat/completions",
             lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
             {**base, "model": "llama-3.1-8b-instant"}, _s.groq_api_key),
        ping("Anthropic (Haiku)", "https://api.anthropic.com/v1/messages",
             lambda k: {"x-api-key": k, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
             {"model": "claude-haiku-4-5-20251001", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
             _s.anthropic_api_key),
        ping("DeepSeek", "https://api.deepseek.com/chat/completions",
             lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
             {**base, "model": "deepseek-chat"}, deepseek_key),
        ping("OpenAI", "https://api.openai.com/v1/chat/completions",
             lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
             {**base, "model": "gpt-4o-mini"}, openai_key),
        ping("xAI Grok", "https://api.x.ai/v1/chat/completions",
             lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
             {**base, "model": "grok-3"}, xai_key),
        ping("Moonshot Kimi", "https://api.moonshot.cn/v1/chat/completions",
             lambda k: {"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
             {**base, "model": "kimi-k2-5"}, moonshot_key),
    ]
    import asyncio as _asyncio2
    results = await _asyncio2.gather(*tasks)
    return JSONResponse({"providers": list(results), "checked_at": _time.strftime("%H:%M:%S")})


@app.get("/admin/otp")
async def otp_page(request: Request):
    if await _is_valid_session(request):
        return RedirectResponse("/admin", status_code=303)

    await _validate_admin_basic_auth(request)
    now = time.time()
    async with _admin_otp_lock:
        otp_state = await _get_admin_otp_state()
        otp_code = otp_state.get(_ADMIN_OTP_CODE_KEY, "")
        try:
            otp_expires_at = float(otp_state.get(_ADMIN_OTP_EXPIRE_KEY, "0") or 0)
        except Exception:
            otp_expires_at = 0.0
        try:
            last_sent = float(otp_state.get(_ADMIN_OTP_LAST_SENT_KEY, "0") or 0)
        except Exception:
            last_sent = 0.0

        has_valid_otp = bool(otp_code) and otp_expires_at > now
        recently_sent = now - last_sent < _OTP_SEND_COOLDOWN

        if not has_valid_otp and not recently_sent:
            otp = _generate_otp()
            otp_expires_at = now + OTP_EXPIRE
            await _store_admin_otp(otp, otp_expires_at, now)
            await _send_otp_telegram(otp)
            just_sent = True
        else:
            just_sent = False

    status_copy = "📱 ส่ง OTP ไป Telegram แล้ว" if just_sent else "📱 OTP ยังไม่หมดอายุ กรอกได้เลย"
    initial_seconds = max(1, min(OTP_EXPIRE, int(otp_expires_at - now)))
    otp_html = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Admin OTP - Ener-AI</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      background:#000; color:#fff;
      display:flex; align-items:center; justify-content:center;
      height:100vh; font-family:monospace;
    }
    .otp-box {
      background:#111; border:1px solid #222;
      border-radius:12px; padding:40px;
      width:340px; text-align:center;
    }
    .otp-box h2 { color:#00ff88; margin-bottom:8px; font-size:20px; }
    .otp-box p { color:#888; font-size:13px; margin-bottom:24px; }
    .otp-input {
      width:100%; padding:14px;
      background:#000; border:1px solid #333;
      color:#00ff88; border-radius:8px;
      font-size:28px; text-align:center;
      letter-spacing:8px; font-family:monospace;
      margin-bottom:16px;
    }
    .otp-input:focus { outline:none; border-color:#00ff88; }
    .submit-btn {
      width:100%; padding:12px;
      background:#00ff88; color:#000;
      border:none; border-radius:8px;
      font-size:15px; font-weight:bold;
      cursor:pointer;
    }
    .submit-btn:hover { background:#00cc70; }
    .resend-btn {
      background:none; border:none;
      color:#555; cursor:pointer;
      font-size:12px; margin-top:12px;
      text-decoration:underline;
    }
    .resend-btn:hover { color:#888; }
    .error { color:#ff4444; font-size:13px; margin-top:8px; }
    .sent-msg { color:#00ff88; font-size:12px; margin-bottom:16px; }
    .timer { color:#ffaa00; font-size:12px; margin-top:8px; }
  </style>
</head>
<body>
  <div class="otp-box">
    <h2>🔐 Admin Access</h2>
    <p>OTP ส่งไป Telegram แล้วครับ</p>
    <div class="sent-msg">__STATUS_COPY__</div>

    <form method="POST" action="/admin/otp/verify">
      <input type="text" name="otp" class="otp-input"
             maxlength="6" placeholder="000000"
             autofocus autocomplete="off"
             oninput="this.value=this.value.replace(/[^0-9]/g,'')">
      <button type="submit" class="submit-btn">✅ เข้าใช้งาน</button>
    </form>

    <div class="timer" id="timer">หมดอายุใน 5:00</div>
    <button class="resend-btn" onclick="resend()">ส่ง OTP ใหม่</button>
    <div style="margin-top:16px;">
      <a href="/admin/reset" style="color:#888; font-size:12px; text-decoration:underline;">
        ลืมรหัสผ่าน? รีเซ็ตผ่าน Telegram OTP
      </a>
    </div>
    <div id="msg"></div>
  </div>

  <script>
    let seconds = __INITIAL_SECONDS__;
    const timer = setInterval(() => {
      seconds--;
      const m = Math.floor(seconds / 60);
      const s = seconds % 60;
      document.getElementById('timer').textContent =
        `หมดอายุใน ${m}:${s.toString().padStart(2, '0')}`;
      if (seconds <= 0) {
        clearInterval(timer);
        document.getElementById('timer').textContent = '⏰ OTP หมดอายุแล้ว';
        document.getElementById('timer').style.color = '#ff4444';
      }
    }, 1000);

    async function resend() {
      const res = await fetch('/admin/otp/resend', { method: 'POST' });
      if (!res.ok) {
        document.getElementById('msg').textContent = '❌ ส่ง OTP ไม่สำเร็จ';
        document.getElementById('msg').style.color = '#ff4444';
        return;
      }

      const data = await res.json();
      if (data.ok) {
        document.getElementById('msg').textContent = '✅ ส่ง OTP ใหม่แล้ว';
        document.getElementById('msg').style.color = '#00ff88';
        seconds = 300;
        document.getElementById('timer').style.color = '#ffaa00';
      } else if (typeof data.wait === 'number') {
        document.getElementById('msg').textContent = `กรุณารอ ${data.wait} วินาที`;
        document.getElementById('msg').style.color = '#ffaa00';
      }
    }
  </script>
</body>
</html>"""

    return HTMLResponse(
        otp_html
        .replace("__STATUS_COPY__", escape(status_copy))
        .replace("__INITIAL_SECONDS__", str(initial_seconds))
    )


@app.post("/admin/otp/verify")
async def verify_otp(request: Request):
    await _validate_admin_basic_auth(request)
    form = await request.form()
    otp = str(form.get("otp", "")).strip()
    now = time.time()
    otp_state = await _get_admin_otp_state()
    stored_otp = otp_state.get(_ADMIN_OTP_CODE_KEY, "")
    try:
        otp_expires_at = float(otp_state.get(_ADMIN_OTP_EXPIRE_KEY, "0") or 0)
    except Exception:
        otp_expires_at = 0.0

    if not stored_otp or otp != stored_otp or now > otp_expires_at:
        return HTMLResponse(
            """
        <script>
        alert('OTP ไม่ถูกต้องหรือหมดอายุแล้วครับ');
        history.back();
        </script>
        """
        )

    await _clear_admin_otp()
    token = _generate_session_token()
    await _store_admin_session(token, now + SESSION_EXPIRE)

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "admin_session",
        token,
        max_age=SESSION_EXPIRE,
        httponly=True,
        samesite="strict",
    )
    return response


@app.post("/admin/otp/resend")
async def resend_otp(request: Request):
    await _validate_admin_basic_auth(request)
    otp_state = await _get_admin_otp_state()
    now = time.time()
    try:
        last_sent = float(otp_state.get(_ADMIN_OTP_LAST_SENT_KEY, "0") or 0)
    except Exception:
        last_sent = 0.0
    if now - last_sent < 60:
        remaining = int(60 - (now - last_sent))
        return {"ok": False, "wait": remaining}

    otp = _generate_otp()
    await _store_admin_otp(otp, now + OTP_EXPIRE, now)
    await _send_otp_telegram(otp)
    return {"ok": True}


@app.get("/admin/reset")
async def admin_reset_page(request: Request):
    if await _is_valid_session(request):
        return RedirectResponse("/admin", status_code=303)

    now = time.time()
    otp_state = await _get_admin_reset_otp_state()
    try:
        otp_expires_at = float(otp_state.get(_ADMIN_RESET_OTP_EXPIRE_KEY, "0") or 0)
    except Exception:
        otp_expires_at = 0.0
    has_valid_otp = bool(otp_state.get(_ADMIN_RESET_OTP_CODE_KEY, "")) and otp_expires_at > now
    initial_seconds = max(0, min(OTP_EXPIRE, int(otp_expires_at - now))) if has_valid_otp else 0
    reset_html = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reset Admin Password - Ener-AI</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      background:#000; color:#fff;
      display:flex; align-items:center; justify-content:center;
      min-height:100vh; font-family:monospace; padding:24px;
    }
    .reset-box {
      background:#111; border:1px solid #222; border-radius:12px;
      padding:32px; width:100%; max-width:420px; text-align:center;
    }
    .reset-box h2 { color:#00ff88; margin-bottom:8px; font-size:20px; }
    .reset-box p { color:#888; font-size:13px; margin-bottom:18px; }
    .hint { color:#ffaa00; font-size:12px; margin-bottom:16px; line-height:1.6; }
    .field {
      width:100%; padding:12px 14px; background:#000; border:1px solid #333;
      color:#fff; border-radius:8px; margin-bottom:12px; font-size:15px;
    }
    .field.otp {
      color:#00ff88; font-size:24px; letter-spacing:8px; text-align:center;
    }
    .field:focus { outline:none; border-color:#00ff88; }
    .primary-btn, .secondary-btn {
      width:100%; padding:12px; border:none; border-radius:8px;
      font-size:14px; font-weight:bold; cursor:pointer;
    }
    .primary-btn { background:#00ff88; color:#000; margin-top:4px; }
    .primary-btn:hover { background:#00cc70; }
    .secondary-btn {
      background:#1d1d1d; color:#fff; border:1px solid #333; margin-bottom:16px;
    }
    .secondary-btn:hover { background:#252525; }
    .meta { color:#888; font-size:12px; margin-top:14px; }
    .timer { color:#ffaa00; font-size:12px; margin:10px 0 16px; }
    .msg { min-height:18px; font-size:12px; margin-bottom:12px; }
    a { color:#888; font-size:12px; text-decoration:underline; }
  </style>
</head>
<body>
  <div class="reset-box">
    <h2>🔐 Reset Admin Password</h2>
    <p>รีเซ็ตรหัสเข้า `/admin` และ `/workspace` ผ่าน Telegram OTP</p>
    <div class="hint">กดส่ง OTP แล้วเช็กรหัสใน Telegram จากนั้นตั้งรหัสใหม่ที่ต้องการ</div>
    <button class="secondary-btn" onclick="sendResetOtp()">ส่ง OTP ไป Telegram</button>
    <div class="timer" id="timer">__TIMER_COPY__</div>
    <div class="msg" id="msg"></div>
    <form method="POST" action="/admin/reset/confirm">
      <input type="text" name="otp" class="field otp" maxlength="6" placeholder="000000"
             autocomplete="one-time-code" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
      <input type="password" name="new_password" class="field" placeholder="รหัสผ่านใหม่อย่างน้อย 8 ตัวอักษร">
      <input type="password" name="confirm_password" class="field" placeholder="ยืนยันรหัสผ่านใหม่">
      <button type="submit" class="primary-btn">บันทึกรหัสผ่านใหม่</button>
    </form>
    <div class="meta">username ยังคงเป็น <strong>admin</strong></div>
    <div class="meta" style="margin-top:8px;"><a href="/admin">กลับไปหน้า login</a></div>
  </div>
  <script>
    let seconds = __INITIAL_SECONDS__;
    const timerEl = document.getElementById('timer');
    function renderTimer() {
      if (seconds > 0) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        timerEl.textContent = `OTP ใช้ได้อีก ${m}:${s.toString().padStart(2, '0')}`;
        timerEl.style.color = '#ffaa00';
      } else {
        timerEl.textContent = 'ยังไม่ได้ส่ง OTP หรือ OTP หมดอายุแล้ว';
        timerEl.style.color = '#888';
      }
    }
    renderTimer();
    const timer = setInterval(() => {
      if (seconds > 0) {
        seconds--;
        renderTimer();
      }
    }, 1000);

    async function sendResetOtp() {
      const res = await fetch('/admin/reset/send', { method: 'POST' });
      const msg = document.getElementById('msg');
      if (!res.ok) {
        msg.textContent = '❌ ส่ง OTP ไม่สำเร็จ';
        msg.style.color = '#ff4444';
        return;
      }
      const data = await res.json();
      if (data.ok) {
        seconds = Number(data.expires_in || 300);
        renderTimer();
        msg.textContent = '✅ ส่ง OTP ไป Telegram แล้ว';
        msg.style.color = '#00ff88';
      } else if (typeof data.wait === 'number') {
        msg.textContent = `กรุณารอ ${data.wait} วินาที`;
        msg.style.color = '#ffaa00';
      } else {
        msg.textContent = '❌ ส่ง OTP ไม่สำเร็จ';
        msg.style.color = '#ff4444';
      }
    }
  </script>
</body>
</html>"""
    timer_copy = "OTP พร้อมใช้งาน" if has_valid_otp else "ยังไม่ได้ส่ง OTP หรือ OTP หมดอายุแล้ว"
    return HTMLResponse(
        reset_html
        .replace("__INITIAL_SECONDS__", str(initial_seconds))
        .replace("__TIMER_COPY__", escape(timer_copy))
    )


@app.post("/admin/reset/send")
async def admin_reset_send(request: Request):
    now = time.time()
    async with _admin_otp_lock:
        otp_state = await _get_admin_reset_otp_state()
        try:
            last_sent = float(otp_state.get(_ADMIN_RESET_OTP_LAST_SENT_KEY, "0") or 0)
        except Exception:
            last_sent = 0.0
        if now - last_sent < 60:
            remaining = int(60 - (now - last_sent))
            return JSONResponse({"ok": False, "wait": remaining})

        otp = _generate_otp()
        await _store_admin_reset_otp(otp, now + OTP_EXPIRE, now)
        await _send_otp_telegram(otp, title="Ener-AI Admin Password Reset OTP")
    return JSONResponse({"ok": True, "expires_in": OTP_EXPIRE})


@app.post("/admin/reset/confirm")
async def admin_reset_confirm(request: Request):
    form = await request.form()
    otp = str(form.get("otp", "")).strip()
    new_password = str(form.get("new_password", "")).strip()
    confirm_password = str(form.get("confirm_password", "")).strip()
    validation_error = _validate_new_admin_password(new_password, confirm_password)
    if validation_error:
        return HTMLResponse(
            f"""
        <script>
        alert({json.dumps(validation_error, ensure_ascii=False)});
        window.location.href = '/admin/reset';
        </script>
        """
        )

    now = time.time()
    otp_state = await _get_admin_reset_otp_state()
    stored_otp = otp_state.get(_ADMIN_RESET_OTP_CODE_KEY, "")
    try:
        otp_expires_at = float(otp_state.get(_ADMIN_RESET_OTP_EXPIRE_KEY, "0") or 0)
    except Exception:
        otp_expires_at = 0.0

    if not stored_otp or otp != stored_otp or now > otp_expires_at:
        return HTMLResponse(
            """
        <script>
        alert('OTP ไม่ถูกต้องหรือหมดอายุแล้วครับ');
        window.location.href = '/admin/reset';
        </script>
        """
        )

    await _set_admin_password(new_password)
    await _clear_admin_reset_otp()
    await _clear_admin_otp()
    await _clear_all_admin_sessions()
    response = HTMLResponse(
        """
    <script>
    alert('เปลี่ยนรหัสผ่านเรียบร้อยแล้ว ใช้ username: admin และรหัสใหม่เพื่อเข้าใช้งาน');
    window.location.href = '/admin';
    </script>
    """
    )
    response.delete_cookie("admin_session")
    return response


@app.post("/admin/logout")
async def logout(request: Request):
    token = request.cookies.get("admin_session", "")
    if token:
        await _delete_admin_session(token)
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("admin_session")
    return response


@app.post("/admin/config/update")
async def admin_config_update(request: Request):
    await _verify_admin_session(request)
    body = await request.json()
    key = str(body.get("key", "")).strip()
    value = str(body.get("value", "")).strip()
    if not key:
        raise HTTPException(status_code=400, detail="key required")
    if key == "active_model":
        if value not in {"auto", "haiku", "groq", "gemini", "qwen3b", "qwen7b"}:
            raise HTTPException(status_code=400, detail="active_model ไม่ถูกต้อง")
        if value == "haiku" and not settings.anthropic_api_key:
            raise HTTPException(status_code=400, detail="Claude Haiku ยังไม่มี key")
        if value == "groq" and not settings.groq_api_key:
            raise HTTPException(status_code=400, detail="Groq ยังไม่มี key")
        if value == "gemini" and not settings.gemini_api_key:
            raise HTTPException(status_code=400, detail="Gemini ยังไม่มี key")
    await set_config(key, value)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("admin_config_updated", f"{key} updated"),
        )
        await db.commit()
    if key == "active_model" and value != "auto":
        import asyncio as _asyncio

        _asyncio.create_task(_generate_model_handoff(value))
    return JSONResponse({"ok": True})


@app.post("/admin/config/test-line")
async def admin_test_line(request: Request):
    await _verify_admin_session(request)
    from app.agents.standup_agent import send_to_line

    ok, msg = await send_to_line("🧪 ทดสอบการส่ง LINE จาก Ener-AI")
    return JSONResponse({"ok": ok, "message": msg})


@app.get("/admin/metrics")
async def admin_metrics_dashboard(request: Request):
    await _require_admin(request)
    status, metrics = await asyncio.gather(_load_admin_status(), _load_metrics_payload("10h"))
    return build_metrics_html(status, metrics)


@app.get("/admin/logs")
async def admin_logs(request: Request):
    await _require_admin(request)
    return build_logs_html()


@app.get("/admin/terminal")
async def terminal_page(request: Request):
    await _require_admin(request)
    now = time.time()
    async with _terminal_otp_lock:
        otp_state = await _get_terminal_otp_state()
        otp_code = otp_state.get(_TERMINAL_OTP_CODE_KEY, "")
        try:
            otp_expires_at = float(otp_state.get(_TERMINAL_OTP_EXPIRE_KEY, "0") or 0)
        except Exception:
            otp_expires_at = 0.0
        try:
            last_sent = float(otp_state.get(_TERMINAL_OTP_LAST_SENT_KEY, "0") or 0)
        except Exception:
            last_sent = 0.0
        has_valid_otp = bool(otp_code) and otp_expires_at > now
        recently_sent = now - last_sent < _OTP_SEND_COOLDOWN
        if not has_valid_otp and not recently_sent:
            otp = _generate_otp()
            otp_expires_at = now + OTP_EXPIRE
            await _store_terminal_otp(otp, otp_expires_at, now)
            await _send_otp_telegram(otp, title="Ener-AI Terminal OTP")
            terminal_status_copy = "📱 ส่ง Terminal OTP ไป Telegram แล้ว"
        else:
            terminal_status_copy = "📱 Terminal OTP ยังไม่หมดอายุ กรอกได้เลย"
    initial_seconds = max(1, min(OTP_EXPIRE, int(otp_expires_at - now)))
    server_name = escape(str(getattr(settings, "server_host", "") or "my-ener.uk"))
    terminal_html = """<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Terminal - Ener-AI</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #000; color: #fff; font-family: monospace; }}
    .term-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 16px;
      background: #111;
      border-bottom: 1px solid #222;
    }}
    .term-header a {{ color: #888; text-decoration: none; font-size: 12px; }}
    .term-header a:hover {{ color: #fff; }}
    #terminal-login {{
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: calc(100vh - 40px);
      gap: 12px;
    }}
    #terminal-login input {{
      padding: 10px 16px;
      background: #111;
      border: 1px solid #333;
      color: #fff;
      border-radius: 6px;
      font-size: 16px;
      width: 280px;
    }}
    #terminal-login button {{
      padding: 10px 24px;
      background: #00ff88;
      color: #000;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-weight: bold;
    }}
    #terminal-container {{ display: none; }}
    #terminal {{ height: calc(100vh - 40px); padding: 4px; }}
    #drop-zone {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,255,136,0.1);
      border: 3px dashed #00ff88;
      z-index: 999;
      align-items: center;
      justify-content: center;
      font-size: 24px;
      color: #00ff88;
    }}
    #drop-zone.active {{ display: flex; }}
    #upload-progress {{
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: #111;
      border: 1px solid #333;
      padding: 12px 16px;
      border-radius: 8px;
      font-size: 12px;
      display: none;
      min-width: 200px;
      z-index: 1000;
    }}
    .progress-bar {{
      height: 4px;
      background: #222;
      border-radius: 2px;
      margin-top: 8px;
    }}
    .progress-fill {{
      height: 4px;
      background: #00ff88;
      border-radius: 2px;
      width: 0%;
      transition: width 0.3s;
    }}
  </style>
</head>
<body>
  <div class="term-header">
    <span>⚡ Ener-AI Terminal - __SERVER_NAME__</span>
    <div style="display:flex;gap:12px;align-items:center">
      <span style="font-size:11px;color:#888">ผ่าน 2 ชั้น auth ก่อนใช้งาน terminal</span>
      <a href="/admin">← Admin</a>
    </div>
  </div>

  <div id="drop-zone">📁 วางไฟล์ที่นี่เพื่ออัปโหลด</div>
  <div id="upload-progress">
    <div id="upload-filename">กำลังอัปโหลด...</div>
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
    <div id="upload-status" style="color:#888;margin-top:4px;font-size:10px"></div>
  </div>
  <div id="terminal-login">
    <h2>🔐 Terminal Access</h2>
    <p>กรอก Terminal OTP เพื่อเข้าใช้งาน</p>
    <div style="color:#00ff88;font-size:12px;margin-bottom:8px">__TERMINAL_STATUS__</div>
    <input type="text" id="term-pass" placeholder="Terminal OTP" maxlength="6" onkeydown="if(event.key==='Enter') verifyTerminal()" oninput="this.value=this.value.replace(/[^0-9]/g,'')">
    <button onclick="verifyTerminal()">เข้าใช้งาน</button>
    <div id="term-timer" style="color:#ffaa00;font-size:12px;margin-top:8px">หมดอายุใน 5:00</div>
    <button style="margin-top:10px;background:none;color:#777;border:none;text-decoration:underline" onclick="resendTerminalOtp()">ส่ง OTP ใหม่</button>
    <p id="term-error" style="color:red;display:none">OTP ไม่ถูกต้อง</p>
  </div>
  <div id="terminal-container">
    <div id="terminal"></div>
  </div>

  <script>
    let terminalSeconds = __INITIAL_TERMINAL_SECONDS__;
    let term = null;
    let fitAddon = null;
    let ws = null;
    let terminalInputBound = false;
    const terminalTimer = setInterval(() => {
      terminalSeconds -= 1;
      const m = Math.floor(terminalSeconds / 60);
      const s = terminalSeconds % 60;
      const timerEl = document.getElementById('term-timer');
      if (!timerEl) return;
      timerEl.textContent = `หมดอายุใน ${m}:${String(Math.max(0, s)).padStart(2, '0')}`;
      if (terminalSeconds <= 0) {
        clearInterval(terminalTimer);
        timerEl.textContent = '⏰ Terminal OTP หมดอายุแล้ว';
        timerEl.style.color = '#ff4444';
      }
    }, 1000);

    function initTerminal(token) {{
      document.getElementById('terminal-login').style.display = 'none';
      document.getElementById('terminal-container').style.display = 'block';

      if (!term) {{
        term = new Terminal({{
          theme: {{
            background: '#000000',
            foreground: '#ffffff',
            cursor: '#00ff88',
            selection: 'rgba(0,255,136,0.3)',
          }},
          fontFamily: 'JetBrains Mono, Cascadia Code, monospace',
          fontSize: 14,
          cursorBlink: true,
        }});
        fitAddon = new FitAddon.FitAddon();
        term.loadAddon(fitAddon);
        term.open(document.getElementById('terminal'));
        fitAddon.fit();
        window.addEventListener('resize', () => fitAddon.fit());
      }}

      if (!terminalInputBound) {{
        term.onData((data) => {{
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(data);
        }});
        terminalInputBound = true;
      }}

      const wsScheme = location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${{wsScheme}}://${{location.host}}/admin/terminal/ws?token=${{encodeURIComponent(token)}}`;
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {{
        term.writeln('\\x1b[32m✅ เชื่อมต่อ Terminal สำเร็จ\\x1b[0m');
        term.writeln('\\x1b[90mTip: ลากไฟล์มาวางเพื่ออัปโหลดไปที่ /root/ener-ai/data/\\x1b[0m');
        term.write('\\r\\n');
      }};
      ws.onmessage = (event) => term.write(event.data);
      ws.onclose = () => term.writeln('\\r\\n\\x1b[31m❌ การเชื่อมต่อปิดแล้ว\\x1b[0m');
      ws.onerror = () => term.writeln('\\r\\n\\x1b[31m❌ WebSocket error\\x1b[0m');
    }}

    async function verifyTerminal() {{
      const otp = document.getElementById('term-pass').value;
      const errorEl = document.getElementById('term-error');
      errorEl.style.display = 'none';

      const res = await fetch('/admin/terminal/verify', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        credentials: 'same-origin',
        body: JSON.stringify({{ otp }}),
      }});

      if (res.ok) {{
        const data = await res.json();
        initTerminal(data.token);
      }} else {{
        errorEl.style.display = 'block';
      }}
    }}

    async function resendTerminalOtp() {{
      const res = await fetch('/admin/terminal/resend', {{
        method: 'POST',
        credentials: 'same-origin',
      }});
      const errorEl = document.getElementById('term-error');
      if (!res.ok) {{
        errorEl.textContent = 'ส่ง OTP ไม่สำเร็จ';
        errorEl.style.display = 'block';
        return;
      }}
      const data = await res.json();
      if (data.ok) {{
        errorEl.textContent = '✅ ส่ง Terminal OTP ใหม่แล้ว';
        errorEl.style.color = '#00ff88';
        errorEl.style.display = 'block';
        terminalSeconds = 300;
        const timerEl = document.getElementById('term-timer');
        if (timerEl) timerEl.style.color = '#ffaa00';
      }} else if (typeof data.wait === 'number') {{
        errorEl.textContent = `กรุณารอ ${{data.wait}} วินาที`;
        errorEl.style.color = '#ffaa00';
        errorEl.style.display = 'block';
      }}
    }}

    const dropZone = document.getElementById('drop-zone');
    let dragCounter = 0;

    document.addEventListener('dragenter', (event) => {{
      event.preventDefault();
      dragCounter += 1;
      dropZone.classList.add('active');
    }});
    document.addEventListener('dragleave', () => {{
      dragCounter = Math.max(0, dragCounter - 1);
      if (dragCounter === 0) dropZone.classList.remove('active');
    }});
    document.addEventListener('dragover', (event) => event.preventDefault());
    document.addEventListener('drop', (event) => {{
      event.preventDefault();
      dragCounter = 0;
      dropZone.classList.remove('active');

      const files = event.dataTransfer.files;
      if (files.length > 0) uploadFile(files[0]);
    }});

    function uploadFile(file) {{
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const progressEl = document.getElementById('upload-progress');
      const fillEl = document.getElementById('progress-fill');
      const nameEl = document.getElementById('upload-filename');
      const statusEl = document.getElementById('upload-status');

      progressEl.style.display = 'block';
      nameEl.textContent = `อัปโหลด: ${{file.name}}`;
      fillEl.style.width = '0%';
      statusEl.textContent = '0%';

      const formData = new FormData();
      formData.append('file', file);
      formData.append('path', '/root/ener-ai/data/');

      const xhr = new XMLHttpRequest();
      xhr.upload.onprogress = (event) => {{
        if (event.lengthComputable) {{
          const pct = Math.round(event.loaded / event.total * 100);
          fillEl.style.width = pct + '%';
          statusEl.textContent = pct + '%';
        }}
      }};
      xhr.onload = () => {{
        if (xhr.status === 200) {{
          const res = JSON.parse(xhr.responseText);
          statusEl.textContent = `✅ บันทึกที่ ${{res.path}}`;
          term.writeln(`\\r\\n\\x1b[32m✅ อัปโหลด ${{file.name}} → ${{res.path}}\\x1b[0m`);
          setTimeout(() => {{ progressEl.style.display = 'none'; }}, 3000);
        }} else {{
          statusEl.textContent = '❌ อัปโหลดล้มเหลว';
        }}
      }};
      xhr.onerror = () => {{
        statusEl.textContent = '❌ อัปโหลดล้มเหลว';
      }};

      xhr.open('POST', '/admin/upload');
      xhr.send(formData);
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(
        terminal_html
        .replace("__SERVER_NAME__", server_name)
        .replace("__TERMINAL_STATUS__", escape(terminal_status_copy))
        .replace("__INITIAL_TERMINAL_SECONDS__", str(initial_seconds))
        .replace("{{", "{")
        .replace("}}", "}")
    )


@app.post("/admin/terminal/verify")
async def verify_terminal(request: Request):
    await _require_admin(request)

    body = await request.json()
    otp = str(body.get("otp", "")).strip()
    now = time.time()
    otp_state = await _get_terminal_otp_state()
    stored_otp = otp_state.get(_TERMINAL_OTP_CODE_KEY, "")
    try:
        otp_expires_at = float(otp_state.get(_TERMINAL_OTP_EXPIRE_KEY, "0") or 0)
    except Exception:
        otp_expires_at = 0.0
    if not stored_otp or otp != stored_otp or now > otp_expires_at:
        raise HTTPException(status_code=401, detail="Invalid terminal OTP")

    await _clear_terminal_otp()
    _prune_terminal_tokens(now)
    current_password = await _get_admin_password()
    token = hashlib.sha256(f"{otp}{now}{current_password}".encode()).hexdigest()[:32]
    _terminal_tokens[token] = now
    return {"token": token}


@app.post("/admin/terminal/resend")
async def resend_terminal_otp(request: Request):
    await _require_admin(request)
    otp_state = await _get_terminal_otp_state()
    now = time.time()
    try:
        last_sent = float(otp_state.get(_TERMINAL_OTP_LAST_SENT_KEY, "0") or 0)
    except Exception:
        last_sent = 0.0
    if now - last_sent < 60:
        remaining = int(60 - (now - last_sent))
        return {"ok": False, "wait": remaining}

    otp = _generate_otp()
    await _store_terminal_otp(otp, now + OTP_EXPIRE, now)
    await _send_otp_telegram(otp, title="Ener-AI Terminal OTP")
    return {"ok": True}


@app.websocket("/admin/terminal/ws")
async def terminal_ws(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    now = time.time()
    _prune_terminal_tokens(now)
    issued_at = _terminal_tokens.get(token)
    if not issued_at or now - issued_at > _TERMINAL_TOKEN_TTL_SECONDS:
        await websocket.close(code=4001)
        return
    await handle_terminal_ws(websocket)


@app.post("/admin/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    path: str = Form(default="/root/ener-ai/data/"),
):
    await _require_admin(request)

    target_dir = _resolve_upload_dir(path)
    filename = Path(file.filename or "upload.bin").name
    if filename in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="ชื่อไฟล์ไม่ถูกต้อง")

    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / filename

    try:
        with destination.open("wb") as output:
            shutil.copyfileobj(file.file, output)
    finally:
        await file.close()

    return {
        "success": True,
        "path": str(destination),
        "size": destination.stat().st_size,
    }


async def _generate_model_handoff(new_model: str) -> None:
    """Summarise recent messages so the new model knows what was discussed."""
    try:
        from app.core.ai import chat_json

        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT role, content FROM messages
                ORDER BY id DESC LIMIT 10
                """
            )
            rows = await cursor.fetchall()
        if not rows:
            return
        convo = "\n".join(
            f"{'กบ' if row['role'] == 'user' else 'AI'}: {str(row['content'] or '')[:200]}"
            for row in reversed(rows)
        )
        result = await chat_json(
            f"สรุปบทสนทนานี้ใน 2-3 ประโยคสั้นๆ เพื่อให้ AI ตัวใหม่รับช่วงต่อได้:\n\n{convo}",
            system='คุณคือตัวช่วยสรุปบทสนทนา ตอบเป็น JSON: {"summary": "...สรุปสั้นๆ ภาษาไทย..."}',
            agent="system",
        )
        summary = str(result.get("summary", "")).strip()
        if not summary:
            return
        handoff = f"[Model เพิ่ง switch มาเป็น {new_model}] บทสนทนาก่อนหน้า: {summary}"
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO memories (key, value, tag)
                VALUES ('model_handoff_context', ?, 'system')
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    tag = excluded.tag,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (handoff,),
            )
            await db.commit()
    except Exception:
        pass


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

    await set_config("active_model", model)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("admin_model_switched", f"model={model}"),
        )
        await db.commit()
    import asyncio as _asyncio

    _asyncio.create_task(_generate_model_handoff(model))
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
    log_entries = await _load_log_entries(filter_value, lines)
    return JSONResponse(
        {
            "lines": log_entries,
            "logs": [
                " ".join(
                    part for part in [f"[{entry.get('time', '--:--')}]", entry.get("level", ""), entry.get("message", "")]
                    if part
                ).strip()
                for entry in log_entries
            ],
        }
    )
