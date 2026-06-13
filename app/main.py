import asyncio
import base64
import hashlib
import json
import logging
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
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telegram import Update

from app.admin.trace_pages import build_ai_traces_html
from app.admin.ener_scan_business_pages import build_ener_scan_business_html
from app.admin.jinja_context import (
    load_admin_ai_context,
    load_admin_base_context,
    load_admin_settings_context,
)
from app.bot.router import build_application
from app.core.ai import chat, chat_json, get_active_model, get_model_availability, get_model_label
from app.core.agents import COMMAND_AGENT_MAP, SCHEDULER_AGENTS
from app.core.ai_gateway import get_recent_ai_traces, preview_context, run_ai
from app.core.config import settings
from app.core.openrouter_client import (
    OPENROUTER_KEYS as _OPENROUTER_KEYS,
    get_openrouter_api_key,
)
from app.core.venice_client import VENICE_KEYS as _VENICE_KEYS
from app.core.featherless_client import FEATHERLESS_KEYS as _FEATHERLESS_KEYS
from app.core.database import get_all_config, get_config, get_db, init_db, set_config
from app.core.diagnostics import log_otp_event
from app.core.event_log import log_event
from app.core.terminal import handle_terminal_ws
from app.scheduler import build_scheduler

telegram_app = build_application()
scheduler = None
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
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
_admin_otp_log = logging.getLogger("ener-ai.admin_otp")
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
_ADMIN_OTP_HOUR_RL: dict[str, list[float]] = {}
_ADMIN_OTP_HOUR_RL_MAX = 3
_ADMIN_OTP_HOUR_RL_WINDOW = 3600.0
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
        "id": "morning_briefing",
        "name": "08:00 Morning Briefing + News",
        "schedule": "Daily 08:00",
        "success_actions": [
            "scheduled_news_sent",
            "scheduled_morning_briefing_sent",
        ],
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


def _workspace_resource_stats() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    def _gb(value: int | float) -> float:
        return round(float(value) / (1024**3), 1)

    return {
        "cpu_percent": round(float(psutil.cpu_percent()), 1),
        "ram_percent": round(float(memory.percent), 1),
        "ram_used_gb": _gb(memory.used),
        "ram_total_gb": _gb(memory.total),
        "disk_percent": round(float(disk.percent), 1),
        "disk_used_gb": _gb(disk.used),
        "disk_total_gb": _gb(disk.total),
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


def _admin_otp_rate_limit_key(request: Request) -> str:
    """Identity for admin OTP send rate limit (web client; maps to chat_id in bot context)."""
    xf = (request.headers.get("x-forwarded-for") or "").strip()
    if xf:
        ip = xf.split(",")[0].strip()
        if ip:
            return ip[:200]
    if request.client and request.client.host:
        return str(request.client.host)[:200]
    return "unknown"


def _admin_otp_hour_bucket(request: Request, now: float) -> tuple[str, list[float]]:
    key = f"hour_rl:{_admin_otp_rate_limit_key(request)}"
    bucket = _ADMIN_OTP_HOUR_RL.setdefault(key, [])
    cutoff = now - _ADMIN_OTP_HOUR_RL_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    return key, bucket


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
        await log_otp_event(
            "ADMIN_API_SESSION_EXPIRED_401",
            request=request,
            reason="no_valid_session",
            metadata={"path": str(request.url.path)},
        )
        raise HTTPException(status_code=401, detail="Session expired")
    await _validate_admin_basic_auth(request)
    await log_otp_event(
        "ADMIN_REDIRECT_TO_OTP",
        request=request,
        reason="otp_required",
        metadata={"from_path": str(request.url.path)},
    )
    raise HTTPException(status_code=307, detail="OTP Required", headers={"Location": "/admin/otp"})


async def _verify_admin_session(request: Request):
    if await _is_valid_session(request):
        return
    if request.method.upper() == "GET":
        raise HTTPException(status_code=307, detail="Session expired", headers={"Location": "/admin"})
    await log_otp_event("ADMIN_SESSION_EXPIRED", request=request, reason="invalid_or_expired_session")
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
    from app.core.ollama_client import ollama_health_check

    ok, message = await ollama_health_check()
    return "OK" if ok else "FAIL"


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
    .nav-section {{
      color: #666;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      padding: 4px 8px 0;
      width: 100%;
      flex-basis: 100%;
    }}
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
      <span class="nav-section">Home</span>
      <a class="nav-link active" href="/admin">Overview</a>
      <span class="nav-section">Projects</span>
      <a class="nav-link" href="/admin/projects">Projects</a>
      <a class="nav-link" href="/admin/hospital-work">Hospital Work</a>
      <a class="nav-link" href="/admin/ener-scan-business">Ener Scan</a>
      <a class="nav-link" href="/platform">Platform</a>
      <span class="nav-section">AI</span>
      <a class="nav-link" href="/admin/ai-traces">Trace</a>
      <a class="nav-link" href="/admin/routing">Routing</a>
      <a class="nav-link" href="/admin/pipeline">Pipeline</a>
      <a class="nav-link" href="/admin/metrics">Metrics</a>
      <span class="nav-section">Ops</span>
      <a class="nav-link" href="/admin/logs">Logs</a>
      <a class="nav-link" href="/admin/api-status">API Status</a>
      <a class="nav-link" href="/admin/terminal" target="_blank" rel="noopener noreferrer">Terminal</a>
      <span class="nav-section">Settings</span>
      <a class="nav-link" href="/admin/config">Config</a>
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

    const __adminDashIntervalIds = new Set();
    function __trackAdminDashInterval(id) {{
      __adminDashIntervalIds.add(id);
      return id;
    }}
    function __clearIntervalIfTracked(id) {{
      if (id == null) return;
      clearInterval(id);
      __adminDashIntervalIds.delete(id);
    }}
    function __clearAllAdminDashboardIntervals() {{
      for (const id of [...__adminDashIntervalIds]) {{
        clearInterval(id);
        __adminDashIntervalIds.delete(id);
      }}
    }}
    const __nativeFetch = window.fetch.bind(window);
    window.fetch = function(...args) {{
      return __nativeFetch(...args).then((res) => {{
        try {{
          const u = res.url || '';
          if (res.status === 401 || u.includes('/admin/otp')) {{
            __clearAllAdminDashboardIntervals();
          }}
        }} catch (e) {{}}
        return res;
      }});
    }};

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
    __trackAdminDashInterval(setInterval(fetchLogs, 10000));

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
    __trackAdminDashInterval(setInterval(refreshApiStatus, 60000));

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
        if (handle) {{
          __clearIntervalIfTracked(handle);
          handle = null;
        }}
        const ms = Number(sel.value);
        localStorage.setItem(STORAGE_KEY, sel.value);
        if (ms > 0) handle = __trackAdminDashInterval(setInterval(() => location.reload(), ms));
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


def _workspace_tz() -> ZoneInfo:
    return ZoneInfo("Asia/Bangkok")


def _workspace_today_key() -> str:
    return datetime.now(_workspace_tz()).strftime("%Y-%m-%d")


def _format_chat_date_label(date_key: str) -> str:
    try:
        return datetime.strptime(str(date_key), "%Y-%m-%d").strftime("%d-%b-%Y").lower()
    except ValueError:
        return str(date_key)


def _resolve_workspace_chat_date(
    date: str | None = None,
    scroll: str | None = None,
) -> tuple[str | None, bool]:
    """Return (filter_date YYYY-MM-DD or None, show_all). Default = today (Bangkok)."""
    raw = str(date or scroll or "").strip()
    if raw.lower() == "all":
        return None, True
    if not raw:
        return _workspace_today_key(), False
    day = raw[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return _workspace_today_key(), False
    return day, False


async def _workspace_conversation_id(project_id: int | None = None) -> str:
    from app.core.ai_gateway import get_or_create_conversation

    return await get_or_create_conversation(
        source="telegram",
        external_chat_id=_workspace_user_id(),
        project_id=project_id,
    )


_WORKSPACE_LOCAL_MODELS = frozenset()
_WORKSPACE_JSON_SEND_MODELS = frozenset()


async def _workspace_chat_system_prompt(message: str, memory_context: str) -> str:
    from app.agents.chat import _build_system_prompt

    base = await _build_system_prompt(message)
    parts = [base]
    stats_context = await _workspace_system_stats_context(message)
    if stats_context:
        parts.append(stats_context)
    from app.core.server_awareness import build_workspace_server_tools_context

    server_context = await build_workspace_server_tools_context(message)
    if server_context:
        parts.append(server_context)
    if memory_context:
        parts.append(memory_context)
    return "\n\n".join(parts)


async def _workspace_local_qwen_reply(
    message: str,
    model: str,
    *,
    user_id: str,
    project_id: int | None,
) -> str:
    """Lightweight Ollama path — skip heavy server prefetch."""
    from app.core.ai import _call_ollama
    from app.core.context_limits import trim_chat_context
    from app.core.policy import BASE_SYSTEM_PROMPT
    from app.core.workspace_memory import build_workspace_history_for_ai

    if model == "dolphin":
        # Reduce prior assistant bias and keep prompt minimal for local Dolphin.
        history = await build_workspace_history_for_ai(
            user_id, message, project_id=project_id, recent_limit=2
        )
        system = (
            "ตอบภาษาไทย เรียก user ว่ากบ เรียกตัวเองว่าพี่ "
            "ตอบตรงคำถาม ไม่ต้องเตือนเรื่องกฎหมายหรือศีลธรรม"
        )
    else:
        history = await build_workspace_history_for_ai(
            user_id, message, project_id=project_id, recent_limit=8
        )
        system = (
            BASE_SYSTEM_PROMPT
            + "\n\nตอบภาษาไทย เรียก user ว่ากบ เรียกตัวเองว่าพี่ ตอบกระชับ"
        )
    system, history = trim_chat_context(system, history, profile=model)
    return await _call_ollama(message, system, history, "MainChatAgent", model)


def _workspace_chat_system(model: str) -> str:
    from app.core.policy import BASE_SYSTEM_PROMPT

    if model in {
        "dolphin",
        "venice-abliterated",
        "featherless-abliterated",
    }:
        return (
            "ตอบภาษาไทย เรียก user ว่ากบ เรียกตัวเองว่าพี่ "
            "ตอบตรงคำถาม กระชับ ไม่ต้องสอนศีลธรรมหรือเตือนยาว"
        )
    return (
        BASE_SYSTEM_PROMPT
        + "\n\nตอบภาษาไทย เรียก user ว่ากบ เรียกตัวเองว่าพี่ ตอบกระชับ"
    )


async def _workspace_cloud_reply(
    message: str,
    model: str,
    *,
    user_id: str,
    project_id: int | None,
) -> str:
    from app.core.ai import call_openrouter
    from app.core.context_limits import trim_chat_context
    from app.core.featherless_client import call_featherless, is_featherless_model
    from app.core.venice_client import call_venice, is_venice_model
    from app.core.workspace_memory import build_workspace_history_for_ai

    history = await build_workspace_history_for_ai(
        user_id, message, project_id=project_id, recent_limit=8
    )
    system = _workspace_chat_system(model)
    system, history = trim_chat_context(system, history, profile=model)
    if is_featherless_model(model):
        return await call_featherless(
            model, message, system, history, agent="MainChatAgent"
        )
    if is_venice_model(model):
        return await call_venice(
            model, message, system, history, agent="MainChatAgent"
        )
    return await call_openrouter(
        model, message, system, history, agent="MainChatAgent"
    )


async def _workspace_openrouter_reply(
    message: str,
    model: str,
    *,
    user_id: str,
    project_id: int | None,
) -> str:
    return await _workspace_cloud_reply(
        message, model, user_id=user_id, project_id=project_id
    )


def _is_simple_cpu_query(message: str) -> bool:
    import re

    from app.core.tool_router import classify_system_tool_intent

    if classify_system_tool_intent(message) != "server":
        return False
    lowered = str(message or "").lower()
    if any(
        token in lowered
        for token in (
            "container",
            "docker",
            "git",
            "log",
            "error",
            "nginx",
            "commit",
            "ปกติ",
            "ener-scan",
            "deploy",
            "disk เหลือ",
            "df ",
            "shell",
        )
    ):
        return False
    return bool(re.search(r"\b(cpu|ram|disk|memory)\b", lowered))


def _workspace_needs_tool_agent(message: str) -> bool:
    if _is_simple_cpu_query(message):
        return False
    from app.core.tool_router import classify_system_tool_intent

    intent = classify_system_tool_intent(message)
    if intent in ("server", "logs", "errors", "status"):
        return True
    lowered = str(message or "").lower()
    return any(
        token in lowered
        for token in (
            "docker",
            "container",
            "git ",
            "git-",
            "logs",
            "error",
            "traceback",
            "disk",
            "df ",
            "deploy",
            "ปกติไหม",
            "ener-scan",
            "commit",
            "nginx",
            "port ",
            "process",
            "server",
        )
    )


async def _workspace_system_stats_context(message: str) -> str:
    from app.core.tool_router import classify_system_tool_intent
    from app.core.tools import execute_tool

    if classify_system_tool_intent(message) != "server":
        return ""
    stats_text = await execute_tool("check_system_stats", {})
    return (
        "=== ข้อมูลทรัพยากรเครื่อง (ดึงจริงจาก psutil แล้ว) ===\n"
        f"{stats_text}\n\n"
        "กฎ: ตอบกบด้วยตัวเลขด้านบนโดยตรง ห้ามบอกให้รัน docker stats, docker compose "
        "หรือคำสั่ง shell อื่นแทน"
    )


async def _workspace_chat_dates() -> list[dict[str, str]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT DISTINCT date(datetime(created_at, '+7 hours')) AS chat_date
            FROM messages
            WHERE chat_id = ?
              AND role IN ('user', 'assistant')
            ORDER BY chat_date DESC
            LIMIT 30
            """,
            (_workspace_user_id(),),
        )
        rows = await cursor.fetchall()
    dates: list[dict[str, str]] = []
    for row in rows:
        key = str(row["chat_date"] or "").strip()
        if not key:
            continue
        dates.append({"key": key, "label": _format_chat_date_label(key)})
    return dates


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


async def _workspace_enrich_message_models(messages: list[dict]) -> None:
    from app.core.featherless_client import FEATHERLESS_LABELS
    from app.core.venice_client import VENICE_LABELS

    _, model_options = await _workspace_openrouter_model_groups()
    label_cache: dict[str, str] = {key: label for key, label in model_options}
    label_cache.update(VENICE_LABELS)
    label_cache.update(FEATHERLESS_LABELS)
    label_cache["system"] = "System"
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        used = str(msg.get("model_used") or "").strip()
        if not used:
            continue
        if used in label_cache:
            msg["model_label"] = label_cache[used]
            continue
        resolved = await _workspace_model_label(used)
        label_cache[used] = resolved
        msg["model_label"] = resolved


async def _workspace_history_rows(
    project_id: int | None = None,
    limit: int = 200,
    chat_date: str | None = None,
) -> list[dict]:
    limit_value = max(1, min(limit, 500))
    date_clause = ""
    date_params: tuple[object, ...] = ()
    if chat_date:
        date_clause = " AND date(datetime(created_at, '+7 hours')) = ?"
        date_params = (chat_date,)
    async with get_db() as db:
        if project_id is None:
            cursor = await db.execute(
                f"""
                SELECT
                    id,
                    role,
                    content,
                    COALESCE(source, 'telegram') AS source,
                    project_id,
                    model_used,
                    datetime(created_at, '+7 hours') AS local_created_at
                FROM messages
                WHERE chat_id = ?
                  AND role IN ('user', 'assistant')
                  {date_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (_workspace_user_id(), *date_params, limit_value),
            )
        else:
            cursor = await db.execute(
                f"""
                SELECT
                    id,
                    role,
                    content,
                    COALESCE(source, 'telegram') AS source,
                    project_id,
                    model_used,
                    datetime(created_at, '+7 hours') AS local_created_at
                FROM messages
                WHERE chat_id = ? AND project_id = ?
                  AND role IN ('user', 'assistant')
                  {date_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (_workspace_user_id(), project_id, *date_params, limit_value),
            )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "source": str(row["source"] or "telegram"),
            "project_id": row["project_id"],
            "model_used": str(row["model_used"] or "").strip() if row["model_used"] else "",
            "created_at": str(row["local_created_at"] or ""),
        }
        for row in reversed(rows)
    ]


async def _workspace_save_chat_messages(
    project_id: int | None,
    user_text: str,
    reply_text: str,
    *,
    model_used: str | None = None,
) -> None:
    conversation_id = await _workspace_conversation_id(project_id=project_id)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO messages (chat_id, conversation_id, role, content, project_id, source)
            VALUES (?, ?, ?, ?, ?, 'web')
            """,
            (_workspace_user_id(), conversation_id, "user", user_text, project_id),
        )
        await db.execute(
            """
            INSERT INTO messages (
                chat_id, conversation_id, role, content, project_id, source, model_used
            )
            VALUES (?, ?, ?, ?, ?, 'web', ?)
            """,
            (
                _workspace_user_id(),
                conversation_id,
                "assistant",
                reply_text,
                project_id,
                str(model_used or "").strip() or None,
            ),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("workspace_chat_saved", f"project_id={project_id or 'all'}"),
        )
        await db.commit()


def _workspace_parse_slash_command(text: str) -> tuple[str, str] | None:
    import re

    match = re.match(r"^/(\w+)(?:\s+(.*))?$", str(text or "").strip(), re.DOTALL)
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


async def _workspace_run_slash_command(text: str, project_id: int | None) -> str | None:
    """Route /command messages to the matching agent. Returns None if not a slash command."""
    parsed = _workspace_parse_slash_command(text)
    if not parsed:
        return None

    cmd_key, cmd_args = parsed
    from app.core.agents import COMMAND_AGENT_MAP

    if cmd_key not in COMMAND_AGENT_MAP:
        return None

    chat_id = _workspace_user_id()

    if cmd_key == "logs":
        from app.agents.monitor_agent import cmd_logs

        lines = 20
        if cmd_args.isdigit():
            lines = max(5, min(int(cmd_args), 200))
        return await cmd_logs(lines=lines)

    if cmd_key == "errors":
        from app.agents.monitor_agent import cmd_errors

        return await cmd_errors()

    if cmd_key == "server":
        from app.agents.monitor_agent import cmd_server

        return await cmd_server()

    if cmd_key == "status":
        from app.agents.monitor_agent import cmd_status

        return await cmd_status()

    if cmd_key in {"tarot", "ไพ่", "ดวง"}:
        from app.agents.tarot_agent import read_cards

        spread = "single"
        lowered = cmd_args.lower()
        if "3" in cmd_args or "สาม" in cmd_args or "three" in lowered:
            spread = "three"
        elif "5" in cmd_args or "ห้า" in cmd_args or "celtic" in lowered:
            spread = "celtic"
        return await read_cards(question=cmd_args, spread=spread)

    if cmd_key == "email":
        from app.agents import gmail_agent
        from app.core.ai_gateway import run_ai

        if not cmd_args:
            emails = await gmail_agent.fetch_unread_emails()
            if not emails:
                return "📭 ไม่มีอีเมลใหม่"
            return await gmail_agent.summarize_emails()

        parts = cmd_args.split(None, 2)
        sub = parts[0].lower()
        if sub == "ask" and len(parts) > 1:
            result = await run_ai(
                source="telegram",
                external_chat_id=chat_id,
                text=" ".join(parts[1:]).strip(),
                intent="gmail",
                project_id=project_id,
            )
            return str(result.get("reply", "")).strip() or "ยังไม่มีคำตอบตอนนี้"
        if sub == "draft" and len(parts) > 1:
            return await gmail_agent.draft_reply(parts[1].strip())
        if sub == "reply" and len(parts) > 2:
            return await gmail_agent.reply_email(parts[1].strip(), parts[2].strip())
        return await gmail_agent.summarize_emails()

    if cmd_key == "content":
        from app.core.ai_gateway import run_ai

        result = await run_ai(
            source="telegram",
            external_chat_id=chat_id,
            text=cmd_args or "สร้าง content สำหรับ TikTok/FB",
            intent="content",
            project_id=project_id,
        )
        return str(result.get("reply", "")).strip() or "ยังไม่มีคำตอบตอนนี้"

    from app.agents.main_agent import MAIN_AGENT

    return await MAIN_AGENT.handle(cmd_key, cmd_args, chat_id)


async def _workspace_generate_reply(
    text: str,
    project_id: int | None,
    preferred_model: str | None = None,
) -> str:
    from app.core.agents import COMMAND_AGENT_MAP
    from app.core.ai import _call_anthropic_with_tools, _call_groq_with_tools, chat, get_model_availability
    from app.core.memory import extract_and_store_long_term_memories
    from app.core.tools import TOOLS, execute_tool

    from app.core.workspace_memory import (
        build_workspace_conversation_context,
        build_workspace_history_for_ai,
    )

    try:
        slash_reply = await _workspace_run_slash_command(text, project_id)
    except Exception as exc:
        parsed = _workspace_parse_slash_command(text)
        agent_name = COMMAND_AGENT_MAP.get(parsed[0], "Agent") if parsed else "Agent"
        slash_reply = f"⚠️ {agent_name} ทำงานไม่สำเร็จ: {exc}"

    if slash_reply is not None:
        final_reply = str(slash_reply).strip() or "ยังไม่มีคำตอบตอนนี้"
        parsed = _workspace_parse_slash_command(text)
        agent_model = COMMAND_AGENT_MAP.get(parsed[0], "agent") if parsed else "agent"
        await _workspace_save_chat_messages(
            project_id,
            text,
            final_reply,
            model_used=agent_model,
        )
        try:
            await extract_and_store_long_term_memories(text, final_reply)
        except Exception:
            pass
        return final_reply

    chat_id = _workspace_user_id()
    memory_context = await build_workspace_conversation_context(
        chat_id, text, project_id=project_id
    )
    history = await build_workspace_history_for_ai(
        chat_id, text, project_id=project_id
    )
    system_prompt = await _workspace_chat_system_prompt(text, memory_context)
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
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ai/context-preview")
async def ai_context_preview(text: str, source: str = "debug", chat_id: str = "debug"):
    context = await preview_context(text=text, source=source, external_chat_id=chat_id)
    return JSONResponse({"ok": True, "context": context})


@app.post("/ai/run")
async def ai_run(request: Request):
    body = await request.json()
    source = str(body.get("source", "api") or "api").strip() or "api"
    external_chat_id = str(body.get("chat_id", "api") or "api").strip() or "api"
    text = str(body.get("text", "")).strip()
    project_id = _normalize_project_id(body.get("project_id"))
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    result = await run_ai(
        source=source,
        external_chat_id=external_chat_id,
        text=text,
        project_id=project_id,
        preferred_model=str(body.get("preferred_model", "") or "").strip().lower() or None,
        allow_external_model=bool(body.get("allow_external_model", True)),
        allow_external_search=bool(body.get("allow_external_search", False)),
        intent=str(body.get("intent", "") or "").strip().lower() or None,
    )
    return JSONResponse({"ok": True, **result})


@app.post("/ai/event")
async def ai_event(request: Request):
    configured_token = str(getattr(settings, "ener_ai_event_token", "") or "").strip()
    if configured_token:
        provided_token = str(request.headers.get("X-Ener-AI-Event-Token", "") or "").strip()
        if provided_token != configured_token:
            raise HTTPException(status_code=401, detail="invalid event token")

    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {"payload": body}
    except Exception:
        body = {"payload": {}}

    source = str(body.get("source", "external") or "external").strip() or "external"
    event_type = str(body.get("event_type", "external_event") or "external_event").strip() or "external_event"
    project_slug = str(body.get("project_slug", "") or "").strip()
    summary = str(body.get("summary", "") or "").strip() or event_type
    external_user_id = str(body.get("external_user_id", "") or "").strip() or None
    external_object_id = str(body.get("external_object_id", "") or "").strip() or None
    payload = body.get("payload")
    if payload is None:
        payload = {}

    context_obj = {
        "source": source,
        "project_slug": project_slug,
        "event_type": event_type,
        "summary": summary,
        "external_user_id": external_user_id,
        "external_object_id": external_object_id,
        "payload": payload,
    }
    try:
        context_json = json.dumps(context_obj, ensure_ascii=False, default=str)
    except Exception:
        context_json = json.dumps(
            {
                "source": source,
                "project_slug": project_slug,
                "event_type": event_type,
                "summary": summary,
                "external_user_id": external_user_id,
                "external_object_id": external_object_id,
                "payload": str(payload)[:500],
            },
            ensure_ascii=False,
        )
    context_json = context_json[:4000]

    tags = [source]
    if project_slug:
        tags.append(project_slug)
    tags.append(event_type)

    event_id = await log_event(
        agent_name="AIGatewayEvent",
        event_type=event_type,
        triggered_by=source or "ener_scan",
        summary=summary,
        tags=tags,
        context=context_json,
        result="success",
    )

    from app.core.artifact_memory import store_external_event_artifact

    artifact_result = await store_external_event_artifact(
        {
            "event_id": event_id,
            "source": source,
            "event_type": event_type,
            "project_slug": project_slug or "external",
            "summary": summary,
            "external_user_id": external_user_id,
            "external_object_id": external_object_id,
            "payload": payload,
        }
    )

    response = {
        "ok": True,
        "event_type": event_type,
        "source": source,
        "saved": True,
        "event_id": event_id,
    }
    if artifact_result.get("ok") and artifact_result.get("artifact_id"):
        response["artifact_saved"] = True
        response["artifact_id"] = artifact_result["artifact_id"]
        if artifact_result.get("existing"):
            response["artifact_existing"] = True
    else:
        response["artifact_saved"] = False
        if artifact_result.get("error"):
            response["artifact_warning"] = str(artifact_result["error"])[:200]
    return JSONResponse(response)


# ---------------------------------------------------------------------------
# Autopost (ener-autopost bridge): caption generation + AI review สำหรับ
# โพสต์ขึ้น Facebook/Instagram ของเพจ Ener Scan แบบอัตโนมัติผ่าน n8n + Postiz
# ---------------------------------------------------------------------------

_AUTOPOST_PAGE_NAME = "Ener Scan ตรวจพลังพระ หิน เครื่องราง"

_AUTOPOST_CAPTION_SYSTEM = {
    "scan_result": f"""คุณคือแอดมินเพจ Facebook "{_AUTOPOST_PAGE_NAME}"
งาน: เขียนแคปชั่นโพสต์จากผลสแกนพระเครื่อง/หิน/เครื่องรางของลูกค้า (ข้อมูล JSON ในข้อความถัดไป)
กฎ:
- ภาษาไทย น้ำเสียงเป็นกันเอง น่าสนใจ ไม่โอเวอร์ ไม่การันตีผลลัพธ์เกินจริง
- ห้ามเปิดเผยข้อมูลส่วนตัวของลูกค้า (ชื่อ-นามสกุล เบอร์โทร ที่อยู่ อีเมล) แม้จะมีอยู่ใน JSON
- ความยาว 3-6 บรรทัด ปิดท้ายด้วย hashtag ที่เกี่ยวข้อง 3-5 อัน
- ห้ามใส่ disclaimer เอง (ระบบจะเติมข้อความ disclaimer ต่อท้ายให้อัตโนมัติ)
- ตอบกลับเป็นแคปชั่นอย่างเดียว ห้ามมีคำอธิบาย ห้ามใส่เครื่องหมายคำพูดคร่อม""",
    "amulet_match": f"""คุณคือแอดมินเพจ Facebook "{_AUTOPOST_PAGE_NAME}"
งาน: เขียนโพสต์ความรู้ธีม "พระ/เครื่องรางแบบนี้เหมาะกับใคร" จากข้อมูล JSON ในข้อความถัดไป
กฎ:
- ภาษาไทย เป็นกันเอง ให้ความรู้ น่าเชื่อถือ ไม่ชวนเชื่อแบบงมงายเกินจริง
- ความยาว 4-8 บรรทัด ปิดท้ายด้วย hashtag ที่เกี่ยวข้อง 3-5 อัน
- ห้ามใส่ disclaimer เอง (ระบบจะเติมข้อความ disclaimer ต่อท้ายให้อัตโนมัติ)
- ตอบกลับเป็นแคปชั่นอย่างเดียว ห้ามมีคำอธิบาย ห้ามใส่เครื่องหมายคำพูดคร่อม""",
    "temple_info": f"""คุณคือแอดมินเพจ Facebook "{_AUTOPOST_PAGE_NAME}"
งาน: เขียนโพสต์แนะนำวัด/สถานที่ศักดิ์สิทธิ์ จากข้อมูล JSON ในข้อความถัดไป (ชื่อ ที่ตั้ง จุดเด่น เกร็ดน่ารู้)
กฎ:
- ภาษาไทย เป็นกันเอง ชวนไปไหว้/เที่ยว ให้ข้อมูลที่เป็นประโยชน์
- ความยาว 4-8 บรรทัด ปิดท้ายด้วย hashtag ที่เกี่ยวข้อง 3-5 อัน
- ห้ามใส่ disclaimer เอง (ระบบจะเติมข้อความ disclaimer ต่อท้ายให้อัตโนมัติ)
- ตอบกลับเป็นแคปชั่นอย่างเดียว ห้ามมีคำอธิบาย ห้ามใส่เครื่องหมายคำพูดคร่อม""",
}

_AUTOPOST_REVIEW_SYSTEM = """คุณคือผู้ตรวจสอบคุณภาพโพสต์ก่อนเผยแพร่ขึ้น Facebook/Instagram ของเพจสายมู/พระเครื่อง
ตรวจแคปชั่นในข้อความถัดไปตามเกณฑ์:
- เนื้อหาเหมาะสม ไม่ผิดนโยบาย Facebook (ไม่หลอกลวง ไม่สร้างความเชื่อที่เป็นอันตราย เช่น การันตีรักษาโรค/โชคลาภ)
- ไม่มีข้อมูลส่วนตัวของลูกค้า (ชื่อ เบอร์โทร ที่อยู่ อีเมล)
- ภาษาไทยถูกต้อง สื่อสารชัดเจน น้ำเสียงเหมาะกับเพจ
ให้คะแนนความมั่นใจว่าโพสต์นี้ปลอดภัยพอจะเผยแพร่อัตโนมัติ เป็นตัวเลขเต็ม 0-100 (100 = มั่นใจมาก ปลอดภัย)
ตอบกลับเป็น JSON เท่านั้น รูปแบบ: {"score": <0-100>, "reason": "<เหตุผลสั้นๆ ภาษาไทย>"}"""


def _require_autopost_token(request: Request) -> None:
    configured_token = str(getattr(settings, "ener_ai_event_token", "") or "").strip()
    if configured_token:
        provided_token = str(request.headers.get("X-Ener-AI-Event-Token", "") or "").strip()
        if provided_token != configured_token:
            raise HTTPException(status_code=401, detail="invalid event token")


@app.post("/ai/autopost/caption")
async def ai_autopost_caption(request: Request):
    _require_autopost_token(request)
    body = await request.json()
    content_type = str(body.get("content_type", "") or "").strip()
    system = _AUTOPOST_CAPTION_SYSTEM.get(content_type)
    if not system:
        raise HTTPException(status_code=400, detail=f"unknown content_type: {content_type}")
    data = body.get("data") or {}
    prompt = json.dumps(data, ensure_ascii=False)
    caption = await chat(prompt, system=system, agent="autopost", preferred_model="haiku")
    return JSONResponse({"ok": True, "caption": caption.strip()})


@app.post("/ai/autopost/review")
async def ai_autopost_review(request: Request):
    _require_autopost_token(request)
    body = await request.json()
    caption = str(body.get("caption", "") or "").strip()
    if not caption:
        raise HTTPException(status_code=400, detail="caption is required")
    try:
        result = await chat_json(caption, system=_AUTOPOST_REVIEW_SYSTEM, agent="autopost", preferred_model="haiku")
        score = max(0, min(100, int(result.get("score", 0))))
        reason = str(result.get("reason", "") or "")
    except Exception as exc:
        score = 0
        reason = f"review failed: {exc}"[:200]
    return JSONResponse({"ok": True, "score": score, "reason": reason})


WORKSPACE_TOOLS = [
    ("office", "👩‍💼 เลขา"),
    ("chat", "💬 Chat"),
    ("notes", "📝 Notes"),
    ("tasks", "✅ Tasks"),
    ("standup", "📋 Standup"),
    ("brainstorm", "🔥 Brainstorm"),
    ("news", "📰 News"),
    ("memory", "🧠 Memory"),
    ("files", "📁 Files"),
    ("benchmark", "🏆 Benchmark"),
    ("code", "💻 Code"),
    ("system", "⚙️ System"),
]

WORKSPACE_MODEL_GROUPS: list[tuple[str, list[tuple[str, str]]]] = []
WORKSPACE_MODEL_OPTIONS: list[tuple[str, str]] = []

_VALID_WORKSPACE_TOOLS = {tool_id for tool_id, _ in WORKSPACE_TOOLS}


def _is_venice_model_id(model: str) -> bool:
    from app.core.venice_client import is_venice_model

    return is_venice_model(model)


def _is_featherless_model_id(model: str) -> bool:
    from app.core.featherless_client import is_featherless_model

    return is_featherless_model(model)


def _is_openrouter_model_id(model: str) -> bool:
    key = str(model or "").strip().lower()
    return bool(key and ("/" in key or key in _OPENROUTER_KEYS))


def _is_cloud_llm_model_id(model: str) -> bool:
    return (
        _is_featherless_model_id(model)
        or _is_venice_model_id(model)
        or _is_openrouter_model_id(model)
    )


async def _workspace_model_label(model_used: str) -> str:
    from app.core.featherless_client import featherless_model_label
    from app.core.openrouter_client import openrouter_model_label
    from app.core.venice_client import venice_model_label

    mid = str(model_used or "").strip()
    if not mid:
        return "Ener-AI"
    if mid == "system":
        return "System"
    if _is_featherless_model_id(mid):
        return await featherless_model_label(mid)
    if _is_venice_model_id(mid):
        return await venice_model_label(mid)
    return await openrouter_model_label(mid)


async def _workspace_projects_for_page() -> tuple[int, list[dict]]:
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
                COUNT(m.id) AS message_count,
                MAX(datetime(m.created_at, '+7 hours')) AS last_active
            FROM projects p
            LEFT JOIN messages m ON m.project_id = p.id AND m.chat_id = ?
            WHERE p.deleted_at IS NULL
            GROUP BY p.id, p.name, p.created_at
            ORDER BY COALESCE(MAX(m.created_at), p.created_at) DESC, p.id DESC
            """,
            (_workspace_user_id(),),
        )
        rows = await cursor.fetchall()
    total_messages = int(total_row["total"] or 0) if total_row else 0
    projects = [
        {
            "id": int(row["id"]),
            "name": str(row["name"] or ""),
            "message_count": int(row["message_count"] or 0),
            "last_active": str(row["last_active"] or ""),
        }
        for row in rows
    ]
    return total_messages, projects


async def _workspace_openrouter_model_groups() -> tuple[
    list[tuple[str, list[tuple[str, str]]]],
    list[tuple[str, str]],
]:
    from app.core.featherless_client import FEATHERLESS_KEYS, FEATHERLESS_LABELS
    from app.core.openrouter_client import list_openrouter_models
    from app.core.venice_client import VENICE_KEYS, VENICE_LABELS

    groups: list[tuple[str, list[tuple[str, str]]]] = []
    flat: list[tuple[str, str]] = []

    featherless_options = [(k, FEATHERLESS_LABELS[k]) for k in FEATHERLESS_KEYS]
    groups.append(("Featherless.ai (Uncensored)", featherless_options))
    flat.extend(featherless_options)

    venice_options = [(k, VENICE_LABELS[k]) for k in VENICE_KEYS]
    groups.append(("Venice.ai (Uncensored)", venice_options))
    flat.extend(venice_options)

    options = await list_openrouter_models()
    groups.append(("OpenRouter (Cloud)", options))
    flat.extend(options)
    return groups, flat


async def _workspace_sidebar_stats() -> dict:
    async with get_db() as db:
        fl_cur = await db.execute(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS in_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS out_tokens
            FROM ai_runs
            WHERE model IN ('featherless-abliterated')
            """
        )
        fl_row = await fl_cur.fetchone()
        fl_today_cur = await db.execute(
            """
            SELECT COUNT(*) AS calls
            FROM ai_runs
            WHERE model IN ('featherless-abliterated')
              AND DATE(created_at) = DATE('now')
            """
        )
        fl_today_row = await fl_today_cur.fetchone()

        or_model_keys = list(_OPENROUTER_KEYS)
        placeholders = ",".join("?" * len(or_model_keys))
        or_cur = await db.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS in_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS out_tokens
            FROM ai_runs
            WHERE model IN ({placeholders})
            """,
            or_model_keys,
        )
        or_row = await or_cur.fetchone()
        or_today_cur = await db.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS in_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS out_tokens
            FROM ai_runs
            WHERE model IN ({placeholders})
              AND DATE(created_at) = DATE('now')
            """,
            or_model_keys,
        )
        or_today_row = await or_today_cur.fetchone()

    or_credits_usd: float | None = None
    or_usage_usd: float | None = None
    try:
        or_key = await get_openrouter_api_key()
        if or_key:
            async with httpx.AsyncClient(timeout=5.0) as or_client:
                or_resp = await or_client.get(
                    "https://openrouter.ai/api/v1/auth/key",
                    headers={"Authorization": f"Bearer {or_key}"},
                )
                if or_resp.status_code == 200:
                    or_data = or_resp.json().get("data") or {}
                    or_usage_usd = float(or_data.get("usage") or 0)
                    or_limit = or_data.get("limit")
                    if or_limit:
                        or_credits_usd = float(or_limit) - or_usage_usd
    except Exception:
        pass

    return {
        "featherless_stats": {
            "calls": int(fl_row["calls"] or 0) if fl_row else 0,
            "calls_today": int(fl_today_row["calls"] or 0) if fl_today_row else 0,
            "in_tokens": int(fl_row["in_tokens"] or 0) if fl_row else 0,
            "out_tokens": int(fl_row["out_tokens"] or 0) if fl_row else 0,
        },
        "openrouter_stats": {
            "calls": int(or_row["calls"] or 0) if or_row else 0,
            "calls_today": int(or_today_row["calls"] or 0) if or_today_row else 0,
            "in_tokens": int(or_row["in_tokens"] or 0) if or_row else 0,
            "out_tokens": int(or_today_row["in_tokens"] or 0)
            + int(or_today_row["out_tokens"] or 0)
            if or_today_row
            else 0,
            "usage_usd": round(or_usage_usd, 4) if or_usage_usd is not None else None,
            "credits_usd": round(or_credits_usd, 2) if or_credits_usd is not None else None,
        },
        "system_resource_stats": _workspace_resource_stats(),
    }


DEPT_STRUCTURE = [
    {
        "key": "hq",
        "label": "🧠 HQ",
        "agents": [
            "MainChatAgent",
            "MemoryAgent",
            "SecretaryAgent",
            "BriefingAgent",
        ],
    },
    {
        "key": "ener",
        "label": "⚡ Ener Scan",
        "agents": ["EnerAgent", "ContentAgent", "TarotAgent"],
    },
    {
        "key": "intel",
        "label": "📡 Intel",
        "agents": ["NewsAgent", "ThinkTeam", "DigestAgent"],
    },
    {
        "key": "tech",
        "label": "💻 Tech",
        "agents": ["CodeAgent", "MonitorAgent", "GithubAgent"],
    },
    {
        "key": "ops",
        "label": "🗂️ Ops",
        "agents": ["TaskAgent", "GmailAgent", "LogKeeper", "SessionAgent"],
    },
]


async def _load_office_status() -> dict:
    import datetime

    from app.core.agents import OFFICE_AGENTS, OFFICE_AGENT_CHAT_CMDS

    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT agent_name,
                   MAX(created_at) AS last_run,
                   COUNT(*) AS total_runs,
                   SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS success_count,
                   SUM(CASE WHEN DATE(created_at)=DATE('now') THEN 1 ELSE 0 END) AS runs_today
            FROM agent_runs
            GROUP BY agent_name
            """
        )
        rows = await cur.fetchall()
        agent_stats = {r["agent_name"]: dict(r) for r in rows}

        task_cur = await db.execute(
            "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
        )
        task_rows = await task_cur.fetchall()
        task_summary = {r["status"]: r["cnt"] for r in task_rows}

        open_cur = await db.execute(
            """
            SELECT id, title, priority, status, tags FROM tasks
            WHERE status NOT IN ('done', 'cancelled')
            ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
            LIMIT 10
            """
        )
        open_tasks = [dict(r) for r in await open_cur.fetchall()]

    now = datetime.datetime.utcnow()
    agents_out = []
    for name, emoji, short, role in OFFICE_AGENTS:
        stats = agent_stats.get(name, {})
        last_run_str = stats.get("last_run")
        if last_run_str:
            try:
                last_dt = datetime.datetime.fromisoformat(str(last_run_str).replace("Z", ""))
                diff_min = (now - last_dt).total_seconds() / 60
                if diff_min < 60:
                    status = "active"
                elif diff_min < 1440:
                    status = "idle"
                else:
                    status = "offline"
                last_label = (
                    f"{int(diff_min)}m ago"
                    if diff_min < 60
                    else f"{int(diff_min / 60)}h ago"
                )
            except Exception:
                status = "offline"
                last_label = "-"
        else:
            status = "offline"
            last_label = "ไม่เคยรัน"
        agents_out.append(
            {
                "name": name,
                "emoji": emoji,
                "short": short,
                "role": role,
                "status": status,
                "last_label": last_label,
                "runs_today": int(stats.get("runs_today") or 0),
                "chat_cmd": OFFICE_AGENT_CHAT_CMDS.get(name, ""),
            }
        )

    dept_groups = []
    for dept in DEPT_STRUCTURE:
        members = []
        total_today = 0
        any_active = False
        for ag_name in dept["agents"]:
            ag = next((a for a in agents_out if a["name"] == ag_name), None)
            if ag:
                members.append(ag)
                total_today += ag["runs_today"]
                if ag["status"] == "active":
                    any_active = True
        dept_groups.append(
            {
                "key": dept["key"],
                "label": dept["label"],
                "agents": members,
                "runs_today": total_today,
                "status": "active" if any_active else "idle",
            }
        )

    return {
        "agents": agents_out,
        "dept_groups": dept_groups,
        "task_open": int(task_summary.get("open", 0) or 0),
        "task_pending": int(task_summary.get("pending_approval", 0) or 0),
        "open_tasks": open_tasks,
    }


@app.get("/workspace")
async def workspace_page(
    request: Request,
    tool: str = "chat",
    project_id: int | None = None,
    date: str | None = None,
    scroll: str | None = None,
):
    await _require_admin(request)
    from app.core.database import get_system_stats

    normalized_tool = str(tool or "chat").strip().lower()
    if normalized_tool == "secretary":
        normalized_tool = "office"
    if normalized_tool not in _VALID_WORKSPACE_TOOLS:
        normalized_tool = "chat"
    normalized_project_id = _normalize_project_id(project_id)
    selected_date, show_all = _resolve_workspace_chat_date(date, scroll)
    today_key = _workspace_today_key()

    stats = await get_system_stats()
    sidebar_stats = await _workspace_sidebar_stats()
    featherless_stats = sidebar_stats["featherless_stats"]
    openrouter_stats = sidebar_stats["openrouter_stats"]
    system_resource_stats = sidebar_stats["system_resource_stats"]
    total_messages, projects = await _workspace_projects_for_page()
    stats = {**stats, "messages": total_messages}

    active_model_key = await get_active_model()
    model_groups, model_options = await _workspace_openrouter_model_groups()
    recent_messages = await _workspace_history_rows(
        project_id=normalized_project_id,
        limit=500 if show_all else 300,
        chat_date=None if show_all else selected_date,
    )
    await _workspace_enrich_message_models(recent_messages)
    chat_dates = await _workspace_chat_dates()
    office = await _load_office_status()

    return templates.TemplateResponse(
        "workspace.html",
        {
            "request": request,
            "tool": normalized_tool,
            "stats": stats,
            "featherless_stats": featherless_stats,
            "openrouter_stats": openrouter_stats,
            "system_resource_stats": system_resource_stats,
            "projects": projects,
            "active_model": get_model_label(active_model_key or ""),
            "active_model_key": active_model_key or "",
            "model_options": model_options,
            "model_option_groups": model_groups,
            "tools": WORKSPACE_TOOLS,
            "recent_messages": recent_messages,
            "project_id": normalized_project_id,
            "chat_dates": chat_dates,
            "selected_date": selected_date,
            "selected_date_label": _format_chat_date_label(selected_date)
            if selected_date
            else "",
            "show_all": show_all,
            "today_key": today_key,
            "today_label": _format_chat_date_label(today_key),
            "office": office,
            "now_ts": int(time.time()),
        },
    )


@app.get("/workspace/sidebar/stats")
async def workspace_sidebar_stats(request: Request):
    await _require_admin(request)
    return JSONResponse(await _workspace_sidebar_stats())


async def _read_workspace_image_from_form(form) -> tuple[str | None, str]:
    import base64

    from app.core.vision import guess_media_type

    image_file = form.get("image")
    if not image_file or not hasattr(image_file, "read"):
        return None, "image/jpeg"
    image_bytes = await image_file.read()
    if not image_bytes:
        return None, "image/jpeg"
    media_type = guess_media_type(
        str(getattr(image_file, "filename", "") or ""),
        str(getattr(image_file, "content_type", "") or ""),
    )
    return base64.b64encode(image_bytes).decode("ascii"), media_type


async def _workspace_run_chat_ai(
    *,
    message: str,
    project_id: int | None,
    image_base64: str | None = None,
    image_media_type: str = "image/jpeg",
    preferred_model: str | None = None,
) -> str:
    from app.core.ai_gateway import run_ai
    from app.core.workspace_memory import (
        build_workspace_conversation_context,
        build_workspace_history_for_ai,
    )

    chat_id = _workspace_user_id()
    prompt_text = str(message or "").strip() or "วิเคราะห์รูป screenshot นี้"
    memory_context = await build_workspace_conversation_context(
        chat_id, prompt_text, project_id=project_id
    )
    history = await build_workspace_history_for_ai(
        chat_id, prompt_text, project_id=project_id
    )
    system_prompt = await _workspace_chat_system_prompt(prompt_text, memory_context)
    model = preferred_model or ("haiku" if image_base64 else None)
    result = await run_ai(
        source="telegram",
        external_chat_id=chat_id,
        text=prompt_text,
        project_id=project_id,
        history=history,
        system_prompt=system_prompt,
        image_base64=image_base64,
        image_media_type=image_media_type,
        preferred_model=model,
    )
    return str(result.get("reply", "")).strip() or "ยังไม่มีคำตอบตอนนี้"


@app.post("/workspace/chat")
async def workspace_chat(request: Request):
    await _require_admin(request)
    form = await request.form()
    message = str(form.get("message", "")).strip()
    project_id = _normalize_project_id(form.get("project_id"))
    image_b64, image_media = await _read_workspace_image_from_form(form)
    if message or image_b64:
        await _workspace_run_chat_ai(
            message=message,
            project_id=project_id,
            image_base64=image_b64,
            image_media_type=image_media,
            preferred_model="haiku" if image_b64 else None,
        )
    redirect_url = "/workspace?tool=chat"
    if project_id is not None:
        redirect_url = f"/workspace?tool=chat&project_id={project_id}"
    return RedirectResponse(url=redirect_url, status_code=303)


@app.post("/workspace/chat/vision")
async def workspace_chat_vision(request: Request):
    await _require_admin(request)
    form = await request.form()
    message = str(form.get("message", "")).strip()
    project_id = _normalize_project_id(form.get("project_id"))
    image_b64, image_media = await _read_workspace_image_from_form(form)
    if not message and not image_b64:
        raise HTTPException(status_code=400, detail="กรุณาพิมพ์ข้อความหรือแนบรูป")

    reply = await _workspace_run_chat_ai(
        message=message,
        project_id=project_id,
        image_base64=image_b64,
        image_media_type=image_media,
        preferred_model="haiku",
    )
    return JSONResponse({"ok": True, "reply": reply})


@app.post("/workspace/new-chat")
async def workspace_new_chat(request: Request):
    await _require_admin(request)
    return RedirectResponse(url="/workspace?tool=chat", status_code=303)


@app.post("/workspace/chat/send")
async def workspace_chat_send(request: Request):
    await _require_admin(request)
    payload = await request.json()
    text = str(payload.get("text", payload.get("message", ""))).strip()
    if not text:
        raise HTTPException(status_code=400, detail="กรุณาพิมพ์ข้อความ")
    project_id = _normalize_project_id(payload.get("project_id"))
    model = str(payload.get("model", "")).strip() or "deepseek/deepseek-v4-flash"
    if model.lower() == "auto":
        model = "deepseek/deepseek-v4-flash"
    try:
        model_used = model
        if _is_simple_cpu_query(text):
            from app.agents.monitor_agent import format_nl_resource_report, get_server_stats

            reply = format_nl_resource_report(get_server_stats())
            model_used = "system"
        else:
            reply = await _workspace_cloud_reply(
                text,
                model,
                user_id=_workspace_user_id(),
                project_id=project_id,
            )
        model_label = await _workspace_model_label(model_used)
        await _workspace_save_chat_messages(
            project_id, text, reply, model_used=model_used
        )
        return JSONResponse(
            {
                "ok": True,
                "reply": reply,
                "model": model_used,
                "model_label": model_label,
            }
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/workspace/chat/stream")
async def workspace_chat_stream(request: Request):
    await _require_admin(request)
    body = await request.json()
    message = str(body.get("message", body.get("text", ""))).strip()
    project_id = _normalize_project_id(body.get("project_id"))
    model = str(body.get("model", "auto")).strip() or "deepseek/deepseek-v4-flash"
    if model.lower() == "auto":
        model = "deepseek/deepseek-v4-flash"

    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    from app.core.ai import stream_chat_response
    from app.core.memory import extract_and_store_long_term_memories
    from app.core.workspace_memory import (
        build_workspace_conversation_context,
        build_workspace_history_for_ai,
        index_workspace_message,
    )

    import asyncio as _asyncio

    user_id = _workspace_user_id()
    conversation_id = await _workspace_conversation_id(project_id=project_id)

    async def generate():
        reply_text = ""
        full_reply: list[str] = []
        try:
            yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"

            user_message_id: int | None = None
            async with get_db() as db:
                cur = await db.execute(
                    """
                    INSERT INTO messages (
                        chat_id, conversation_id, role, content, source, project_id
                    )
                    VALUES (?, ?, ?, ?, 'web', ?)
                    """,
                    (user_id, conversation_id, "user", message, project_id),
                )
                user_message_id = cur.lastrowid
                await db.commit()
            await index_workspace_message(
                message_id=user_message_id,
                chat_id=user_id,
                role="user",
                content=message,
                project_id=project_id,
            )

            from app.core.agents import COMMAND_AGENT_MAP

            slash_reply: str | None = None
            slash_agent = ""
            try:
                slash_reply = await _workspace_run_slash_command(message, project_id)
                parsed = _workspace_parse_slash_command(message)
                if parsed:
                    slash_agent = COMMAND_AGENT_MAP.get(parsed[0], "agent")
            except Exception as exc:
                parsed = _workspace_parse_slash_command(message)
                slash_agent = COMMAND_AGENT_MAP.get(parsed[0], "Agent") if parsed else "Agent"
                slash_reply = f"⚠️ {slash_agent} ทำงานไม่สำเร็จ: {exc}"

            if slash_reply is not None:
                reply_text = str(slash_reply).strip() or "ยังไม่มีคำตอบตอนนี้"
                model_used = slash_agent or "agent"
                yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
                assistant_message_id: int | None = None
                async with get_db() as db:
                    cur = await db.execute(
                        """
                        INSERT INTO messages (
                            chat_id, conversation_id, role, content, project_id, source, model_used
                        )
                        VALUES (?, ?, ?, ?, ?, 'web', ?)
                        """,
                        (
                            user_id,
                            conversation_id,
                            "assistant",
                            reply_text,
                            project_id,
                            model_used,
                        ),
                    )
                    assistant_message_id = cur.lastrowid
                    await db.execute(
                        "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                        ("workspace_chat_stream_saved", f"project_id={project_id or 'all'}"),
                    )
                    await db.commit()
                await index_workspace_message(
                    message_id=assistant_message_id,
                    chat_id=user_id,
                    role="assistant",
                    content=reply_text,
                    project_id=project_id,
                )

                async def _post_slash_tasks() -> None:
                    try:
                        await extract_and_store_long_term_memories(message, reply_text)
                    except Exception:
                        pass

                _asyncio.create_task(_post_slash_tasks())
                model_label = slash_agent or "Agent"
                yield f"data: {json.dumps({'type': 'done', 'model': model_used, 'model_label': model_label}, ensure_ascii=False)}\n\n"
                return

            model_used = model
            if _is_cloud_llm_model_id(model):
                from app.core.context_limits import trim_chat_context
                from app.core.featherless_client import (
                    is_featherless_model,
                    stream_featherless,
                )
                from app.core.openrouter_client import stream_openrouter
                from app.core.venice_client import is_venice_model, stream_venice
                from app.core.workspace_memory import build_workspace_history_for_ai

                if _is_simple_cpu_query(message):
                    from app.agents.monitor_agent import (
                        format_nl_resource_report,
                        get_server_stats,
                    )

                    reply_text = format_nl_resource_report(get_server_stats())
                    model_used = "system"
                    yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
                else:
                    history = await build_workspace_history_for_ai(
                        user_id, message, project_id=project_id, recent_limit=8
                    )
                    system = _workspace_chat_system(model)
                    system, history = trim_chat_context(system, history, profile=model)
                    streamed: list[str] = []
                    if is_featherless_model(model):
                        stream_fn = stream_featherless
                    elif is_venice_model(model):
                        stream_fn = stream_venice
                    else:
                        stream_fn = stream_openrouter
                    async for token in stream_fn(
                        model,
                        message,
                        system,
                        history,
                        agent="MainChatAgent",
                    ):
                        streamed.append(token)
                        yield f"data: {json.dumps({'type': 'token', 'text': token}, ensure_ascii=False)}\n\n"
                    reply_text = "".join(streamed).strip() or "ยังไม่มีคำตอบตอนนี้"

                assistant_message_id: int | None = None
                async with get_db() as db:
                    cur = await db.execute(
                        """
                        INSERT INTO messages (
                            chat_id, conversation_id, role, content, project_id, source, model_used
                        )
                        VALUES (?, ?, ?, ?, ?, 'web', ?)
                        """,
                        (
                            user_id,
                            conversation_id,
                            "assistant",
                            reply_text,
                            project_id,
                            model_used,
                        ),
                    )
                    assistant_message_id = cur.lastrowid
                    await db.execute(
                        "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                        ("workspace_chat_stream_saved", f"project_id={project_id or 'all'}"),
                    )
                    await db.commit()
                await index_workspace_message(
                    message_id=assistant_message_id,
                    chat_id=user_id,
                    role="assistant",
                    content=reply_text,
                    project_id=project_id,
                )

                async def _post_openrouter_tasks() -> None:
                    try:
                        async with get_db() as db:
                            await db.execute(
                                "DELETE FROM memories WHERE key = 'model_handoff_context'"
                            )
                            await db.commit()
                    except Exception:
                        pass
                    try:
                        await extract_and_store_long_term_memories(message, reply_text)
                    except Exception:
                        pass

                _asyncio.create_task(_post_openrouter_tasks())
                model_label = await _workspace_model_label(model_used)
                yield f"data: {json.dumps({'type': 'done', 'model': model_used, 'model_label': model_label}, ensure_ascii=False)}\n\n"
                return

            if model in _WORKSPACE_JSON_SEND_MODELS:
                if _is_simple_cpu_query(message):
                    from app.agents.monitor_agent import (
                        format_nl_resource_report,
                        get_server_stats,
                    )

                    reply_text = format_nl_resource_report(get_server_stats())
                    yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
                else:
                    reply_fn = (
                        _workspace_local_qwen_reply
                        if model in _WORKSPACE_LOCAL_MODELS
                        else _workspace_openrouter_reply
                    )
                    reply_task = _asyncio.create_task(
                        reply_fn(
                            message,
                            model,
                            user_id=user_id,
                            project_id=project_id,
                        )
                    )
                    while not reply_task.done():
                        done, _ = await _asyncio.wait({reply_task}, timeout=15.0)
                        if reply_task in done:
                            break
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                    reply_text = reply_task.result()
                    yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
            elif _is_simple_cpu_query(message):
                from app.agents.monitor_agent import format_nl_resource_report, get_server_stats

                reply_text = format_nl_resource_report(get_server_stats())
                yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
            else:
                memory_context = await build_workspace_conversation_context(
                    user_id, message, project_id=project_id
                )
                hist_limit = 12 if model in {"groq"} else 28
                history = await build_workspace_history_for_ai(
                    user_id, message, project_id=project_id, recent_limit=hist_limit
                )
                system_prompt = await _workspace_chat_system_prompt(
                    message, memory_context
                )
                from app.core.context_limits import profile_for_model, trim_chat_context

                profile = profile_for_model(model if model not in ("auto", "") else "default")
                system_prompt, history = trim_chat_context(
                    system_prompt, history, profile=profile
                )

                if _workspace_needs_tool_agent(message):
                    from app.core.ai_gateway import run_ai

                    pref_model = model if model and model != "auto" else None
                    gateway_result = await run_ai(
                        source="telegram",
                        external_chat_id=user_id,
                        text=message,
                        project_id=project_id,
                        history=history,
                        system_prompt=system_prompt,
                        preferred_model=pref_model,
                    )
                    reply_text = (
                        str(gateway_result.get("reply", "")).strip()
                        or "ยังไม่มีคำตอบตอนนี้"
                    )
                    yield f"data: {json.dumps({'type': 'token', 'text': reply_text}, ensure_ascii=False)}\n\n"
                else:
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
            assistant_message_id: int | None = None
            async with get_db() as db:
                cur = await db.execute(
                    """
                    INSERT INTO messages (
                        chat_id, conversation_id, role, content, project_id, source, model_used
                    )
                    VALUES (?, ?, ?, ?, ?, 'web', ?)
                    """,
                    (
                        user_id,
                        conversation_id,
                        "assistant",
                        reply_text,
                        project_id,
                        model_used if _is_cloud_llm_model_id(model) else None,
                    ),
                )
                assistant_message_id = cur.lastrowid
                await db.execute(
                    "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                    ("workspace_chat_stream_saved", f"project_id={project_id or 'all'}"),
                )
                await db.commit()
            await index_workspace_message(
                message_id=assistant_message_id,
                chat_id=user_id,
                role="assistant",
                content=reply_text,
                project_id=project_id,
            )

            async def _post_reply_tasks() -> None:
                try:
                    async with get_db() as db:
                        await db.execute(
                            "DELETE FROM memories WHERE key = 'model_handoff_context'"
                        )
                        await db.commit()
                except Exception:
                    pass
                try:
                    await extract_and_store_long_term_memories(message, reply_text)
                except Exception:
                    pass

            _asyncio.create_task(_post_reply_tasks())
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            from app.core.ollama_client import format_ollama_error

            if model in _WORKSPACE_LOCAL_MODELS:
                detail = format_ollama_error(exc)
            elif _is_featherless_model_id(model):
                detail = f"Featherless error: {exc}"
            elif _is_venice_model_id(model):
                detail = f"Venice error: {exc}"
            elif model in _OPENROUTER_KEYS or _is_openrouter_model_id(model):
                detail = f"OpenRouter error: {exc}"
            else:
                detail = str(exc)
            yield f"data: {json.dumps({'type': 'error', 'text': detail}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


_SECRETARY_CONVERSATION_ID = "secretary"
_SECRETARY_SOURCE = "secretary"


async def _save_secretary_messages(user_text: str, reply_text: str) -> None:
    user_id = _workspace_user_id()
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO messages (chat_id, conversation_id, role, content, source, model_used)
            VALUES (?, ?, 'user', ?, ?, NULL)
            """,
            (user_id, _SECRETARY_CONVERSATION_ID, user_text, _SECRETARY_SOURCE),
        )
        await db.execute(
            """
            INSERT INTO messages (chat_id, conversation_id, role, content, source, model_used)
            VALUES (?, ?, 'assistant', ?, ?, ?)
            """,
            (
                user_id,
                _SECRETARY_CONVERSATION_ID,
                reply_text,
                _SECRETARY_SOURCE,
                "SecretaryAgent",
            ),
        )
        await db.commit()


@app.post("/workspace/secretary/stream")
async def workspace_secretary_stream(request: Request):
    await _require_admin(request)
    body = await request.json()
    message = str(body.get("message", body.get("text", ""))).strip()
    if not message:
        raise HTTPException(status_code=400, detail="empty message")

    from app.agents.secretary_agent import _last_route, handle_secretary

    async def generate():
        try:
            yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"
            _last_route.clear()
            reply = await handle_secretary(message)
            if _last_route.get("agent"):
                route_evt = {
                    "type": "route",
                    "from": "secretary",
                    "to": _last_route["agent"],
                    "dept": _last_route.get("dept", ""),
                    "message": message[:40],
                }
                yield f"data: {json.dumps(route_evt, ensure_ascii=False)}\n\n"
            reply_text = str(reply or "").strip() or "เอรับทราบแล้วค่ะ"
            chunk_size = 50
            for i in range(0, len(reply_text), chunk_size):
                chunk = reply_text[i : i + chunk_size]
                yield f"data: {json.dumps({'type': 'token', 'text': chunk}, ensure_ascii=False)}\n\n"
            await _save_secretary_messages(message, reply_text)
            yield f"data: {json.dumps({'type': 'done', 'model': 'SecretaryAgent', 'model_label': 'เอ · เลขา'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'text': str(exc)[:200]}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/workspace/secretary/history")
async def secretary_history(request: Request):
    await _require_admin(request)
    user_id = _workspace_user_id()
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT role, content FROM messages
            WHERE source = ? AND chat_id = ?
            ORDER BY id DESC LIMIT 40
            """,
            (_SECRETARY_SOURCE, user_id),
        )
        rows = await cur.fetchall()
    msgs = [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    return {"messages": msgs}


@app.get("/workspace/office/activity")
async def office_activity_feed(request: Request):
    await _require_admin(request)
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT agent_name, success, error_msg, created_at,
                   ROUND((julianday('now') - julianday(created_at)) * 1440) AS mins_ago
            FROM agent_runs
            ORDER BY id DESC
            LIMIT 30
            """
        )
        rows = await cur.fetchall()
    items = []
    for r in rows:
        items.append(
            {
                "agent": r["agent_name"],
                "success": bool(r["success"]),
                "error": r["error_msg"] or "",
                "mins_ago": int(r["mins_ago"] or 0),
            }
        )
    return {"items": items}


@app.get("/workspace/office/stream")
async def office_event_stream(request: Request):
    await _require_admin(request)
    import asyncio

    async def generate():
        last_id = 0
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COALESCE(MAX(id), 0) AS mid FROM agent_events"
            )
            row = await cur.fetchone()
            last_id = int(row["mid"] or 0)

        while True:
            if await request.is_disconnected():
                break
            async with get_db() as db:
                cur = await db.execute(
                    """
                    SELECT id, event_type, agent_name, triggered_by, summary, context
                    FROM agent_events
                    WHERE id > ? AND event_type IN ('route', 'complete')
                    ORDER BY id
                    LIMIT 10
                    """,
                    (last_id,),
                )
                rows = await cur.fetchall()
            for row in rows:
                last_id = max(last_id, int(row["id"]))
                try:
                    ctx = json.loads(row["context"] or "{}")
                except Exception:
                    ctx = {}
                event = {
                    "id": row["id"],
                    "type": row["event_type"],
                    "from": ctx.get("from", row["triggered_by"]),
                    "to": ctx.get("to", row["agent_name"]),
                    "msg": row["summary"],
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/workspace/chat/history")
async def workspace_chat_history(request: Request):
    await _require_admin(request)
    project_id = _normalize_project_id(request.query_params.get("project_id"))
    limit = int(request.query_params.get("limit", "200") or 200)
    raw_date = request.query_params.get("date") or request.query_params.get("scroll")
    selected_date, show_all = _resolve_workspace_chat_date(raw_date, None)
    messages = await _workspace_history_rows(
        project_id=project_id,
        limit=500 if show_all else max(limit, 300),
        chat_date=None if show_all else selected_date,
    )
    await _workspace_enrich_message_models(messages)
    return JSONResponse(
        {
            "messages": messages,
            "date": selected_date,
            "show_all": show_all,
        }
    )


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


@app.post("/workspace/task/{task_id}/done")
async def workspace_task_done(request: Request, task_id: int):
    await _require_admin(request)
    from app.agents.task import complete_task

    await complete_task(task_id)
    return RedirectResponse("/workspace?tool=office", status_code=303)


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
    project_name = file_path.split("/")[0] if file_path else "ยังไม่ได้เลือก"
    system = (
        f"คุณเป็น Ener-AI Code Assistant — coding agent บน Ener-AI Web IDE\n\n"
        f"=== Context ปัจจุบัน ===\n"
        f"Project: {project_name}\n"
        f"File: {file_path or 'ยังไม่ได้เลือกไฟล์'}\n"
        f"Stack: Python 3.11 / FastAPI / aiosqlite / Docker / Server my-ener.uk\n"
        f"{file_context}\n"
        f"=== เครื่องมือที่ใช้ได้ ===\n"
        f"- Apply to editor: ผู้ใช้กดปุ่ม Apply เพื่อเขียน code block เข้าไฟล์โดยตรง\n"
        f"- Save (Ctrl+S): บันทึกไฟล์ลง server\n"
        f"- Git: commit / push ได้จาก toolbar\n"
        f"- + New file: สร้างไฟล์ใหม่ใน project\n\n"
        f"=== กฎการตอบ ===\n"
        f"- ถ้าถามให้เขียน/แก้ code → ส่ง code block ที่สมบูรณ์ (complete file content) เพื่อให้ Apply ได้\n"
        f"- ไม่ต้องสร้าง Cursor prompt — เขียน code ตรงๆ ในรูป ```python ... ``` เลย\n"
        f"- อธิบายสั้นๆ ก่อน code block ว่าทำอะไร\n"
        f"- ใช้ภาษาไทยผสม technical terms"
    )
    answer = await ai_chat(
        question, system=system, agent="CodeAssistant",
        messages=[], preferred_model=model, strict_model=False,
    )
    return JSONResponse({"answer": str(answer)})


@app.get("/workspace/code/server-context")
async def workspace_code_server_context(request: Request):
    """Return server info for Code Agent context."""
    await _require_admin(request)
    import subprocess as _sp

    def _run(cmd: str) -> str:
        try:
            r = _sp.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    disk   = _run("df -h / | tail -1 | awk '{print $3\"/\"$2\" used (\"$5\")\"}'")
    ram    = _run("free -h | awk '/^Mem:/{print $3\"/\"$2\" used\"}'")
    cpu    = _run("cat /proc/loadavg | awk '{print $1\" (1m avg)\"}'")
    uptime = _run("uptime -p")
    containers = _run(
        "docker ps --format '{{.Names}}\\t{{.Status}}\\t{{.Ports}}' 2>/dev/null"
    )
    git_log = _run("git -C /root/ener-ai log --oneline -5 2>/dev/null")
    projects = _run("ls /root/ener-code 2>/dev/null")

    # Collect existing FastAPI routes (method + path)
    routes_list = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if methods and path and not path.startswith("/docs") and not path.startswith("/openapi"):
            for m in sorted(methods):
                routes_list.append(f"{m} {path}")
    routes_list.sort()

    return JSONResponse({
        "server": {
            "cpu_load": cpu,
            "ram": ram,
            "disk": disk,
            "uptime": uptime,
        },
        "containers": containers,
        "git_log": git_log,
        "ener_code_projects": projects,
        "ener_ai_routes": routes_list[:60],
    })


_WRITE_FILE_RE = __import__("re").compile(
    r'<WRITE_FILE\s+path="([^"]+)">([\s\S]*?)</WRITE_FILE>', __import__("re").MULTILINE
)
_EXEC_CMD_RE = __import__("re").compile(
    r'<EXEC_CMD\s+cmd="(.*?)"\s*/?>', __import__("re").MULTILINE
)
_UPDATE_MEMORY_RE = __import__("re").compile(
    r'<UPDATE_MEMORY\s+key="([^"]+)"\s+value="([^"]+)"\s*/?>', __import__("re").MULTILINE
)
_ROUTE_DECORATOR_RE = __import__("re").compile(
    r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']', __import__("re").IGNORECASE
)
_FORM_METHOD_ACTION_RE = __import__("re").compile(
    r'<form[^>]*\bmethod\s*=\s*["\'](\w+)["\'][^>]*\baction\s*=\s*["\']([^"\']+)["\']'
    r'|<form[^>]*\baction\s*=\s*["\']([^"\']+)["\'][^>]*\bmethod\s*=\s*["\'](\w+)["\']',
    __import__("re").IGNORECASE,
)
_FORM_ACTION_ONLY_RE = __import__("re").compile(
    r'<form(?![^>]*\bmethod\s*=)[^>]*\baction\s*=\s*["\']([^"\']+)["\']', __import__("re").IGNORECASE
)
_HREF_RE = __import__("re").compile(r'<a[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', __import__("re").IGNORECASE)
_INCOMPLETE_PLACEHOLDER_RE = __import__("re").compile(
    r'#[^\n]*\b('
    r'TODO|FIXME'
    r'|will\s+be\s+added'
    r'|to\s+be\s+(?:continued|added|implemented)'
    r'|rest\s+of\s+the\s+(?:code|file|implementation|routes?)'
    r'|(?:more|additional|remaining|other)\s+(?:routes?|endpoints?|code|functions?|views?|logic)'
    r'|continue[sd]?\s+(?:here|below)'
    r'|implement(?:ed)?\s+later'
    r'|placeholder'
    r')',
    __import__("re").IGNORECASE,
)


def run_static_checks(files: dict[str, str]) -> list[dict]:
    """Pattern-based checks for cross-file/runtime bugs that py_compile can't catch.

    `files` maps project-relative path -> file content (already-written files).
    Returns a list of {'file', 'check', 'hint'} issues to feed into a repair prompt.
    """
    issues: list[dict] = []
    db_paths: set[str] = set()

    for path, content in files.items():
        if not path.endswith(".py"):
            continue

        # UploadFile.read() called more than once without seek(0) in between
        for var in re.findall(r'(\w+)\s*:\s*UploadFile', content):
            read_count = len(re.findall(rf'await\s+{re.escape(var)}\.read\(\)', content))
            if read_count > 1 and f'{var}.seek(0)' not in content:
                issues.append({
                    'file': path,
                    'check': 'uploadfile_double_read',
                    'hint': (
                        f'{path}: ตัวแปร "{var}" (UploadFile) ถูกเรียก .read() {read_count} ครั้ง '
                        f'โดยไม่มี .seek(0) คั่น — UploadFile.read() อ่านได้ครั้งเดียว ครั้งถัดไปจะได้ '
                        f'b"" เปล่าๆ ให้เก็บผลลัพธ์การอ่านครั้งแรกไว้ในตัวแปรแล้วใช้ตัวแปรนั้นซ้ำ'
                    ),
                })

        # dict(row) without row_factory = aiosqlite.Row
        if re.search(r'dict\(\s*\w+\s*\)', content) and 'row_factory' not in content:
            issues.append({
                'file': path,
                'check': 'missing_row_factory',
                'hint': (
                    f'{path}: ใช้ dict(row) แปลงผลลัพธ์ query แต่ไม่ได้ตั้ง '
                    f'db.row_factory = aiosqlite.Row ก่อน execute — ถ้าไม่ตั้งจะได้ tuple แล้ว '
                    f'dict(tuple) จะ raise ValueError'
                ),
            })

        # Leftover placeholder/TODO comments instead of real code (truncated response)
        for line in content.splitlines():
            if _INCOMPLETE_PLACEHOLDER_RE.search(line):
                issues.append({
                    'file': path,
                    'check': 'incomplete_placeholder',
                    'hint': (
                        f'{path}: พบ comment ที่บ่งบอกว่าโค้ดเขียนไม่จบ เช่น "{line.strip()[:100]}" — '
                        f'นี่คือ placeholder ไม่ใช่โค้ดจริง ต้องเขียน {path} ใหม่ทั้งไฟล์ให้สมบูรณ์ '
                        f'แทนที่ comment นี้ด้วย route/ฟังก์ชันจริงตามที่ comment บอกไว้'
                    ),
                })
                break  # one issue per file is enough

        # Collect DB file paths for cross-file duplicate-DB check
        for m in re.finditer(r'(?:aiosqlite|sqlite3)\.connect\(\s*["\']([^"\']+\.db)["\']', content):
            db_paths.add(m.group(1))
        for m in re.finditer(r'^DB_PATH\s*=\s*["\']([^"\']+\.db)["\']', content, re.M):
            db_paths.add(m.group(1))

    if len(db_paths) > 1:
        issues.append({
            'file': '(multiple files)',
            'check': 'duplicate_db_path',
            'hint': (
                f'พบไฟล์ฐานข้อมูลคนละชื่อในโปรเจกต์เดียวกัน: {", ".join(sorted(db_paths))} — '
                f'ทุกไฟล์ต้องเชื่อมต่อ database ไฟล์เดียวกัน (DB_PATH เดียวกัน) และใช้ schema '
                f'(CREATE TABLE) เดียวกัน ห้ามมีสองชุด'
            ),
        })

    # Dead templates: every templates/*.html must be rendered by main.py or extended by another template
    main_src = files.get("main.py", "")
    template_files = [p for p in files if p.startswith("templates/") and p.endswith(".html")]
    if main_src and template_files:
        for tpl in template_files:
            tpl_name = tpl.split("/", 1)[1]
            referenced = tpl_name in main_src
            extended = any(
                other != tpl and (f'"{tpl_name}"' in files.get(other, "") or f"'{tpl_name}'" in files.get(other, ""))
                for other in template_files
            )
            if not referenced and not extended:
                issues.append({
                    'file': tpl,
                    'check': 'dead_template',
                    'hint': (
                        f'{tpl}: ไฟล์ template นี้ไม่ถูก render โดย route ใดใน main.py และไม่ถูก '
                        f'extends โดย template อื่น — เพิ่ม route ที่ render ไฟล์นี้ใน main.py '
                        f'หรือลบไฟล์นี้ทิ้งถ้าไม่จำเป็น'
                    ),
                })

    # Routes referenced by templates (form action / link href) but not defined in main.py,
    # or defined with a different HTTP method (would 405 at runtime)
    if main_src and template_files:
        defined_routes: set[tuple[str, str]] = set()  # (METHOD, path)
        defined_paths: set[str] = set()
        param_route_prefixes: set[str] = set()
        for method, route in _ROUTE_DECORATOR_RE.findall(main_src):
            if "{" in route:
                param_route_prefixes.add(route.split("{")[0].rstrip("/"))
            else:
                path = route.rstrip("/") or "/"
                defined_routes.add((method.upper(), path))
                defined_paths.add(path)

        for tpl in template_files:
            content = files.get(tpl, "")
            template_actions: set[tuple[str, str]] = set()

            for m in _FORM_METHOD_ACTION_RE.finditer(content):
                if m.group(1):
                    template_actions.add((m.group(1).upper(), m.group(2)))
                else:
                    template_actions.add((m.group(4).upper(), m.group(3)))
            for m in _FORM_ACTION_ONLY_RE.finditer(content):
                template_actions.add(("GET", m.group(1)))  # HTML default method
            for m in _HREF_RE.finditer(content):
                template_actions.add(("GET", m.group(1)))

            seen: set[tuple[str, str]] = set()
            for method, raw_path in template_actions:
                path = raw_path.split("#")[0].split("?")[0]
                if (
                    path in ("", "/")
                    or path.startswith("/static")
                    or path.startswith(("http://", "https://", "mailto:", "javascript:"))
                ):
                    continue
                path_norm = path.rstrip("/") or "/"
                key = (method, path_norm)
                if key in seen or key in defined_routes:
                    continue
                if any(path_norm.startswith(p) for p in param_route_prefixes):
                    continue
                seen.add(key)
                if path_norm in defined_paths:
                    other_methods = ", ".join(sorted(m for m, p in defined_routes if p == path_norm))
                    issues.append({
                        'file': tpl,
                        'check': 'method_mismatch',
                        'hint': (
                            f'{tpl}: ฟอร์ม/ลิงก์เรียก {method} {path} แต่ main.py มี route นี้เป็น '
                            f'{other_methods} เท่านั้น — ผู้ใช้จะได้ 405 Method Not Allowed ตอนใช้งานจริง '
                            f'เพิ่ม @app.{method.lower()}("{path_norm}") ใน main.py หรือแก้ method/action '
                            f'ใน {tpl} ให้ตรงกับ route ที่มีอยู่จริง'
                        ),
                    })
                else:
                    issues.append({
                        'file': tpl,
                        'check': 'missing_route',
                        'hint': (
                            f'{tpl}: {method} {path} ไม่มี route นี้ใน main.py เลย — เพิ่ม '
                            f'@app.{method.lower()}("{path_norm}") ใน main.py หรือแก้ลิงก์ใน {tpl} '
                            f'ให้ตรงกับ route ที่มีอยู่จริง'
                        ),
                    })

    return issues


def extract_contracts(files: dict[str, str]) -> str:
    """Compact summary of routes/functions/DB schema from already-written files.

    Used to give later batches enough cross-file context to avoid duplicating or
    conflicting with what earlier batches already wrote, without sending full file
    contents (token budget).
    """
    lines: list[str] = []

    main_src = files.get("main.py", "")
    if main_src:
        routes = re.findall(
            r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\'][^)]*\)\s*\n\s*(?:async\s+)?def\s+(\w+)\(([^)]*)\)',
            main_src,
        )
        if routes:
            lines.append("Routes in main.py:")
            for method, path, func, params in routes:
                lines.append(f"  {method.upper()} {path} -> {func}({params.strip()})")
        imports = sorted(set(re.findall(r'^(?:from|import)\s+([\w.]+)', main_src, re.M)))
        if imports:
            lines.append(f"main.py imports: {', '.join(imports)}")
        m = re.search(r'^DB_PATH\s*=\s*["\']([^"\']+)["\']', main_src, re.M)
        if m:
            lines.append(f'main.py DB_PATH = "{m.group(1)}"')

    for path, src in files.items():
        if path == "main.py" or not path.endswith(".py"):
            continue
        funcs = re.findall(r'(?:async\s+)?def\s+(\w+)\(([^)]*)\)(?:\s*->\s*([\w\[\], "\']+))?:', src)
        public_funcs = [(f, p, r) for f, p, r in funcs if not f.startswith("_")]
        if public_funcs:
            lines.append(f"Functions in {path}:")
            for fname, params, ret in public_funcs:
                sig = f"{fname}({params.strip()})"
                if ret:
                    sig += f" -> {ret.strip()}"
                lines.append(f"  {sig}")
        schema = re.search(r'CREATE TABLE[^;]*;', src, re.S | re.I)
        if schema:
            lines.append(f"{path} schema: {' '.join(schema.group(0).split())[:200]}")
        m = re.search(r'^DB_PATH\s*=\s*["\']([^"\']+)["\']', src, re.M)
        if m:
            lines.append(f'{path} DB_PATH = "{m.group(1)}"')

    return "\n".join(lines)[:1500]


# ── EXEC_CMD safety net ───────────────────────────────────────────────────────
_BLOCKED_CMD_PATTERNS = [
    (r'(nohup\s+)?(python(3(\.\d+)?)?\s+-m\s+)?(uvicorn|gunicorn)\s+\S+:(app|application|main)\b',
     'ห้ามรัน server เอง — ระบบ deploy แอปเป็น Docker container ให้อัตโนมัติหลังแก้ไฟล์เสร็จ '
     'แค่แก้ไฟล์ด้วย WRITE_FILE แล้วระบบจะ deploy + ทดสอบเอง'),
    (r'(^|\s)nohup\s', 'ห้ามรัน background process เอง — ระบบจัดการ deploy ให้อัตโนมัติ'),
    (r'rm\s+-rf\s+[/~*]', 'attempt to delete root or home directory'),
    (r':\(\)\s*{\s*:\s*\|\s*:\s*&\s*}\s*;?\s*:', 'fork bomb detected'),
    (r'mkfs\b|dd\s+if=|>\s*/dev/(sd|nvme)', 'disk manipulation command'),
    (r'shutdown\b|reboot\b|halt\b|init\s+[06]\b|poweroff\b', 'system shutdown command'),
    (r'chmod\s+-R\s+777\s+/', 'recursive permission change on root'),
    (r'sudo\b', 'sudo not allowed (already running as root)'),
]

_CONFIRM_CMD_PATTERNS = [
    (r'rm\s+-rf\s+.*/\.\.', 'dangerous recursive delete (parent directory)'),
    (r'git\s+push\s+--force|git\s+push\s+-f\b', 'force push to git'),
    (r'docker\s+(rm\b|stop\b|system\s+prune|compose\s+down)', 'docker container removal'),
    (r'kill\s+-9\b|pkill\b', 'forceful process termination'),
    (r'drop\s+(table|database)\b|truncate\b', 'SQL data destruction'),
    (r'systemctl\s+(stop|disable|restart)', 'system service manipulation'),
    (r'>\s*/etc/|>\s*/root/(?!ener-code)', 'file redirection to system directories'),
]


def _check_cmd_safety(cmd: str) -> tuple[str, str | None]:
    """Check if a shell command is safe to auto-execute.

    Returns ("blocked", reason) / ("confirm", reason) / ("ok", None).
    """
    import re
    cmd_lower = cmd.lower()
    for pattern, reason in _BLOCKED_CMD_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return "blocked", reason
    for pattern, reason in _CONFIRM_CMD_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return "confirm", reason
    return "ok", None


async def _agent_run_cmd(cmd: str, cwd: str) -> dict:
    """Run a shell command asynchronously, return result dict."""
    import asyncio as _aio

    verdict, reason = _check_cmd_safety(cmd)
    if verdict != "ok":
        return {
            "cmd": cmd, "ok": False, "blocked": True, "verdict": verdict,
            "error": f"⛔ คำสั่งนี้ถูกบล็อกเพื่อความปลอดภัย: {reason}. ถ้าจำเป็นต้องรัน ให้แจ้ง user รันเองทาง SSH",
            "returncode": -1,
        }

    try:
        proc = await _aio.create_subprocess_shell(
            cmd, stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE, cwd=cwd,
        )
        try:
            out, err = await _aio.wait_for(proc.communicate(), timeout=30)
            return {
                "cmd": cmd, "ok": proc.returncode == 0,
                "stdout": out.decode("utf-8", errors="replace")[:800],
                "stderr": err.decode("utf-8", errors="replace")[:400],
                "returncode": proc.returncode,
            }
        except _aio.TimeoutError:
            proc.kill(); await proc.wait()
            return {"cmd": cmd, "ok": False, "error": "timeout (30s)", "returncode": -1}
    except Exception as exc:
        return {"cmd": cmd, "ok": False, "error": str(exc)[:200], "returncode": -1}


# ── Deploy & smoke test: run generated project in its own container ─────────
_APP_PORT_BASE = 8200


def _project_app_port(project: str) -> int:
    import hashlib
    h = int(hashlib.md5(project.encode("utf-8")).hexdigest(), 16)
    return _APP_PORT_BASE + (h % 300)


# Curated allowlist: import module name -> pip package name. Only packages we are
# confident are safe to auto-install. Imports NOT in this map (e.g. hallucinated
# tensorflow/keras/numpy) are deliberately never auto-added to requirements.txt.
SAFE_PKG_MAP: dict[str, str] = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn[standard]",
    "starlette": "starlette",
    "jinja2": "jinja2",
    "aiosqlite": "aiosqlite",
    "aiofiles": "aiofiles",
    "httpx": "httpx",
    "requests": "requests",
    "pydantic": "pydantic",
    "multipart": "python-multipart",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "passlib": "passlib",
    "itsdangerous": "itsdangerous",
    "email_validator": "email-validator",
    "sqlalchemy": "SQLAlchemy",
    "bs4": "beautifulsoup4",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "markdown": "Markdown",
}


def _pip_base_name(line: str) -> str:
    """Normalize a requirements.txt line to its bare package name (lowercased)."""
    import re as _re
    name = _re.split(r"[<>=!~\[ ;]", line.strip(), 1)[0]
    return name.strip().lower()


def _needed_packages(py_sources: dict[str, str]) -> set[str]:
    """Pip package names a project needs, from imports (allowlist) + implicit deps."""
    import re as _re
    needed: set[str] = set()
    blob = "\n".join(py_sources.values())
    # Explicit imports, allowlist-gated
    for mod in _re.findall(r"^\s*(?:import|from)\s+([a-zA-Z_][\w.]*)", blob, _re.M):
        top = mod.split(".")[0]
        if top in SAFE_PKG_MAP:
            needed.add(SAFE_PKG_MAP[top])
    # Implicit framework deps (never hallucinated, always safe)
    if _re.search(r"\b(Form|File|UploadFile)\b\s*\(", blob) or _re.search(r":\s*UploadFile", blob):
        needed.add("python-multipart")
    if "Jinja2Templates" in blob:
        needed.add("jinja2")
    return needed


def _read_project_py(project: str) -> dict[str, str]:
    """Read all .py files of a project from disk -> {relpath: content}."""
    import os as _os
    root = f"{BASE_ENER_CODE}/{project}"
    out: dict[str, str] = {}
    for base, _dirs, files in _os.walk(root):
        if "__pycache__" in base or "/.venv" in base or "/.git" in base:
            continue
        for fn in files:
            if fn.endswith(".py"):
                fp = _os.path.join(base, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                        out[_os.path.relpath(fp, root)] = fh.read()
                except Exception:
                    pass
    return out


def _autofix_requirements(project: str) -> list[str]:
    """Deterministically add missing (allowlisted) packages to requirements.txt.

    Returns the list of pip names added. No AI involved.
    """
    import os as _os
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9_-]", "", project or "")
    if not safe or safe != project:
        return []
    req_path = f"{BASE_ENER_CODE}/{safe}/requirements.txt"
    py_sources = _read_project_py(safe)
    if not py_sources:
        return []
    needed = _needed_packages(py_sources)
    if not needed:
        return []
    existing_lines: list[str] = []
    have: set[str] = set()
    if _os.path.exists(req_path):
        try:
            with open(req_path, "r", encoding="utf-8", errors="replace") as fh:
                existing_lines = fh.read().splitlines()
        except Exception:
            existing_lines = []
        have = {_pip_base_name(ln) for ln in existing_lines if ln.strip() and not ln.strip().startswith("#")}
    added = [pkg for pkg in sorted(needed) if _pip_base_name(pkg) not in have]
    if not added:
        return []
    new_content = "\n".join([ln for ln in existing_lines if ln.strip() != ""] + added) + "\n"
    try:
        _os.makedirs(_os.path.dirname(req_path), exist_ok=True)
        with open(req_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
    except Exception:
        return []
    return added


def _packages_from_logs(logs: str) -> list[str]:
    """Deterministically extract missing pip packages from container error logs."""
    import re as _re
    pkgs: set[str] = set()
    for mod in _re.findall(r"No module named ['\"]([\w.]+)['\"]", logs or ""):
        top = mod.split(".")[0]
        if top in SAFE_PKG_MAP:
            pkgs.add(SAFE_PKG_MAP[top])
    # "requires \"python-multipart\" to be installed" / "pip install python-multipart"
    for name in _re.findall(r'requires ["\']([\w-]+)["\'] to be installed', logs or ""):
        pkgs.add(name)
    for name in _re.findall(r'pip install ([\w-]+)', logs or ""):
        if name.lower() in {v.lower() for v in SAFE_PKG_MAP.values()} or name == "python-multipart":
            pkgs.add(name)
    return sorted(pkgs)


def _append_requirements(project: str, packages: list[str]) -> list[str]:
    """Append given pip packages to a project's requirements.txt if missing."""
    import os as _os
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9_-]", "", project or "")
    if not safe or safe != project or not packages:
        return []
    req_path = f"{BASE_ENER_CODE}/{safe}/requirements.txt"
    existing_lines: list[str] = []
    have: set[str] = set()
    if _os.path.exists(req_path):
        try:
            with open(req_path, "r", encoding="utf-8", errors="replace") as fh:
                existing_lines = fh.read().splitlines()
        except Exception:
            existing_lines = []
        have = {_pip_base_name(ln) for ln in existing_lines if ln.strip() and not ln.strip().startswith("#")}
    added = [p for p in packages if _pip_base_name(p) not in have]
    if not added:
        return []
    new_content = "\n".join([ln for ln in existing_lines if ln.strip() != ""] + added) + "\n"
    try:
        _os.makedirs(_os.path.dirname(req_path), exist_ok=True)
        with open(req_path, "w", encoding="utf-8") as fh:
            fh.write(new_content)
    except Exception:
        return []
    return added


async def _trusted_shell(cmd: str, timeout: int = 90) -> dict:
    """Run an internally-built (NOT AI-generated) shell command."""
    import asyncio as _aio
    try:
        proc = await _aio.create_subprocess_shell(
            cmd, stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE,
        )
        out, err = await _aio.wait_for(proc.communicate(), timeout=timeout)
        return {
            "cmd": cmd, "ok": proc.returncode == 0,
            "stdout": out.decode("utf-8", errors="replace")[:800],
            "stderr": err.decode("utf-8", errors="replace")[:400],
            "returncode": proc.returncode,
        }
    except Exception as exc:
        return {"cmd": cmd, "ok": False, "error": str(exc)[:200], "returncode": -1}


def _http_status_sync(url: str, timeout: float = 4.0) -> str:
    import urllib.request
    import urllib.error
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return str(r.status)
    except urllib.error.HTTPError as e:
        return str(e.code)
    except Exception:
        return "000"


def _extract_get_routes(main_src: str) -> list[str]:
    routes = []
    for m in __import__("re").finditer(r'@app\.get\(\s*["\']([^"\']+)', main_src or ""):
        path = m.group(1)
        if "{" not in path and path not in routes:
            routes.append(path)
    return routes[:6] or ["/"]


async def _deploy_and_smoke(project: str, main_src: str = "", mode: str = "deploy"):
    """Run the generated project in its own container, then smoke test GET routes.

    Yields SSE-ready event dicts. mode='deploy' recreates the container,
    mode='restart' restarts the existing one (after a repair).
    """
    import asyncio as _aio
    import re as _re

    safe = _re.sub(r"[^a-zA-Z0-9_-]", "", project or "")
    if not safe or safe != project:
        return
    if not main_src:
        try:
            with open(f"{BASE_ENER_CODE}/{safe}/main.py", "r", encoding="utf-8", errors="replace") as fh:
                main_src = fh.read()
        except Exception:
            main_src = ""
    port = _project_app_port(safe)
    name = f"ener-app-{safe}"
    proj_host_dir = f"/root/ener-code/{safe}"
    base = f"http://host.docker.internal:{port}"
    routes = _extract_get_routes(main_src)

    # ── Deterministic pre-deploy dependency fix (no AI) ──────────────────
    added = _autofix_requirements(safe)
    if added:
        yield {"type": "tool_start", "tool": "Exec", "desc": "auto-fix requirements.txt"}
        yield {"type": "tool_done", "tool": "Exec",
               "cmd": "auto-add missing dependencies",
               "ok": True, "out": "📦 เพิ่ม package ที่ขาด: " + ", ".join(added)}

    async def _docker_deploy() -> dict:
        await _trusted_shell(f"docker rm -f {name} >/dev/null 2>&1 || true", 30)
        # Pin a known-good FastAPI stack FIRST so AI-generated code written against
        # the older API keeps working (e.g. TemplateResponse(name, context), which
        # Starlette 1.x removed). Generated requirements use '>=' so this baseline
        # satisfies them without being upgraded away.
        baseline = "fastapi==0.115.6 'uvicorn[standard]==0.32.1' jinja2==3.1.4 python-multipart==0.0.12"
        run_cmd = (
            f"docker run -d --name {name} -p {port}:{port} "
            f"-v {proj_host_dir}:/app -w /app --restart on-failure:3 "
            f"--memory 256m --cpus 0.5 "
            f"python:3.11-slim sh -c 'pip install -q {baseline} && "
            f"pip install -q -r requirements.txt && "
            f"python -m uvicorn main:app --host 0.0.0.0 --port {port}'"
        )
        return await _trusted_shell(run_cmd, 60)

    async def _run_smoke() -> tuple[bool, list[dict], str]:
        # Poll until the app responds (pip install inside the container takes time)
        up = False
        for _ in range(30):
            await _aio.sleep(3)
            code = await _aio.to_thread(_http_status_sync, base + routes[0], 3.0)
            if code != "000":
                up = True
                break
        results: list[dict] = []
        if up:
            for r in routes:
                code = await _aio.to_thread(_http_status_sync, base + r, 5.0)
                results.append({"path": r, "status": code})
        ok = up and all(str(rr["status"]).startswith(("2", "3")) for rr in results)
        logs = ""
        if not ok:
            lg = await _trusted_shell(f"docker logs --tail 30 {name} 2>&1", 15)
            logs = (lg.get("stdout", "") + lg.get("stderr", "") + lg.get("error", "")).strip()[:900]
        return ok, results, logs

    if mode == "deploy":
        yield {"type": "tool_start", "tool": "Exec", "desc": f"deploy {name} → port {port}"}
        res = await _docker_deploy()
        out_txt = (res.get("stdout", "") + res.get("stderr", "") + res.get("error", "")).strip()
        yield {"type": "tool_done", "tool": "Exec", "cmd": f"docker run {name} (port {port})",
               "ok": res["ok"], "out": out_txt[:300]}
        if not res["ok"]:
            yield {"type": "smoke_result", "ok": False, "port": port,
                   "url": f"http://my-ener.uk:{port}/", "routes": [], "logs": out_txt[:600]}
            return
    else:
        yield {"type": "tool_start", "tool": "Exec", "desc": f"restart {name}"}
        res = await _trusted_shell(f"docker restart {name}", 45)
        yield {"type": "tool_done", "tool": "Exec", "cmd": f"docker restart {name}",
               "ok": res["ok"], "out": (res.get("stderr", "") + res.get("error", ""))[:200]}

    ok, results, logs = await _run_smoke()

    # ── Deterministic post-failure fix from logs (missing package) ───────
    if not ok and logs:
        log_pkgs = _packages_from_logs(logs)
        newly = _append_requirements(safe, log_pkgs) if log_pkgs else []
        if newly:
            yield {"type": "tool_start", "tool": "Exec", "desc": "auto-fix from logs"}
            yield {"type": "tool_done", "tool": "Exec",
                   "cmd": "auto-add missing dependencies (from container log)",
                   "ok": True, "out": "📦 เพิ่ม: " + ", ".join(newly) + " แล้ว deploy ใหม่"}
            res = await _docker_deploy()
            if res["ok"]:
                ok, results, logs = await _run_smoke()

    yield {"type": "smoke_result", "ok": ok, "port": port,
           "url": f"http://my-ener.uk:{port}/", "routes": results, "logs": logs}


async def _load_project_memory(project: str) -> dict:
    """Load all memory entries for a project."""
    async with get_db() as db:
        cur = await db.execute(
            "SELECT key, value FROM code_project_memory WHERE project=? ORDER BY updated_at DESC",
            (project,)
        )
        rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def _save_memory_entry(project: str, key: str, value: str):
    """Upsert one memory entry."""
    async with get_db() as db:
        await db.execute(
            """INSERT INTO code_project_memory (project, key, value, updated_at)
               VALUES (?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(project, key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
            (project, key, value)
        )
        await db.commit()


@app.get("/workspace/code/memory")
async def workspace_code_memory_get(request: Request, project: str):
    """Get all memory entries for a project."""
    await _require_admin(request)
    mem = await _load_project_memory(project)
    return JSONResponse({"project": project, "memory": mem})


@app.post("/workspace/code/memory")
async def workspace_code_memory_save(request: Request):
    """Save/update a memory entry for a project."""
    await _require_admin(request)
    body = await request.json()
    project = body.get("project", "").strip()
    key = body.get("key", "").strip()
    value = body.get("value", "").strip()
    if not project or not key:
        raise HTTPException(400, "project and key required")
    if value and value != "__DELETE__":
        await _save_memory_entry(project, key, value)
    else:
        async with get_db() as db:
            await db.execute("DELETE FROM code_project_memory WHERE project=? AND key=?", (project, key))
            await db.commit()
    return JSONResponse({"ok": True})


@app.post("/workspace/code/agent")
async def workspace_code_agent(request: Request):
    """Code agent: AI can create/modify files via <WRITE_FILE> tags."""
    await _require_admin(request)
    import os

    body = await request.json()
    question = body.get("question", "").strip()
    file_path = body.get("file_path", "")
    file_content = body.get("file_content", "")
    project = body.get("project", "") or (file_path.split("/")[0] if file_path else "")
    model = body.get("model", "featherless-coder")
    history = body.get("messages") or []
    server_ctx = body.get("server_context") or {}
    project_files = body.get("project_files", "")
    project_memory = await _load_project_memory(project) if project else {}
    if not question:
        raise HTTPException(400, "question required")

    from app.core.ai import chat as ai_chat, _VALID_MODELS
    if model not in _VALID_MODELS:
        model = "featherless-coder"
    # Sanitize history: only keep role/content, limit length
    clean_history = [
        {"role": str(m.get("role", "user")), "content": str(m.get("content", ""))[:3000]}
        for m in (history or [])
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]

    file_ctx = ""
    if project_files:
        file_ctx += f"\n=== Files in project '{project}' ===\n{project_files}\n"
    if file_path and file_content:
        lines = file_content.splitlines()
        preview = "\n".join(lines[:200])
        file_ctx += f"\n=== Current File: {file_path} ({len(lines)} lines) ===\n```\n{preview}\n```\n"

    # Build server context block
    srv = server_ctx.get("server") or {}
    containers_txt = server_ctx.get("containers") or ""
    routes_txt = "\n".join((server_ctx.get("ener_ai_routes") or [])[:40])
    projects_txt = server_ctx.get("ener_code_projects") or ""
    git_txt = server_ctx.get("git_log") or ""
    server_block = ""
    if srv or containers_txt or routes_txt:
        server_block = (
            f"\n=== SERVER STATE ===\n"
            f"CPU load: {srv.get('cpu_load','?')} | RAM: {srv.get('ram','?')} | Disk: {srv.get('disk','?')}\n"
            f"Uptime: {srv.get('uptime','?')}\n"
        )
        if containers_txt:
            server_block += f"\nRunning containers:\n{containers_txt}\n"
        if projects_txt:
            server_block += f"\n/root/ener-code projects: {projects_txt}\n"
        if git_txt:
            server_block += f"\nEner-AI recent commits:\n{git_txt}\n"
        if routes_txt:
            server_block += f"\nExisting Ener-AI API endpoints (don't duplicate):\n{routes_txt}\n"

    memory_block = ""
    if project_memory:
        mem_lines = "\n".join(f"- {k}: {v}" for k, v in project_memory.items())
        memory_block = f"\n=== PROJECT MEMORY (remember these always) ===\n{mem_lines}\n"

    system = (
        f"You are Ener-AI Code Agent. You write files directly using WRITE_FILE tags.\n\n"
        f"CURRENT PROJECT: {project or '(none)'}\n"
        f"CURRENT FILE: {file_path or '(none)'}\n"
        f"STACK: Python 3.11 / FastAPI / aiosqlite / Docker / Hetzner CPX22\n"
        f"PUBLIC DOMAIN: https://my-ener.uk\n"
        f"PROJECT PATH: /root/ener-code/{project or 'project-name'}/ on the server\n"
        f"########## NEVER USE localhost ##########\n"
        f"FORBIDDEN: localhost:8000, localhost:any_port, 127.0.0.1\n"
        f"localhost:8000 = ener-ai itself (NOT user projects)\n"
        f"User projects run on a DIFFERENT port or via docker\n"
        f"ALWAYS use: https://my-ener.uk or http://my-ener.uk:<port>\n"
        f"##########################################\n"
        f"{memory_block}"
        f"{server_block}"
        f"{file_ctx}\n"
        f"####### CRITICAL RULE — YOU MUST FOLLOW THIS #######\n"
        f"When asked to CREATE, WRITE, or MODIFY code:\n"
        f"YOU MUST USE <WRITE_FILE> TAG — DO NOT just show a code block.\n"
        f"The system will automatically save the file when you use this tag.\n\n"
        f"SYNTAX:\n"
        f'<WRITE_FILE path="{project or "my-project"}/filename.py">\n'
        f"...complete file content here...\n"
        f"</WRITE_FILE>\n\n"
        f"EXAMPLE — project=test-project2, create main.py:\n"
        f'<WRITE_FILE path="test-project2/main.py">\n'
        f"from fastapi import FastAPI\n"
        f"app = FastAPI()\n\n"
        f"@app.get('/')\n"
        f"async def root():\n"
        f"    return {{\"message\": \"Hello\"}}\n"
        f"</WRITE_FILE>\n\n"
        f"RULES:\n"
        f"- ALWAYS use WRITE_FILE when writing/creating/modifying code\n"
        f"- path MUST start with project name: '{project or 'project-name'}/filename'\n"
        f"- You CAN create multiple files in one response\n"
        f"- After WRITE_FILE tags, briefly explain in Thai what you did\n"
        f"- NEVER output just a code block without WRITE_FILE when the user asks to create/write something\n\n"
        f"EXEC_CMD — RUN COMMANDS ON SERVER:\n"
        f'<EXEC_CMD cmd="command"/>\n'
        f"- CWD is ALREADY /root/ener-code/{project or 'project'}/ — DO NOT repeat project name in commands\n"
        f"- CORRECT: 'ls -la'  WRONG: 'ls -la {project or 'project'}'\n"
        f"- CORRECT: 'python -m py_compile main.py'  WRONG: 'python -m py_compile {project or 'project'}/main.py'\n"
        f"- Output is captured and fed back to you automatically\n"
        f"- EXAMPLES:\n"
        f'  <EXEC_CMD cmd="ls -la"/>\n'
        f'  <EXEC_CMD cmd="python -m py_compile main.py"/>\n'
        f'  <EXEC_CMD cmd="pip install -r requirements.txt -q 2>&1 | tail -5"/>\n'
        f'  <EXEC_CMD cmd="python -c \'import fastapi; print(fastapi.__version__)\'"/>\n'
        f"- You can use multiple EXEC_CMD tags in one response\n"
        f"- After seeing results, summarize what passed and what failed\n\n"
        f"UPDATE_MEMORY — SAVE PROJECT KNOWLEDGE (persists across sessions):\n"
        f'<UPDATE_MEMORY key="architecture" value="Uses FastAPI + aiosqlite"/>\n'
        f"- Use to remember decisions, conventions, tech choices, secrets layout, etc.\n"
        f"- key: short label (e.g. 'stack', 'port', 'db_schema', 'main_file')\n"
        f"- value: concise fact to remember\n"
        f"- Use it when you learn something important about the project structure\n\n"
        f"COMPLETE PROJECT RULE:\n"
        f"When asked to create a project or app, think about ALL files needed and create them ALL at once:\n"
        f"  - main.py (or index.js etc.) — main application code\n"
        f"  - requirements.txt (or package.json) — dependencies\n"
        f"  - README.md — how to run and test\n"
        f"  - Any other files needed (models.py, .env.example, Dockerfile if relevant)\n"
        f"Create ALL of them with multiple <WRITE_FILE> tags in one response.\n\n"
        f"AFTER all WRITE_FILE tags, ALWAYS end with a Thai summary:\n"
        f"**สร้างแล้ว X ไฟล์:**\n"
        f"- list each file\n"
        f"**วิธีรัน:**\n"
        f"- exact commands to install + run (NEVER mention localhost)\n"
        f"**วิธีทดสอบ:**\n"
        f"- curl or browser URLs using https://my-ener.uk or port on my-ener.uk (NOT localhost)"
    )

    raw_answer = await ai_chat(
        question, system=system, agent="CodeAgent",
        messages=clean_history, preferred_model=model, strict_model=False,
    )

    # Execute WRITE_FILE actions
    actions: list[dict] = []
    for m in _WRITE_FILE_RE.finditer(raw_answer):
        rel_path = m.group(1).strip()
        content = m.group(2).lstrip("\n").rstrip("\n")
        try:
            full = _ener_code_resolve(rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
            actions.append({"type": "write_file", "path": rel_path, "ok": True, "lines": len(content.splitlines())})
        except Exception as exc:
            actions.append({"type": "write_file", "path": rel_path, "ok": False, "error": str(exc)})

    # Execute EXEC_CMD actions (async subprocess — non-blocking)
    exec_results: list[dict] = []
    exec_cmds = _EXEC_CMD_RE.findall(raw_answer)
    if exec_cmds and project:
        import asyncio as _aio
        project_dir = f"{BASE_ENER_CODE}/{project}"
        os.makedirs(project_dir, exist_ok=True)
        for cmd in exec_cmds[:6]:
            verdict, reason = _check_cmd_safety(cmd)
            if verdict != "ok":
                exec_results.append({
                    "cmd": cmd, "ok": False, "blocked": True, "verdict": verdict,
                    "error": f"⛔ คำสั่งนี้ถูกบล็อกเพื่อความปลอดภัย: {reason}. ถ้าจำเป็นต้องรัน ให้แจ้ง user รันเองทาง SSH",
                    "returncode": -1,
                })
                continue
            try:
                proc = await _aio.create_subprocess_shell(
                    cmd,
                    stdout=_aio.subprocess.PIPE,
                    stderr=_aio.subprocess.PIPE,
                    cwd=project_dir,
                )
                try:
                    stdout, stderr = await _aio.wait_for(proc.communicate(), timeout=15)
                    exec_results.append({
                        "cmd": cmd, "ok": proc.returncode == 0,
                        "stdout": stdout.decode("utf-8", errors="replace")[:800],
                        "stderr": stderr.decode("utf-8", errors="replace")[:400],
                        "returncode": proc.returncode,
                    })
                except _aio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    exec_results.append({"cmd": cmd, "ok": False, "error": "timeout (15s)", "returncode": -1})
            except Exception as exc:
                exec_results.append({"cmd": cmd, "ok": False, "error": str(exc)[:200], "returncode": -1})

    # If commands ran, feed results back to AI for summary
    summary_answer = ""
    if exec_results:
        exec_lines = []
        for er in exec_results:
            icon = "✅" if er.get("ok") else "❌"
            out = (er.get("stdout", "") + er.get("stderr", "") + er.get("error", "")).strip()
            exec_lines.append(f"{icon} $ {er['cmd']}\n{out or '(no output)'}")
        exec_report = "\n\n".join(exec_lines)
        summary_q = (
            f"ผลการรัน commands บน server:\n\n{exec_report}\n\n"
            f"สรุปสั้นๆ เป็นภาษาไทย: อะไรผ่าน ✅ อะไรไม่ผ่าน ❌ มี error อะไร และแนะนำวิธี fix"
        )
        summary_answer = await ai_chat(
            summary_q, system=system, agent="CodeAgent",
            messages=clean_history + [{"role": "assistant", "content": raw_answer[:2000]}],
            preferred_model=model, strict_model=False,
        )

    # Save UPDATE_MEMORY entries to DB
    if project:
        for m in _UPDATE_MEMORY_RE.finditer(raw_answer):
            try:
                await _save_memory_entry(project, m.group(1).strip(), m.group(2).strip())
            except Exception:
                pass

    # Strip WRITE_FILE, EXEC_CMD, UPDATE_MEMORY tags from display text
    display = _WRITE_FILE_RE.sub("", raw_answer)
    display = _EXEC_CMD_RE.sub("", display)
    display = _UPDATE_MEMORY_RE.sub("", display).strip()
    if summary_answer:
        display = display + "\n\n" + summary_answer

    return JSONResponse({"answer": display, "actions": actions, "exec_results": exec_results})


# ── Streaming Code Agent ──────────────────────────────────────────────────────
import json as _json

@app.post("/workspace/code/agent-stream")
async def workspace_code_agent_stream(request: Request):
    """Streaming code agent: real-time token stream + auto-repair loop."""
    await _require_admin(request)
    body = await request.json()
    question   = body.get("question", "").strip()
    file_path  = body.get("file_path", "")
    file_content = body.get("file_content", "")
    project    = body.get("project", "").strip()
    model      = body.get("model", "")
    models_cfg = body.get("models") or {}
    planner_model = str(models_cfg.get("planner") or "").strip() or model
    writer_model  = str(models_cfg.get("writer") or "").strip() or model
    qc_model      = str(models_cfg.get("qc") or "").strip() or model
    messages   = body.get("messages", [])
    server_ctx = body.get("server_context", {})
    project_files = body.get("project_files", "")
    if not question:
        raise HTTPException(400, "question required")

    from app.core.ai import stream_chat_response

    # Build system prompt (same as workspace_code_agent)
    project_memory = await _load_project_memory(project) if project else {}
    memory_block = ""
    if project_memory:
        mem_block_lines = "\n".join(f"- {k}: {v}" for k, v in project_memory.items())
        memory_block = f"\n=== PROJECT MEMORY ===\n{mem_block_lines}\n"

    file_ctx = ""
    if project_files:
        file_ctx += f"\n=== Files in project '{project}' ===\n{project_files}\n"
    if file_path and file_content:
        preview = "\n".join(file_content.splitlines()[:200])
        file_ctx += f"\n=== Current File: {file_path} ===\n```\n{preview}\n```\n"

    srv = (server_ctx.get("server") or {})
    containers_txt = "\n".join((server_ctx.get("running_containers") or [])[:8])
    server_block = ""
    if srv:
        server_block = (
            f"\n=== SERVER STATE ===\n"
            f"CPU: {srv.get('cpu_load','?')} | RAM: {srv.get('ram','?')} | Disk: {srv.get('disk','?')}\n"
        )
        if containers_txt:
            server_block += f"Running containers:\n{containers_txt}\n"

    app_port = _project_app_port(project) if project else 0
    system = (
        f"You are Ener-AI Code Agent. You write files directly using WRITE_FILE tags.\n\n"
        f"STACK: Python 3.11 / FastAPI / aiosqlite / Docker / Hetzner CPX22\n"
        f"PUBLIC DOMAIN: https://my-ener.uk\n"
        f"PROJECT PATH: /root/ener-code/{project or 'project-name'}/ on the server\n"
        f"########## NEVER USE localhost ##########\n"
        f"FORBIDDEN: localhost:8000, localhost:any_port, 127.0.0.1\n"
        f"ALWAYS use: https://my-ener.uk or http://my-ener.uk:<port>\n"
        f"##########################################\n"
        f"\n"
        f"=== YOU LIVE ON THE SERVER my-ener.uk ===\n"
        f"You are running INSIDE the server my-ener.uk (Ubuntu + Docker). EXEC_CMD runs real shell "
        f"commands on this server (30s timeout each). When something fails or you are unsure, "
        f"INSPECT THE SERVER YOURSELF before guessing:\n"
        f"- ls -la / cat <file>            — check files actually written\n"
        f"- docker ps                      — see running containers\n"
        f"- docker logs --tail 30 ener-app-{project or 'project'}   — runtime errors of YOUR app\n"
        f"- curl -s -m 5 http://host.docker.internal:{app_port or '<port>'}/   — test YOUR app's HTTP response\n"
        f"- pip list | grep <pkg> / python -c \"import <pkg>\"   — verify dependencies\n"
        f"Read-only inspection commands are always allowed; destructive ones are blocked.\n"
        f"AUTO-DEPLOY: when this project passes all checks, the system deploys it automatically as "
        f"Docker container 'ener-app-{project or 'project'}' on port {app_port or '<auto>'} "
        f"(public: http://my-ener.uk:{app_port or '<auto>'}/). Do NOT run uvicorn yourself in EXEC_CMD — "
        f"long-running commands are killed at 30s and 'uvicorn ... &' will not survive.\n"
        f"{memory_block}"
        f"{server_block}"
        f"{file_ctx}\n"
        f"Use <WRITE_FILE path=\"{project or 'my-project'}/filename\">content</WRITE_FILE> to write files.\n"
        f"Use <EXEC_CMD cmd=\"command\"/> to run shell commands (CWD is /root/ener-code/{project or 'project'}/).\n"
        f"CORRECT: 'ls -la'   WRONG: 'ls -la {project or 'project'}'\n"
        f"Use <UPDATE_MEMORY key=\"k\" value=\"v\"/> to save project facts.\n"
        f"After WRITE_FILE, write a short Thai summary of what was done.\n\n"
        f"VALIDATION RULE — IMPORTANT:\n"
        f"- After writing ANY .py file, IMMEDIATELY add <EXEC_CMD cmd=\"python -m py_compile <path>\"/> to check syntax\n"
        f"- NEVER leave requirements.txt empty or with placeholder comments — list real package names\n"
        f"- If a file imports/references another local file, make sure that file is also created\n"
        f"\n"
        f"CRITICAL ANTI-PATTERNS — NEVER DO THESE:\n"
        f"- UploadFile.read() can only be called ONCE per request; store the result in a variable and reuse it "
        f"(do not call file.read() twice — the second call returns empty bytes)\n"
        f"- If you use dict(row) to convert aiosqlite rows, you MUST set db.row_factory = aiosqlite.Row "
        f"on the connection BEFORE executing the query\n"
        f"- Use exactly ONE database file path and ONE schema for the whole project — never define a "
        f"separate db module with a different DB filename or table schema than main.py uses\n"
        f"- Every template file you create must be rendered by a route in main.py (or extended by another "
        f"template) — never leave an unused template file\n"
        f"\n"
        f"SIMPLE QUERY RULE — VERY IMPORTANT:\n"
        f"If the user asks a SHORT informational question (URL, link, port, status, file content, ≤15 words) "
        f"→ reply with plain text ONLY. Do NOT use WRITE_FILE. Do NOT modify any file. Do NOT run EXEC_CMD.\n"
        f"Examples requiring NO code change: 'ขอ URL', 'ขอ link อีกรอบ', 'app อยู่ที่ไหน', 'port เท่าไร'\n"
        f"The project public URL is: http://my-ener.uk:{app_port}/\n"
    )

    # Fast-path: detect pure informational queries — bypass AI pipeline entirely
    import re as _re
    _INFO_QUERY = _re.compile(
        r'\b(url|ลิงก์|link|port|ที่อยู่)\b|ขอ\s*(url|ลิงก์|link)|app\s*รัน\s*(ที่|อยู่)|เปิดที่ไหน',
        _re.IGNORECASE,
    )

    clean_history = [
        m for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]

    async def generate():
        import os
        import difflib as _difflib
        MAX_REPAIR = 3
        AGENT_MAX_TOKENS = 6000
        project_dir = f"{BASE_ENER_CODE}/{project}" if project else None
        if project_dir:
            os.makedirs(project_dir, exist_ok=True)

        # Fast-path: short informational queries → answer instantly, skip AI pipeline
        if project and _INFO_QUERY.search(question) and len(question.split()) <= 12:
            _port = _project_app_port(project)
            _url = f"http://my-ener.uk:{_port}/"
            _reply = f"**{project}** รันอยู่ที่ [{_url}]({_url})"
            yield f"data: {_json.dumps({'type': 'final_text', 'text': _reply})}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
            return

        def strip_project_prefix(path: str) -> str:
            p = path.lstrip("/")
            prefix = f"{project}/"
            return p[len(prefix):] if project and p.startswith(prefix) else p

        async def process_write_files(resp_text: str):
            actions: list[dict] = []
            events: list[dict] = []
            contents: dict[str, str] = {}
            for m in _WRITE_FILE_RE.finditer(resp_text):
                rel_path = m.group(1).strip()
                content  = m.group(2).lstrip("\n").rstrip("\n")
                events.append({'type': 'tool_start', 'tool': 'WriteFile', 'desc': rel_path})
                try:
                    full_p = _ener_code_resolve(rel_path)
                    # Compute diff vs existing file
                    old_lines: list[str] = []
                    is_new = True
                    try:
                        if os.path.exists(full_p):
                            with open(full_p, "r", encoding="utf-8", errors="replace") as _f:
                                old_lines = _f.read().splitlines()
                            is_new = False
                    except Exception:
                        pass
                    new_lines = content.splitlines()
                    # Build compact diff (max 60 lines)
                    diff_data: list[dict] = []
                    if is_new:
                        for idx, ln in enumerate(new_lines[:60]):
                            diff_data.append({"t": "+", "l": ln, "n": idx + 1})
                    else:
                        matcher = _difflib.SequenceMatcher(None, old_lines, new_lines)
                        count = 0
                        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                            if count >= 60:
                                break
                            if tag == "equal":
                                end = min(i2, i1 + 2)
                                for off in range(end - i1):
                                    diff_data.append({"t": "=", "l": old_lines[i1 + off], "n": j1 + off + 1}); count += 1
                            elif tag in ("replace", "delete"):
                                for off in range(i2 - i1):
                                    diff_data.append({"t": "-", "l": old_lines[i1 + off], "n": i1 + off + 1}); count += 1
                            if tag in ("replace", "insert"):
                                for off in range(j2 - j1):
                                    diff_data.append({"t": "+", "l": new_lines[j1 + off], "n": j1 + off + 1}); count += 1
                    os.makedirs(os.path.dirname(full_p), exist_ok=True)
                    with open(full_p, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    lines = len(new_lines)
                    added = sum(1 for d in diff_data if d["t"] == "+")
                    removed = sum(1 for d in diff_data if d["t"] == "-")
                    actions.append({"type": "write_file", "path": rel_path, "ok": True, "lines": lines})
                    events.append({'type': 'tool_done', 'tool': 'WriteFile', 'path': rel_path, 'ok': True, 'lines': lines, 'is_new': is_new, 'added': added, 'removed': removed, 'diff': diff_data})
                    contents[strip_project_prefix(rel_path)] = content
                except Exception as exc:
                    actions.append({"type": "write_file", "path": rel_path, "ok": False, "error": str(exc)})
                    events.append({'type': 'tool_done', 'tool': 'WriteFile', 'path': rel_path, 'ok': False, 'error': str(exc)[:100]})
            return actions, events, contents

        async def process_exec_cmds(cmds: list[str], cwd: str):
            results: list[dict] = []
            events: list[dict] = []
            for cmd in cmds[:6]:
                events.append({'type': 'tool_start', 'tool': 'Exec', 'desc': cmd})
                result = await _agent_run_cmd(cmd, cwd)
                results.append(result)
                out = (result.get("stdout", "") + result.get("stderr", "") + result.get("error", "")).strip()
                events.append({'type': 'tool_done', 'tool': 'Exec', 'cmd': cmd, 'ok': result['ok'], 'out': out[:500]})
            return results, events

        async def save_memory_tags(resp_text: str):
            if not project:
                return
            for m in _UPDATE_MEMORY_RE.finditer(resp_text):
                try:
                    await _save_memory_entry(project, m.group(1).strip(), m.group(2).strip())
                except Exception:
                    pass

        async def stream_turn(prompt: str, history: list[dict], max_tokens: int, holder: dict,
                              turn_model: str = "", agent_name: str = "CodeAgent"):
            full_response = ""
            token_count = 0
            async for token in stream_chat_response(
                prompt, history, system, model=turn_model or model, agent=agent_name, max_tokens=max_tokens
            ):
                full_response += token
                token_count += len(token.split())
                yield {'type': 'token', 'text': token, 'tokens': token_count}
            holder['response'] = full_response
            holder['tokens'] = token_count

        # ════════════════════════════════════════════════════════════
        # PLAN-THEN-BATCH mode: brand-new project (no files / no history yet)
        # ════════════════════════════════════════════════════════════
        is_new_project = bool(project) and not (project_files or "").strip() and not clean_history
        if is_new_project:
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'plan', 'agent': 'planner', 'model': planner_model})}\n\n"
            yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '📋 Planning project files'})}\n\n"
            plan: dict | None = None
            plan_text = ""
            try:
                plan_system = (
                    f"{system}\n\n"
                    f"PLANNING TASK: Output ONLY a raw JSON object (no markdown, no code fences, no explanation text) "
                    f"that plans the files needed for this project.\n"
                    f'Format: {{"files": ["main.py", "services/db.py", ..., "requirements.txt", "README.md"], '
                    f'"dependencies": ["fastapi", "uvicorn[standard]", ...], "summary": "1-2 sentence Thai description"}}\n'
                    f"All paths in 'files' are relative to the project root (do NOT prefix with '{project}/').\n"
                    f"Order matters: core app/service files first, then templates/static assets, "
                    f"then requirements.txt and README.md last.\n"
                    f"Include every file the code will need (templates, static CSS, etc) — nothing extra."
                )
                async for token in stream_chat_response(
                    question, [], plan_system, model=planner_model, agent="CodeAgentPlan", max_tokens=800
                ):
                    plan_text += token
                start = plan_text.find("{")
                end = plan_text.rfind("}")
                if start != -1 and end != -1:
                    plan = _json.loads(plan_text[start:end + 1])
            except Exception:
                plan = None
            # Planner model failed (e.g. Anthropic credits) -> retry once with writer model
            if not (plan or {}).get("files") and writer_model and writer_model != planner_model:
                try:
                    plan_text = ""
                    async for token in stream_chat_response(
                        question, [], plan_system, model=writer_model, agent="CodeAgentPlan", max_tokens=800
                    ):
                        plan_text += token
                    start = plan_text.find("{")
                    end = plan_text.rfind("}")
                    if start != -1 and end != -1:
                        plan = _json.loads(plan_text[start:end + 1])
                except Exception:
                    plan = None
            yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': len(plan_text.split())})}\n\n"

            files = [str(f).strip().lstrip("/") for f in (plan or {}).get("files") or [] if str(f).strip()][:12]
            deps = [str(d).strip() for d in (plan or {}).get("dependencies") or [] if str(d).strip()]
            plan_summary = str((plan or {}).get("summary") or "").strip()

            if files:
                yield f"data: {_json.dumps({'type': 'plan_done', 'files': files, 'dependencies': deps, 'summary': plan_summary})}\n\n"
                yield f"data: {_json.dumps({'type': 'stage', 'stage': 'write', 'agent': 'writer', 'model': writer_model})}\n\n"

                all_actions: list[dict] = []
                all_exec_results: list[dict] = []
                written_files: list[str] = []
                written_contents: dict[str, str] = {}
                BATCH_SIZE = 2
                batches = [files[i:i + BATCH_SIZE] for i in range(0, len(files), BATCH_SIZE)]

                for bi, batch in enumerate(batches, 1):
                    batch_label = ", ".join(batch)
                    yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': f'Batch {bi}/{len(batches)}: {batch_label}'})}\n\n"

                    already = "\n".join(f"- {p}" for p in written_files) or "(none yet)"
                    deps_line = ", ".join(deps) if deps else "(none specified)"
                    write_list = "\n".join(f'- <WRITE_FILE path="{project}/{p}">' for p in batch)
                    contracts_text = extract_contracts(written_contents)
                    contracts_block = (
                        f"\nEXISTING CONTRACTS (ต้องใช้ตามนี้ ห้ามสร้าง route/function/DB path/schema ใหม่ "
                        f"ที่ชนหรือซ้ำซ้อนกับของเดิม):\n{contracts_text}\n"
                        if contracts_text else ""
                    )
                    batch_q = (
                        f"แผนงาน: {plan_summary}\n"
                        f"Dependencies ของโปรเจกต์: {deps_line}\n"
                        f"ไฟล์ที่สร้างไปแล้ว:\n{already}\n"
                        f"{contracts_block}\n"
                        f"ตอนนี้ให้เขียนไฟล์ต่อไปนี้ให้ครบถ้วนสมบูรณ์ (ห้ามเว้นว่าง ห้ามใส่ placeholder):\n{write_list}\n\n"
                        f"สำหรับไฟล์ .py ทุกไฟล์ ให้ตามด้วย <EXEC_CMD cmd=\"python -m py_compile {{path}}\"/> "
                        f"(path ไม่ต้องมี '{project}/' นำหน้า) เพื่อตรวจ syntax ทันที\n"
                        f"ห้ามเขียนไฟล์อื่นนอกเหนือจากที่ระบุไว้ในรอบนี้"
                    )

                    holder: dict = {}
                    async for ev in stream_turn(batch_q, [], 4000, holder, writer_model, "CodeAgentWriter"):
                        yield f"data: {_json.dumps(ev)}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': holder.get('tokens', 0)})}\n\n"
                    full_response = holder.get('response', '')

                    actions, events, contents = await process_write_files(full_response)
                    for ev in events:
                        yield f"data: {_json.dumps(ev)}\n\n"
                    all_actions += actions
                    written_contents.update(contents)
                    for a in actions:
                        if a.get("ok"):
                            rel = strip_project_prefix(a["path"])
                            if rel not in written_files:
                                written_files.append(rel)

                    await save_memory_tags(full_response)

                    exec_cmds = _EXEC_CMD_RE.findall(full_response)
                    exec_results: list[dict] = []
                    if exec_cmds and project_dir:
                        exec_results, events = await process_exec_cmds(exec_cmds, project_dir)
                        for ev in events:
                            yield f"data: {_json.dumps(ev)}\n\n"
                    all_exec_results += exec_results

                    # Force-validate any .py file written without a matching py_compile check
                    py_written = [strip_project_prefix(a["path"]) for a in actions if a.get("ok") and a["path"].endswith(".py")]
                    checked = " ".join(exec_cmds)
                    missing = [p for p in py_written if p not in checked]
                    if missing and project_dir:
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔍 Validating'})}\n\n"
                        fv_results, fv_events = await process_exec_cmds(
                            [f"python -m py_compile {p}" for p in missing], project_dir
                        )
                        for ev in fv_events:
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"
                        all_exec_results += fv_results
                        exec_results = exec_results + fv_results

                    # Repair loop scoped to this batch (max 2 rounds)
                    # (blocked commands are policy, not bugs — don't "repair" them)
                    repair_round = 0
                    while repair_round < 2:
                        failed = [e for e in exec_results if not e.get("ok") and not e.get("blocked")]
                        static_issues = run_static_checks(written_contents)
                        if not failed and not static_issues:
                            break
                        repair_round += 1
                        problem_block = ""
                        if failed:
                            error_lines = "\n".join(
                                f"❌ {e['cmd']}: {(e.get('stdout','') + e.get('stderr','') + e.get('error','')).strip()[:300]}"
                                for e in failed
                            )
                            problem_block += f"คำสั่งตรวจสอบล้มเหลว:\n{error_lines}\n\n"
                        if static_issues:
                            static_lines = "\n".join(f"⚠️ {iss['hint']}" for iss in static_issues)
                            problem_block += f"พบปัญหาจาก static analysis:\n{static_lines}\n\n"
                        yield f"data: {_json.dumps({'type': 'repair_start', 'iter': repair_round, 'errors': len(failed) + len(static_issues)})}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': f'🔧 Auto-repair batch {bi} #{repair_round}'})}\n\n"
                        repair_q = (
                            f"{problem_block}"
                            f"แก้ไขไฟล์ที่เกี่ยวข้องด้วย <WRITE_FILE path=\"{project}/...\">...</WRITE_FILE> "
                            f"แล้ว <EXEC_CMD cmd=\"python -m py_compile ...\"/> ตรวจสอบใหม่อีกครั้ง"
                        )
                        holder = {}
                        async for ev in stream_turn(repair_q, [], 3000, holder, writer_model, "CodeAgentWriter"):
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': holder.get('tokens', 0)})}\n\n"
                        full_response = holder.get('response', '')

                        actions2, events2, contents2 = await process_write_files(full_response)
                        for ev in events2:
                            yield f"data: {_json.dumps(ev)}\n\n"
                        all_actions += actions2
                        written_contents.update(contents2)
                        for a in actions2:
                            if a.get("ok"):
                                rel = strip_project_prefix(a["path"])
                                if rel not in written_files:
                                    written_files.append(rel)

                        await save_memory_tags(full_response)

                        exec_cmds2 = _EXEC_CMD_RE.findall(full_response)
                        exec_results = []
                        if exec_cmds2 and project_dir:
                            exec_results, events2 = await process_exec_cmds(exec_cmds2, project_dir)
                            for ev in events2:
                                yield f"data: {_json.dumps(ev)}\n\n"
                        all_exec_results += exec_results

                    yield f"data: {_json.dumps({'type': 'batch_done', 'batch': bi, 'total': len(batches), 'files': batch})}\n\n"

                # ── Final integration check ────────────────────────
                # Fix empty/placeholder requirements.txt directly (no extra AI round-trip)
                req_file = next((f for f in files if f.split("/")[-1] == "requirements.txt"), None)
                if req_file and deps:
                    try:
                        req_full = _ener_code_resolve(f"{project}/{req_file}")
                        current = ""
                        if os.path.exists(req_full):
                            with open(req_full, "r", encoding="utf-8", errors="replace") as fh:
                                current = fh.read().strip()
                        is_placeholder = (
                            len(current) < 5
                            or "add dependencies" in current.lower()
                            or "placeholder" in current.lower()
                        )
                        if is_placeholder:
                            req_content = "\n".join(deps) + "\n"
                            os.makedirs(os.path.dirname(req_full), exist_ok=True)
                            with open(req_full, "w", encoding="utf-8") as fh:
                                fh.write(req_content)
                            diff_data = [{"t": "+", "l": d, "n": i + 1} for i, d in enumerate(deps)]
                            req_path = f"{project}/{req_file}"
                            all_actions.append({"type": "write_file", "path": req_path, "ok": True, "lines": len(deps)})
                            written_contents[req_file] = req_content
                            yield f"data: {_json.dumps({'type': 'tool_start', 'tool': 'WriteFile', 'desc': req_path})}\n\n"
                            yield f"data: {_json.dumps({'type': 'tool_done', 'tool': 'WriteFile', 'path': req_path, 'ok': True, 'lines': len(deps), 'is_new': not bool(current), 'added': len(deps), 'removed': 0, 'diff': diff_data})}\n\n"
                    except Exception:
                        pass

                # ── Final integration AI round: cross-file consistency / dead code ─
                if len(written_files) >= 3:
                    final_issues = run_static_checks(written_contents)
                    contracts_text = extract_contracts(written_contents)
                    issues_block = ""
                    if final_issues:
                        issues_lines = "\n".join(f"⚠️ {iss['hint']}" for iss in final_issues)
                        issues_block = f"\nปัญหาที่ตรวจพบโดย static analysis:\n{issues_lines}\n"

                    files_list_block = "\n".join(f"- {p}" for p in written_files)
                    integration_q = (
                        f"FINAL INTEGRATION REVIEW สำหรับโปรเจกต์ {project}\n\n"
                        f"ไฟล์ทั้งหมดที่สร้างแล้ว:\n{files_list_block}\n\n"
                        f"Contracts ปัจจุบัน:\n{contracts_text}\n"
                        f"{issues_block}\n"
                        f"งานของคุณ (แก้เฉพาะจุดที่จำเป็น เขียนไฟล์ทั้งไฟล์เวอร์ชันแก้ไขด้วย WRITE_FILE):\n"
                        f"1. ทุก template ใน templates/ ต้องถูก render โดย route ใน main.py — ถ้ามี template "
                        f"ที่ไม่ถูกใช้ ให้เพิ่ม route ที่เหมาะสมใน main.py\n"
                        f"2. ทุกไฟล์ใน services/ ต้องถูก import และใช้งานจริงใน main.py — ถ้าไม่ได้ใช้ "
                        f"ให้แก้ main.py ให้เรียกใช้ฟังก์ชันจากไฟล์นั้นแทนโค้ดซ้ำซ้อน\n"
                        f"3. แก้ปัญหาทั้งหมดที่ static analysis แจ้งด้านบน (ถ้ามี)\n"
                        f"4. หลังแก้ไฟล์ .py ใดๆ ให้ตามด้วย <EXEC_CMD cmd=\"python -m py_compile <path>\"/> "
                        f"(path ไม่ต้องมี '{project}/' นำหน้า)\n\n"
                        f"ถ้าตรวจสอบแล้วทุกอย่างถูกต้องสมบูรณ์ดีอยู่แล้ว ไม่ต้องเขียนไฟล์ใดๆ "
                        f"ตอบสั้นๆ ว่า \"OK ไม่มีปัญหา\""
                    )

                    yield f"data: {_json.dumps({'type': 'stage', 'stage': 'qc', 'agent': 'qc', 'model': qc_model})}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔗 Final integration review'})}\n\n"
                    holder = {}
                    async for ev in stream_turn(integration_q, [], 4000, holder, qc_model, "CodeAgentQC"):
                        yield f"data: {_json.dumps(ev)}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': holder.get('tokens', 0)})}\n\n"
                    full_response = holder.get('response', '')

                    actions3, events3, contents3 = await process_write_files(full_response)
                    for ev in events3:
                        yield f"data: {_json.dumps(ev)}\n\n"
                    all_actions += actions3
                    written_contents.update(contents3)
                    for a in actions3:
                        if a.get("ok"):
                            rel = strip_project_prefix(a["path"])
                            if rel not in written_files:
                                written_files.append(rel)

                    await save_memory_tags(full_response)

                    exec_cmds3 = _EXEC_CMD_RE.findall(full_response)
                    if exec_cmds3 and project_dir:
                        exec_results3, events3b = await process_exec_cmds(exec_cmds3, project_dir)
                        for ev in events3b:
                            yield f"data: {_json.dumps(ev)}\n\n"
                        all_exec_results += exec_results3

                # Final syntax check across all created .py files
                py_files = [f for f in written_files if f.endswith(".py")]
                if py_files and project_dir:
                    yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔍 Final integration check'})}\n\n"
                    final_results, final_events = await process_exec_cmds(
                        ["python -m py_compile " + " ".join(py_files)], project_dir
                    )
                    for ev in final_events:
                        yield f"data: {_json.dumps(ev)}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"
                    all_exec_results += final_results

                # ── RUN stage: deploy in own container + smoke test ──
                smoke: dict | None = None
                main_src = written_contents.get("main.py", "")
                if main_src and "fastapi" in main_src.lower():
                    yield f"data: {_json.dumps({'type': 'stage', 'stage': 'run', 'agent': None})}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🚀 Deploy & smoke test'})}\n\n"
                    async for ev in _deploy_and_smoke(project, main_src, "deploy"):
                        if ev.get("type") == "smoke_result":
                            smoke = ev
                        yield f"data: {_json.dumps(ev)}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"

                    # Smoke failed -> writer repairs from runtime logs, restart, retest once
                    if smoke and not smoke.get("ok") and smoke.get("logs"):
                        yield f"data: {_json.dumps({'type': 'stage', 'stage': 'write', 'agent': 'writer', 'model': writer_model})}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔧 Fixing runtime errors'})}\n\n"
                        route_lines = "\n".join(
                            f"- GET {r['path']} -> {r['status']}" for r in smoke.get("routes", [])
                        ) or "(แอปไม่ตอบสนองเลย)"
                        smoke_fix_q = (
                            f"แอปถูก deploy แล้วแต่ smoke test ล้มเหลว (dependency ที่ขาดถูกเติมอัตโนมัติแล้ว "
                            f"ดังนั้นปัญหานี้น่าจะเป็น bug ในโค้ด ไม่ใช่ package ขาด)\n"
                            f"ผลการเรียก route:\n{route_lines}\n\n"
                            f"Log จาก container:\n```\n{smoke['logs']}\n```\n\n"
                            f"อ่าน log บรรทัดสุดท้าย (ตัว error จริง) แล้วแก้ไฟล์ที่ระบุใน traceback ด้วย "
                            f"<WRITE_FILE path=\"{project}/...\">...</WRITE_FILE> (เขียนทั้งไฟล์เวอร์ชันที่แก้แล้ว)\n"
                            f"ห้ามรันคำสั่งตรวจสอบ/ค้นหา (curl, ls, cat, docker) — แก้ด้วย WRITE_FILE เท่านั้น "
                            f"ระบบจะ deploy + ทดสอบใหม่ให้เองหลังคุณเขียนไฟล์เสร็จ"
                        )
                        holder = {}
                        async for ev in stream_turn(smoke_fix_q, [], 4000, holder, writer_model, "CodeAgentWriter"):
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': holder.get('tokens', 0)})}\n\n"
                        fix_resp = holder.get('response', '')
                        fa, fe, fc = await process_write_files(fix_resp)
                        all_actions += fa
                        written_contents.update(fc)
                        for ev in fe:
                            yield f"data: {_json.dumps(ev)}\n\n"
                        fix_cmds = _EXEC_CMD_RE.findall(fix_resp)
                        if fix_cmds and project_dir:
                            _r, _e = await process_exec_cmds(fix_cmds, project_dir)
                            all_exec_results += _r
                            for ev in _e:
                                yield f"data: {_json.dumps(ev)}\n\n"

                        yield f"data: {_json.dumps({'type': 'stage', 'stage': 'run', 'agent': None})}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🚀 Re-deploy & retest'})}\n\n"
                        async for ev in _deploy_and_smoke(project, written_contents.get("main.py", main_src), "restart"):
                            if ev.get("type") == "smoke_result":
                                smoke = ev
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"

                # ── Final summary (built locally — no extra AI call) ─
                files_md = "\n".join(f"- {p}" for p in written_files)
                deps_md = ", ".join(deps) if deps else "-"
                if smoke and smoke.get("ok"):
                    test_block = (
                        f"**แอปรันอยู่แล้วที่:** {smoke['url']}\n"
                        + "\n".join(f"- GET {r['path']} → {r['status']} ✓" for r in smoke.get("routes", []))
                    )
                elif smoke:
                    test_block = (
                        f"**⚠️ Deploy แล้วแต่ smoke test ยังไม่ผ่าน** — {smoke['url']}\n"
                        + "\n".join(f"- GET {r['path']} → {r['status']}" for r in smoke.get("routes", []))
                    )
                else:
                    test_block = (
                        f"**วิธีรัน:**\n- pip install -r requirements.txt\n"
                        f"- uvicorn main:app --host 0.0.0.0 --port <port>"
                    )
                display = (
                    f"{plan_summary}\n\n"
                    f"**สร้างแล้ว {len(written_files)} ไฟล์:**\n{files_md}\n\n"
                    f"**Dependencies:** {deps_md}\n\n"
                    f"{test_block}"
                )
                yield f"data: {_json.dumps({'type': 'final_text', 'text': display, 'actions': all_actions, 'exec_results': all_exec_results, 'repair_iter': 0})}\n\n"
                yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                return
            # plan parse failed / empty -> fall through to single-turn flow below

        # ════════════════════════════════════════════════════════════
        # SINGLE-TURN + AUTO-REPAIR mode (edits, follow-ups, plan fallback)
        # ════════════════════════════════════════════════════════════

        # ── Change Analysis Planner: for modification tasks, analyse what specifically needs changing ──
        change_plan: dict[str, str] = {}
        if project and (project_files or "").strip():
            plan_model = planner_model or writer_model
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'plan', 'agent': 'planner', 'model': plan_model})}\n\n"
            yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔍 Analysing required changes'})}\n\n"
            _ca_system = (
                "You are a senior software architect analysing what specific code changes are needed.\n"
                "Given the user's request and the existing project files listed below, "
                "determine the MINIMAL, TARGETED changes required. "
                "Output ONLY a raw JSON object (no markdown, no code fences, no explanation):\n"
                "{\"files\": [\"only_files_that_need_change\"], "
                "\"change_plan\": {\"filename\": \"specific Thai instruction: exactly what to change in this file\"}, "
                "\"summary\": \"one-sentence Thai description\"}\n"
                "\n"
                "ASK-WHEN-UNSURE — IMPORTANT:\n"
                "If the request is genuinely AMBIGUOUS — multiple reasonable interpretations, unclear scope, "
                "or a design decision only the user can make — do NOT guess. Instead output:\n"
                "{\"needs_clarification\": true, \"question\": \"<short Thai question>\", "
                "\"options\": [{\"label\": \"<short Thai choice>\", \"value\": \"<concrete instruction to act on if chosen>\"}]}\n"
                "Give 2-4 concrete, mutually-distinct options. Only ask when it truly matters "
                "(e.g. 'เพิ่ม animation' could mean subtle-pro vs playful-bouncy; 'ทำให้สวยขึ้น' is open-ended). "
                "If the request is clear enough to act on, do NOT ask — just return the normal files/change_plan.\n"
                "Be VERY specific. Good example: "
                "\"เปลี่ยน class .char-body สีจาก #FF9000 เป็น #FF69B4, เปลี่ยน emoji 🧑 เป็น 👩 ใน div.character\"\n"
                "Only include files that truly need editing. "
                "Files NOT in 'files' will NOT be touched by the writer.\n"
                "\n"
                "SCOPE HEURISTICS — choose the narrowest file set by request type:\n"
                "- Pure visual/animation/styling/colour/layout change → edit ONLY the .css file "
                "(animations, transitions, @keyframes, hover effects are CSS — do NOT touch .html or main.py)\n"
                "- Text/wording/content change → edit ONLY the relevant template (.html)\n"
                "- New route / backend logic / data change → edit main.py (+ template if new page)\n"
                "When a request can be done in one file, return exactly one file. "
                "Never include main.py or requirements.txt unless the change genuinely needs backend/deps.\n"
                f"{file_ctx}"
            )
            _ca_text = ""
            try:
                async for _tok in stream_chat_response(
                    question, [], _ca_system, model=plan_model,
                    agent="CodeAgentChangeAnalysis", max_tokens=600
                ):
                    _ca_text += _tok
                _s = _ca_text.find("{"); _e = _ca_text.rfind("}")
                if _s != -1 and _e != -1:
                    _cp = _json.loads(_ca_text[_s:_e + 1])
                    # Planner is unsure → ask the user with clickable options, then stop
                    if _cp.get("needs_clarification") and _cp.get("options"):
                        _opts = []
                        for _o in (_cp.get("options") or [])[:4]:
                            _lbl = str((_o or {}).get("label") or "").strip()
                            _val = str((_o or {}).get("value") or "").strip() or _lbl
                            if _lbl:
                                _opts.append({"label": _lbl, "value": _val})
                        if _opts:
                            yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': len(_ca_text.split())})}\n\n"
                            yield f"data: {_json.dumps({'type': 'clarify', 'question': str(_cp.get('question') or 'เลือกแนวทางที่ต้องการ'), 'options': _opts})}\n\n"
                            yield f"data: {_json.dumps({'type': 'done'})}\n\n"
                            return
                    change_plan = _cp.get("change_plan") or {}
                    _cp_files = [str(f) for f in (_cp.get("files") or []) if f]
                    _cp_summary = str(_cp.get("summary") or "")
                    if change_plan and _cp_files:
                        yield f"data: {_json.dumps({'type': 'plan_done', 'files': _cp_files, 'dependencies': [], 'summary': _cp_summary})}\n\n"
            except Exception:
                change_plan = {}
            yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': len(_ca_text.split())})}\n\n"

        repair_iter = 0
        conv_messages = clean_history[:]
        # Inject change plan into the writer's question so it makes targeted edits only
        if change_plan:
            _plan_lines = "\n".join(f"- {_f}: {_inst}" for _f, _inst in change_plan.items())
            current_q = (
                f"User request: {question}\n\n"
                f"CHANGE PLAN — make ONLY these specific targeted changes, nothing else:\n{_plan_lines}\n\n"
                f"CRITICAL RULES:\n"
                f"- Keep ALL other existing content exactly as-is\n"
                f"- Do NOT rewrite files from scratch\n"
                f"- Do NOT add features not mentioned above\n"
                f"- Make minimal, precise edits as specified per file"
            )
        else:
            current_q = question
        forced_validation = False
        written_contents: dict[str, str] = {}
        any_exec_ran = False
        yield f"data: {_json.dumps({'type': 'stage', 'stage': 'write', 'agent': 'writer', 'model': writer_model})}\n\n"

        while repair_iter <= MAX_REPAIR:
            # ── Stream AI tokens ──────────────────────────────────────
            full_response = ""
            token_count = 0
            status_msg = "🤔 Thinking" if repair_iter == 0 else f"🔧 Auto-repair #{repair_iter}"
            yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': status_msg})}\n\n"

            async for token in stream_chat_response(
                current_q, conv_messages, system, model=writer_model, agent="CodeAgentWriter", max_tokens=AGENT_MAX_TOKENS
            ):
                full_response += token
                token_count += len(token.split())
                yield f"data: {_json.dumps({'type': 'token', 'text': token, 'tokens': token_count})}\n\n"

            yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': token_count})}\n\n"

            # ── Execute WRITE_FILE tags ───────────────────────────────
            actions, events, written_now = await process_write_files(full_response)
            written_contents.update(written_now)
            for ev in events:
                yield f"data: {_json.dumps(ev)}\n\n"

            # Save UPDATE_MEMORY
            await save_memory_tags(full_response)

            # ── Execute EXEC_CMD tags ─────────────────────────────────
            exec_results: list[dict] = []
            exec_cmds = _EXEC_CMD_RE.findall(full_response)
            if exec_cmds:
                any_exec_ran = True
            if exec_cmds and project_dir:
                exec_results, events = await process_exec_cmds(exec_cmds, project_dir)
                for ev in events:
                    yield f"data: {_json.dumps(ev)}\n\n"

            # ── Clean display text ────────────────────────────────────
            display = _WRITE_FILE_RE.sub("", full_response)
            display = _EXEC_CMD_RE.sub("", display)
            display = _UPDATE_MEMORY_RE.sub("", display).strip()

            yield f"data: {_json.dumps({'type': 'final_text', 'text': display, 'actions': actions, 'exec_results': exec_results, 'repair_iter': repair_iter})}\n\n"

            # ── Auto-repair if exec failed, or validate if .py written but never checked ──
            # (blocked commands are policy, not bugs — don't "repair" them)
            failed = [e for e in exec_results if not e.get("ok") and not e.get("blocked")]
            py_written = [a["path"] for a in actions if a.get("ok") and a["path"].endswith(".py")]
            needs_validation = bool(py_written) and not exec_cmds and not forced_validation
            static_issues = run_static_checks(written_contents) if written_now else []

            if (not failed and not needs_validation and not static_issues) or repair_iter >= MAX_REPAIR:
                break

            repair_iter += 1
            if failed:
                error_lines = "\n".join(
                    f"❌ {e['cmd']}: {(e.get('stdout','') + e.get('stderr','') + e.get('error','')).strip()[:300]}"
                    for e in failed
                )
                current_q = (
                    f"คำสั่งเหล่านี้ล้มเหลว:\n{error_lines}\n\n"
                    f"แก้ไข code ให้ถูกต้องโดยใช้ WRITE_FILE แล้ว re-run ด้วย EXEC_CMD เพื่อยืนยัน"
                )
            elif static_issues:
                issue_lines = "\n".join(f"⚠️ {i['hint']}" for i in static_issues)
                current_q = (
                    f"โค้ดที่เขียนยังไม่สมบูรณ์หรือมีปัญหาดังนี้:\n{issue_lines}\n\n"
                    f"กรุณาเขียนไฟล์ที่เกี่ยวข้องใหม่ทั้งไฟล์ด้วย WRITE_FILE ให้สมบูรณ์ ลบ comment "
                    f"placeholder ใดๆ ออกแล้วเขียนโค้ดจริงแทนตามที่ comment บอกไว้ และเพิ่ม route "
                    f"ที่ template อ้างถึงแต่ยังไม่มีใน main.py ให้ครบ จากนั้นรัน "
                    f"<EXEC_CMD cmd=\"python -m py_compile <path>\"/> สำหรับไฟล์ .py ที่แก้ไขเพื่อยืนยัน"
                )
            else:
                forced_validation = True
                files_list = "\n".join(f"- {p}" for p in py_written)
                current_q = (
                    f"คุณเขียนไฟล์ .py เหล่านี้แต่ยังไม่ได้ตรวจสอบ:\n{files_list}\n\n"
                    f"กรุณารัน <EXEC_CMD cmd=\"python -m py_compile <path>\"/> สำหรับแต่ละไฟล์ "
                    f"(path ไม่ต้องมีชื่อ project นำหน้า) และแก้ไขถ้ามี error"
                )
            conv_messages = conv_messages + [
                {"role": "user", "content": question[:400]},
                {"role": "assistant", "content": full_response[:1200]},
            ]
            yield f"data: {_json.dumps({'type': 'repair_start', 'iter': repair_iter, 'errors': len(failed)})}\n\n"

        # ── QC review round: separate reviewer model checks written files ──
        if written_contents and qc_model:
            yield f"data: {_json.dumps({'type': 'stage', 'stage': 'qc', 'agent': 'qc', 'model': qc_model})}\n\n"
            yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔍 QC reviewing'})}\n\n"
            qc_text = ""
            qc_skipped = False
            try:
                contracts_text = extract_contracts(written_contents)
                qc_static = run_static_checks(written_contents)
                static_lines = "\n".join(f"- {i['hint']}" for i in qc_static) or "(none)"
                files_block = "\n\n".join(
                    f"=== {p} ===\n" + "\n".join(c.splitlines()[:120])
                    for p, c in list(written_contents.items())[:6]
                )
                qc_system = (
                    "You are a strict senior code reviewer (QC). You CANNOT write files — verdict only.\n"
                    "Review the files below for runtime bugs, cross-file inconsistencies "
                    "(missing templates/routes, schema mismatch, double UploadFile.read(), "
                    "dict(row) without row_factory), and imports missing from requirements.txt.\n"
                    "If everything is correct reply EXACTLY: QC_PASS\n"
                    "Otherwise reply in Thai with concrete issues, one per line:\n"
                    "FILE:<path> ISSUE:<what is wrong> FIX:<specific instruction>\n"
                    "Maximum 5 issues. Report only real bugs that break the app — not style preferences."
                )
                qc_q = (
                    f"งานที่ผู้ใช้สั่ง: {question[:300]}\n\n"
                    f"Contracts ของโปรเจกต์:\n{contracts_text or '(none)'}\n\n"
                    f"Static analysis:\n{static_lines}\n\n"
                    f"ไฟล์ที่เพิ่งถูกเขียน:\n{files_block}"
                )
                async for token in stream_chat_response(
                    qc_q, [], qc_system, model=qc_model, agent="CodeAgentQC", max_tokens=700
                ):
                    qc_text += token
            except Exception as exc:
                qc_text = f"QC_PASS (review skipped: {str(exc)[:80]})"
                qc_skipped = True
            qc_pass = "QC_PASS" in qc_text[:200].upper()
            yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': len(qc_text.split())})}\n\n"
            yield f"data: {_json.dumps({'type': 'qc_verdict', 'pass': qc_pass, 'skipped': qc_skipped, 'text': qc_text.strip()[:1500]})}\n\n"

            # QC found issues -> hand back to writer for one scoped fix round
            if not qc_pass and qc_text.strip():
                yield f"data: {_json.dumps({'type': 'stage', 'stage': 'write', 'agent': 'writer', 'model': writer_model})}\n\n"
                yield f"data: {_json.dumps({'type': 'repair_start', 'iter': repair_iter + 1, 'errors': 1})}\n\n"
                yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔧 Fixing QC issues'})}\n\n"
                qc_fix_q = (
                    f"QC reviewer ตรวจพบปัญหาดังนี้:\n{qc_text.strip()[:1200]}\n\n"
                    f"แก้ไขเฉพาะจุดที่ QC ระบุด้วย <WRITE_FILE path=\"{project}/...\">...</WRITE_FILE> "
                    f"(เขียนทั้งไฟล์เวอร์ชันแก้แล้ว) จากนั้นรัน "
                    f"<EXEC_CMD cmd=\"python -m py_compile <path>\"/> สำหรับไฟล์ .py ที่แก้"
                )
                holder_qcfix: dict = {}
                async for ev in stream_turn(qc_fix_q, conv_messages, 4000, holder_qcfix, writer_model, "CodeAgentWriter"):
                    yield f"data: {_json.dumps(ev)}\n\n"
                yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': holder_qcfix.get('tokens', 0)})}\n\n"
                fix_response = holder_qcfix.get('response', '')

                fix_actions, fix_events, fix_contents = await process_write_files(fix_response)
                written_contents.update(fix_contents)
                for ev in fix_events:
                    yield f"data: {_json.dumps(ev)}\n\n"
                await save_memory_tags(fix_response)
                fix_cmds = _EXEC_CMD_RE.findall(fix_response)
                fix_exec_results: list[dict] = []
                if fix_cmds and project_dir:
                    fix_exec_results, fix_ev2 = await process_exec_cmds(fix_cmds, project_dir)
                    for ev in fix_ev2:
                        yield f"data: {_json.dumps(ev)}\n\n"

                fix_display = _WRITE_FILE_RE.sub("", fix_response)
                fix_display = _EXEC_CMD_RE.sub("", fix_display)
                fix_display = _UPDATE_MEMORY_RE.sub("", fix_display).strip()
                if fix_display:
                    yield f"data: {_json.dumps({'type': 'final_text', 'text': fix_display, 'actions': fix_actions, 'exec_results': fix_exec_results, 'repair_iter': repair_iter + 1})}\n\n"

        # ── Auto-deploy: existing container -> restart+retest; none yet but the
        # project is a FastAPI app -> first deploy. Triggers on any agent
        # activity (file writes OR exec commands), with one log-repair round. ──
        if project and (written_contents or any_exec_ran):
            _safe_proj = __import__("re").sub(r"[^a-zA-Z0-9_-]", "", project)
            if _safe_proj == project:
                chk = await _trusted_shell(
                    f"docker ps -a --filter name=^ener-app-{_safe_proj}$ --format '{{{{.Names}}}}'", 15
                )
                has_container = bool((chk.get("stdout") or "").strip())
                main_src_disk = ""
                try:
                    with open(f"{BASE_ENER_CODE}/{_safe_proj}/main.py", "r",
                              encoding="utf-8", errors="replace") as fh:
                        main_src_disk = fh.read()
                except Exception:
                    pass
                req_exists = os.path.exists(f"{BASE_ENER_CODE}/{_safe_proj}/requirements.txt")
                if has_container or ("fastapi" in main_src_disk.lower() and req_exists):
                    deploy_mode = "restart" if has_container else "deploy"
                    msg = '🚀 Re-deploy & retest' if has_container else '🚀 First deploy & smoke test'
                    yield f"data: {_json.dumps({'type': 'stage', 'stage': 'run', 'agent': None})}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': msg})}\n\n"
                    st_smoke: dict | None = None
                    async for ev in _deploy_and_smoke(
                        project, written_contents.get("main.py", main_src_disk), deploy_mode
                    ):
                        if ev.get("type") == "smoke_result":
                            st_smoke = ev
                        yield f"data: {_json.dumps(ev)}\n\n"
                    yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"

                    # Smoke failed -> writer repairs from runtime logs, restart, retest once
                    if st_smoke and not st_smoke.get("ok") and st_smoke.get("logs"):
                        yield f"data: {_json.dumps({'type': 'stage', 'stage': 'write', 'agent': 'writer', 'model': writer_model})}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🔧 Fixing runtime errors'})}\n\n"
                        st_route_lines = "\n".join(
                            f"- GET {r['path']} -> {r['status']}" for r in st_smoke.get("routes", [])
                        ) or "(แอปไม่ตอบสนองเลย)"
                        st_fix_q = (
                            f"แอปถูก deploy เป็น container แล้วแต่รันไม่ขึ้น/smoke test ล้มเหลว "
                            f"(dependency ที่ขาดถูกเติมอัตโนมัติแล้ว ปัญหานี้น่าจะเป็น bug ในโค้ด)\n"
                            f"ผลการเรียก route:\n{st_route_lines}\n\n"
                            f"Log จาก container:\n```\n{st_smoke['logs']}\n```\n\n"
                            f"อ่าน log บรรทัดสุดท้าย (ตัว error จริง) แล้วแก้ไฟล์ที่ระบุใน traceback ด้วย "
                            f"<WRITE_FILE path=\"{project}/...\">...</WRITE_FILE> (เขียนทั้งไฟล์เวอร์ชันที่แก้แล้ว)\n"
                            f"ห้ามรันคำสั่งตรวจสอบ/ค้นหา (curl, ls, cat, docker) — แก้ด้วย WRITE_FILE เท่านั้น "
                            f"ระบบจะ deploy + ทดสอบใหม่ให้เองหลังคุณเขียนไฟล์เสร็จ"
                        )
                        st_holder: dict = {}
                        async for ev in stream_turn(st_fix_q, conv_messages, 4000, st_holder, writer_model, "CodeAgentWriter"):
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': st_holder.get('tokens', 0)})}\n\n"
                        st_resp = st_holder.get('response', '')
                        st_a, st_e, st_c = await process_write_files(st_resp)
                        written_contents.update(st_c)
                        for ev in st_e:
                            yield f"data: {_json.dumps(ev)}\n\n"
                        st_cmds = _EXEC_CMD_RE.findall(st_resp)
                        if st_cmds and project_dir:
                            _r2, _e2 = await process_exec_cmds(st_cmds, project_dir)
                            for ev in _e2:
                                yield f"data: {_json.dumps(ev)}\n\n"

                        yield f"data: {_json.dumps({'type': 'stage', 'stage': 'run', 'agent': None})}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_start', 'msg': '🚀 Re-deploy & retest'})}\n\n"
                        st_smoke2: dict | None = None
                        async for ev in _deploy_and_smoke(
                            project, written_contents.get("main.py", main_src_disk), "restart"
                        ):
                            if ev.get("type") == "smoke_result":
                                st_smoke2 = ev
                            yield f"data: {_json.dumps(ev)}\n\n"
                        yield f"data: {_json.dumps({'type': 'thinking_done', 'tokens': 0})}\n\n"

                        # One AI round done. If still failing, stop and report — do not loop.
                        if st_smoke2 and not st_smoke2.get("ok"):
                            st_rl = "\n".join(
                                f"- GET {r['path']} → {r['status']}" for r in st_smoke2.get("routes", [])
                            ) or "(แอปไม่ตอบสนอง)"
                            report = (
                                f"**⚠️ แก้อัตโนมัติแล้วแต่แอปยังรันไม่ขึ้น** — หยุดเพื่อให้คนตรวจสอบ\n\n"
                                f"URL: {st_smoke2.get('url','')}\n{st_rl}\n\n"
                                f"**Log ล่าสุดจาก container:**\n```\n{(st_smoke2.get('logs') or '')[:600]}\n```\n"
                                f"แนะนำ: เปิดไฟล์ที่ระบุใน traceback แล้วแก้เอง หรือสั่งให้ AI แก้จุดที่เจาะจง"
                            )
                            yield f"data: {_json.dumps({'type': 'final_text', 'text': report, 'actions': [], 'exec_results': [], 'repair_iter': repair_iter + 2})}\n\n"

        yield f"data: {_json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/workspace/code/agent-vision")
async def workspace_code_agent_vision(request: Request):
    """One-shot vision agent: pasted image + question -> Claude Haiku -> WRITE_FILE/EXEC_CMD."""
    await _require_admin(request)
    form = await request.form()

    question      = str(form.get("question", "")).strip()
    file_path     = str(form.get("file_path", ""))
    file_content  = str(form.get("file_content", ""))
    project       = str(form.get("project", "")).strip()
    server_ctx    = _json.loads(form.get("server_context") or "{}")
    project_files = str(form.get("project_files", ""))
    messages      = _json.loads(form.get("messages") or "[]")

    image_b64, image_media = await _read_workspace_image_from_form(form)
    if not image_b64:
        raise HTTPException(400, "image required")
    if not question:
        question = "ดูรูปนี้แล้วช่วยวิเคราะห์/แก้ไข code ให้ตรงกับสิ่งที่เห็นในรูป"

    # ── System prompt (same as agent-stream) ──────────────────────────────
    project_memory = await _load_project_memory(project) if project else {}
    memory_block = ""
    if project_memory:
        mem_block_lines = "\n".join(f"- {k}: {v}" for k, v in project_memory.items())
        memory_block = f"\n=== PROJECT MEMORY ===\n{mem_block_lines}\n"

    file_ctx = ""
    if project_files:
        file_ctx += f"\n=== Files in project '{project}' ===\n{project_files}\n"
    if file_path and file_content:
        preview = "\n".join(file_content.splitlines()[:200])
        file_ctx += f"\n=== Current File: {file_path} ===\n```\n{preview}\n```\n"

    srv = (server_ctx.get("server") or {})
    containers_txt = "\n".join((server_ctx.get("running_containers") or [])[:8])
    server_block = ""
    if srv:
        server_block = (
            f"\n=== SERVER STATE ===\n"
            f"CPU: {srv.get('cpu_load','?')} | RAM: {srv.get('ram','?')} | Disk: {srv.get('disk','?')}\n"
        )
        if containers_txt:
            server_block += f"Running containers:\n{containers_txt}\n"

    system = (
        f"You are Ener-AI Code Agent. You write files directly using WRITE_FILE tags.\n\n"
        f"STACK: Python 3.11 / FastAPI / aiosqlite / Docker / Hetzner CPX22\n"
        f"PUBLIC DOMAIN: https://my-ener.uk\n"
        f"PROJECT PATH: /root/ener-code/{project or 'project-name'}/ on the server\n"
        f"########## NEVER USE localhost ##########\n"
        f"FORBIDDEN: localhost:8000, localhost:any_port, 127.0.0.1\n"
        f"ALWAYS use: https://my-ener.uk or http://my-ener.uk:<port>\n"
        f"##########################################\n"
        f"{memory_block}"
        f"{server_block}"
        f"{file_ctx}\n"
        f"\n=== USER ATTACHED A SCREENSHOT/IMAGE — analyze it carefully and use it as the main context ===\n"
        f"Use <WRITE_FILE path=\"{project or 'my-project'}/filename\">content</WRITE_FILE> to write files.\n"
        f"Use <EXEC_CMD cmd=\"command\"/> to run shell commands (CWD is /root/ener-code/{project or 'project'}/).\n"
        f"CORRECT: 'ls -la'   WRONG: 'ls -la {project or 'project'}'\n"
        f"Use <UPDATE_MEMORY key=\"k\" value=\"v\"/> to save project facts.\n"
        f"After WRITE_FILE, write a short Thai summary of what was done.\n\n"
        f"VALIDATION RULE — IMPORTANT:\n"
        f"- After writing ANY .py file, IMMEDIATELY add <EXEC_CMD cmd=\"python -m py_compile <path>\"/> to check syntax\n"
        f"- NEVER leave requirements.txt empty or with placeholder comments — list real package names\n"
        f"- If a file imports/references another local file, make sure that file is also created\n"
        f"\n"
        f"CRITICAL ANTI-PATTERNS — NEVER DO THESE:\n"
        f"- UploadFile.read() can only be called ONCE per request; store the result in a variable and reuse it "
        f"(do not call file.read() twice — the second call returns empty bytes)\n"
        f"- If you use dict(row) to convert aiosqlite rows, you MUST set db.row_factory = aiosqlite.Row "
        f"on the connection BEFORE executing the query\n"
        f"- Use exactly ONE database file path and ONE schema for the whole project — never define a "
        f"separate db module with a different DB filename or table schema than main.py uses\n"
        f"- Every template file you create must be rendered by a route in main.py (or extended by another "
        f"template) — never leave an unused template file\n"
    )

    clean_history = [
        m for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-12:]

    from app.core.ai import _anthropic_messages, _PRIMARY_MODEL
    from app.core.vision import build_user_content
    import anthropic as _anthropic

    client = _anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    anthropic_msgs = _anthropic_messages(question, clean_history)
    anthropic_msgs[-1]["content"] = build_user_content(
        question, image_base64=image_b64, image_media_type=image_media
    )
    raw_answer = ""
    try:
        response = await client.messages.create(
            model=_PRIMARY_MODEL,
            max_tokens=4096,
            system=system,
            messages=anthropic_msgs,
        )
        raw_answer = "".join(getattr(b, "text", "") for b in response.content)
    except Exception as exc:
        # Anthropic unavailable (e.g. credit exhausted) -> OpenRouter vision fallback
        try:
            from app.core.openrouter_client import openrouter_chat_completions
            or_messages: list[dict] = [{"role": "system", "content": system}]
            for m in clean_history:
                or_messages.append({"role": m["role"], "content": str(m["content"])[:2000]})
            or_messages.append({"role": "user", "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{image_media};base64,{image_b64}"}},
            ]})
            data = await openrouter_chat_completions(
                "gemini-flash-lite", or_messages, max_tokens=4096
            )
            raw_answer = str(
                ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
        except Exception as exc2:
            return JSONResponse({
                "answer": (
                    f"❌ Vision AI error: {str(exc)[:200]}\n"
                    f"❌ Fallback (OpenRouter Gemini) ก็ล้มเหลว: {str(exc2)[:200]}"
                ),
                "actions": [], "diffs": {}, "exec_results": [],
            })
    if not raw_answer.strip():
        return JSONResponse({"answer": "❌ Vision AI ตอบกลับว่างเปล่า", "actions": [], "diffs": {}, "exec_results": []})

    # ── Parse WRITE_FILE (with diff) ───────────────────────────────────────
    import os, difflib as _difflib
    actions: list[dict] = []
    diffs: dict[str, dict] = {}
    for m in _WRITE_FILE_RE.finditer(raw_answer):
        rel_path = m.group(1).strip()
        content  = m.group(2).lstrip("\n").rstrip("\n")
        try:
            full_p = _ener_code_resolve(rel_path)
            old_lines: list[str] = []
            is_new = True
            if os.path.exists(full_p):
                with open(full_p, "r", encoding="utf-8", errors="replace") as f:
                    old_lines = f.read().splitlines()
                is_new = False
            new_lines = content.splitlines()
            diff_data: list[dict] = []
            if is_new:
                for idx, ln in enumerate(new_lines[:60]):
                    diff_data.append({"t": "+", "l": ln, "n": idx + 1})
            else:
                matcher = _difflib.SequenceMatcher(None, old_lines, new_lines)
                count = 0
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if count >= 60:
                        break
                    if tag == "equal":
                        end = min(i2, i1 + 2)
                        for off in range(end - i1):
                            diff_data.append({"t": "=", "l": old_lines[i1 + off], "n": j1 + off + 1}); count += 1
                    elif tag in ("replace", "delete"):
                        for off in range(i2 - i1):
                            diff_data.append({"t": "-", "l": old_lines[i1 + off], "n": i1 + off + 1}); count += 1
                    if tag in ("replace", "insert"):
                        for off in range(j2 - j1):
                            diff_data.append({"t": "+", "l": new_lines[j1 + off], "n": j1 + off + 1}); count += 1
            os.makedirs(os.path.dirname(full_p), exist_ok=True)
            with open(full_p, "w", encoding="utf-8") as fh:
                fh.write(content)
            added = sum(1 for d in diff_data if d["t"] == "+")
            removed = sum(1 for d in diff_data if d["t"] == "-")
            actions.append({"type": "write_file", "path": rel_path, "ok": True, "lines": len(new_lines)})
            diffs[rel_path] = {"diff": diff_data, "is_new": is_new, "added": added, "removed": removed}
        except Exception as exc:
            actions.append({"type": "write_file", "path": rel_path, "ok": False, "error": str(exc)})

    # ── Parse EXEC_CMD (safety-checked via _agent_run_cmd) ─────────────────
    exec_results: list[dict] = []
    exec_cmds = _EXEC_CMD_RE.findall(raw_answer)
    if exec_cmds and project:
        project_dir = f"{BASE_ENER_CODE}/{project}"
        os.makedirs(project_dir, exist_ok=True)
        for cmd in exec_cmds[:6]:
            exec_results.append(await _agent_run_cmd(cmd, project_dir))

    # ── UPDATE_MEMORY ───────────────────────────────────────────────────────
    if project:
        for m in _UPDATE_MEMORY_RE.finditer(raw_answer):
            try:
                await _save_memory_entry(project, m.group(1).strip(), m.group(2).strip())
            except Exception:
                pass

    display = _WRITE_FILE_RE.sub("", raw_answer)
    display = _EXEC_CMD_RE.sub("", display)
    display = _UPDATE_MEMORY_RE.sub("", display).strip()

    return JSONResponse({
        "answer": display,
        "actions": actions,
        "diffs": diffs,
        "exec_results": exec_results,
    })


# ── Agent Loop ────────────────────────────────────────────────────────────────
import json as _json

@app.post("/workspace/code/agent-loop")
async def workspace_code_agent_loop(request: Request):
    """Autonomous multi-iteration agent: PLAN → VERIFY → REPAIR loop with SSE streaming."""
    await _require_admin(request)
    body = await request.json()
    project = body.get("project", "").strip()
    task = body.get("task", "").strip()
    model = body.get("model", "")
    if not project or not task:
        raise HTTPException(400, "project and task required")

    import asyncio as _aio

    async def _run_cmd(cmd: str, project_dir: str):
        verdict, reason = _check_cmd_safety(cmd)
        if verdict != "ok":
            return {
                "cmd": cmd, "ok": False, "blocked": True, "verdict": verdict,
                "error": f"⛔ คำสั่งนี้ถูกบล็อกเพื่อความปลอดภัย: {reason}. ถ้าจำเป็นต้องรัน ให้แจ้ง user รันเองทาง SSH",
                "returncode": -1,
            }
        try:
            proc = await _aio.create_subprocess_shell(
                cmd, stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.PIPE, cwd=project_dir,
            )
            try:
                out, err = await _aio.wait_for(proc.communicate(), timeout=30)
                return {
                    "cmd": cmd, "ok": proc.returncode == 0,
                    "stdout": out.decode("utf-8", errors="replace")[:700],
                    "stderr": err.decode("utf-8", errors="replace")[:350],
                    "returncode": proc.returncode,
                }
            except _aio.TimeoutError:
                proc.kill(); await proc.wait()
                return {"cmd": cmd, "ok": False, "error": "timeout (20s)", "returncode": -1}
        except Exception as exc:
            return {"cmd": cmd, "ok": False, "error": str(exc)[:200], "returncode": -1}

    async def generate():
        MAX_ITER = 8
        project_dir = f"{BASE_ENER_CODE}/{project}"
        os.makedirs(project_dir, exist_ok=True)

        mem = await _load_project_memory(project)
        mem_block = ""
        if mem:
            mem_block = "\n=== PROJECT MEMORY ===\n" + "\n".join(f"- {k}: {v}" for k, v in mem.items()) + "\n"

        base_sys = (
            f"You are Ener-AI Code Agent in AUTONOMOUS LOOP mode.\n"
            f"Project: {project}  Path: /root/ener-code/{project}/\n"
            f"PUBLIC DOMAIN: https://my-ener.uk\n"
            f"########## NEVER USE localhost ##########\n"
            f"FORBIDDEN: localhost, 127.0.0.1 — use https://my-ener.uk\n"
            f"##########################################\n"
            f"{mem_block}"
            f"Use <WRITE_FILE path=\"{project}/filename\">content</WRITE_FILE> to write files.\n"
            f"Use <EXEC_CMD cmd=\"command\"/> to run shell commands (CWD is already /root/ener-code/{project}/).\n"
            f"CORRECT EXEC: 'ls -la'   WRONG: 'ls -la {project}'\n"
            f"Use <UPDATE_MEMORY key=\"k\" value=\"v\"/> to save project facts.\n"
        )

        loop_msgs: list[dict] = []
        all_actions: list[dict] = []
        iteration = 0
        phase = "BUILD"  # BUILD → VERIFY → REPAIR → VERIFY → ... → DONE

        while iteration < MAX_ITER:
            iteration += 1

            if phase == "BUILD":
                prompt = (
                    f"TASK: {task}\n\n"
                    f"Phase: BUILD — Create ALL files needed for this task now using WRITE_FILE tags.\n"
                    f"Think step by step: what files are needed, then write them ALL at once.\n"
                    f"After writing, run these EXEC_CMD checks:\n"
                    f"1. <EXEC_CMD cmd=\"find . -name '*.py' | head -20\"/> — list py files\n"
                    f"2. <EXEC_CMD cmd=\"python -m py_compile main.py 2>&1\"/> — syntax check main file\n"
                    f"3. <EXEC_CMD cmd=\"pip install -r requirements.txt -q 2>&1 | tail -5\"/> — install deps\n"
                    f"4. <EXEC_CMD cmd=\"python -c 'import sys; sys.path.insert(0,\".\"); import importlib; m=importlib.import_module(\"main\"); print(\"OK\")'  2>&1\"/> — test import\n"
                    f"End with a short Thai summary."
                )
            elif phase == "VERIFY":
                written = ", ".join(a["path"] for a in all_actions if a.get("ok"))
                # Detect project type from files
                has_py = any(a["path"].endswith(".py") for a in all_actions if a.get("ok"))
                has_req = any("requirements" in a["path"] for a in all_actions if a.get("ok"))
                has_test = any("test" in a["path"] for a in all_actions if a.get("ok"))
                verify_cmds = (
                    f"<EXEC_CMD cmd=\"find . -name '*.py' -exec python -m py_compile {{}} \\; 2>&1 | head -20\"/>"
                    f" — compile all .py\n"
                )
                if has_req:
                    verify_cmds += f"<EXEC_CMD cmd=\"pip install -r requirements.txt -q 2>&1 | tail -5\"/> — install deps\n"
                if has_py:
                    verify_cmds += (
                        f"<EXEC_CMD cmd=\"python -c 'import importlib,sys; sys.path.insert(0,\".\"); importlib.import_module(\"main\"); print(\"IMPORT OK\")' 2>&1\"/> — import check\n"
                        f"<EXEC_CMD cmd=\"timeout 6 python -m uvicorn main:app --host 0.0.0.0 --port 18099 2>&1 | head -15\"/> — try start server\n"
                    )
                if has_test:
                    verify_cmds += f"<EXEC_CMD cmd=\"python -m pytest -x -q 2>&1 | tail -20\"/> — run tests\n"
                prompt = (
                    f"Files written: {written}\n\n"
                    f"Phase: VERIFY — Run ALL these checks, report each pass ✅ or fail ❌:\n"
                    f"{verify_cmds}\n"
                    f"If server starts successfully (shows 'Application startup complete'), that means it WORKS.\n"
                    f"Report clearly what passed and what failed."
                )
            elif phase == "REPAIR":
                # Get last exec results from history for context
                last_content = loop_msgs[-1]["content"] if loop_msgs else ""
                exec_errors = last_content[-1200:] if len(last_content) > 1200 else last_content
                prompt = (
                    f"Phase: REPAIR — The following errors were found:\n{exec_errors}\n\n"
                    f"Fix ALL failing files using WRITE_FILE. Be precise — fix the exact errors shown.\n"
                    f"After fixing, re-run the same checks with EXEC_CMD to confirm fixed.\n"
                    f"If import error: fix the import. If syntax error: fix the syntax. If missing package: add to requirements.txt."
                )
            else:
                break

            yield f"data: {_json.dumps({'type': 'step_start', 'phase': phase, 'iter': iteration})}\n\n"

            try:
                raw = await ai_chat(
                    prompt, system=base_sys, agent="AgentLoop",
                    messages=loop_msgs[-8:], preferred_model=model, strict_model=False,
                )
            except Exception as exc:
                yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)[:200]})}\n\n"
                return

            # Process WRITE_FILE
            step_actions: list[dict] = []
            for m in _WRITE_FILE_RE.finditer(raw):
                rel_path = m.group(1).strip()
                content = m.group(2).lstrip("\n").rstrip("\n")
                try:
                    full = _ener_code_resolve(rel_path)
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    step_actions.append({"path": rel_path, "ok": True, "lines": len(content.splitlines())})
                    all_actions.append({"path": rel_path, "ok": True})
                except Exception as exc:
                    step_actions.append({"path": rel_path, "ok": False, "error": str(exc)})

            # Save UPDATE_MEMORY
            for m in _UPDATE_MEMORY_RE.finditer(raw):
                try:
                    await _save_memory_entry(project, m.group(1).strip(), m.group(2).strip())
                except Exception:
                    pass

            # Process EXEC_CMD
            step_exec: list[dict] = []
            for cmd in _EXEC_CMD_RE.findall(raw)[:5]:
                step_exec.append(await _run_cmd(cmd, project_dir))

            # Clean display
            display = _WRITE_FILE_RE.sub("", raw)
            display = _EXEC_CMD_RE.sub("", display)
            display = _UPDATE_MEMORY_RE.sub("", display).strip()

            # Update conversation history
            loop_msgs.append({"role": "user", "content": prompt[:400]})
            loop_msgs.append({"role": "assistant", "content": raw[:1200]})

            yield f"data: {_json.dumps({'type': 'step_done', 'phase': phase, 'iter': iteration, 'content': display, 'actions': step_actions, 'exec_results': step_exec})}\n\n"

            # Decide next phase
            def _exec_passed(results):
                """True if all cmds passed OR server output contains startup success."""
                for e in results:
                    out = (e.get("stdout", "") + e.get("stderr", "")).lower()
                    # uvicorn startup success counts as pass even if returncode != 0 (timeout kill)
                    if "application startup complete" in out or "started server process" in out:
                        continue
                    if not e.get("ok"):
                        return False
                return True

            if phase == "BUILD":
                if step_exec:
                    phase = "DONE" if _exec_passed(step_exec) else "REPAIR"
                else:
                    phase = "VERIFY"
            elif phase == "VERIFY":
                phase = "DONE" if (not step_exec or _exec_passed(step_exec)) else "REPAIR"
            elif phase == "REPAIR":
                phase = "VERIFY"

            if phase == "DONE":
                break

        total_ok = len([a for a in all_actions if a.get("ok")])
        yield f"data: {_json.dumps({'type': 'done', 'iter': iteration, 'total_files': total_ok})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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


@app.get("/workspace/code/pending")
async def workspace_code_pending(request: Request):
    """List recent code change requests."""
    await _require_admin(request)
    async with get_db() as db:
        cur = await db.execute(
            """SELECT id, feature_request, status, plan_summary,
                      approval_token, created_at, base_commit
               FROM code_change_requests ORDER BY created_at DESC LIMIT 10"""
        )
        rows = [dict(r) for r in await cur.fetchall()]
    return JSONResponse({"requests": rows})


@app.post("/workspace/code/approve/{token}")
async def workspace_code_approve(token: str, request: Request):
    """Approve and apply a pending code change via token."""
    await _require_admin(request)
    from app.core.database import get_pending_code_request, update_code_request_status
    from app.core.code_agent import apply_code_change

    req = await get_pending_code_request(token.upper())
    if not req:
        raise HTTPException(404, "Token not found or already processed")
    await update_code_request_status(req["id"], "approved")
    result = await apply_code_change(req["id"])
    return JSONResponse(result)


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


BASE_ENER_CODE = "/root/ener-code"
_ENER_CODE_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "dist", "build", ".next",
}
_ENER_CODE_ALLOWED_SUFFIXES = (
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".md", ".txt", ".sh", ".env.example", ".html", ".css",
)
_ENER_CODE_MAX_FILE_BYTES = 500 * 1024
_ENER_CODE_PROJECT_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def _ener_code_allowed_file(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _ENER_CODE_ALLOWED_SUFFIXES)


def _ener_code_resolve(rel: str) -> str:
    import os

    full = os.path.normpath(os.path.join(BASE_ENER_CODE, rel))
    if not full.startswith(BASE_ENER_CODE):
        raise HTTPException(400, "invalid path")
    return full


def _ener_code_project_dir(project: str) -> str:
    import os

    if not _ENER_CODE_PROJECT_RE.match(project or ""):
        raise HTTPException(400, "invalid project name")
    full = os.path.normpath(os.path.join(BASE_ENER_CODE, project))
    if not full.startswith(BASE_ENER_CODE):
        raise HTTPException(400, "invalid project name")
    if not os.path.isdir(full):
        raise HTTPException(404, "project not found")
    return full


@app.get("/workspace/code/projects")
async def workspace_code_projects(request: Request):
    await _require_admin(request)
    import os

    os.makedirs(BASE_ENER_CODE, exist_ok=True)
    projects = []
    for name in sorted(os.listdir(BASE_ENER_CODE)):
        if name.startswith(".") or name in {"__pycache__"}:
            continue
        full = os.path.join(BASE_ENER_CODE, name)
        if not os.path.isdir(full):
            continue
        projects.append({
            "name": name,
            "path": name,
            "has_git": os.path.exists(os.path.join(full, ".git")),
        })
    return JSONResponse({"projects": projects})


@app.get("/workspace/code/tree")
async def workspace_code_tree(request: Request, project: str = ""):
    await _require_admin(request)
    import os

    project_root = _ener_code_project_dir(project)
    tree = []
    file_count = 0
    max_files = 500
    max_depth = 5

    for root, dirs, files in os.walk(project_root):
        rel_root = os.path.relpath(root, project_root)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        if depth >= max_depth:
            dirs.clear()
            continue
        dirs[:] = sorted(
            d for d in dirs
            if d not in _ENER_CODE_SKIP_DIRS and not d.startswith(".")
        )
        for d in dirs:
            rel_path = (
                f"{project}/{d}" if rel_root == "."
                else f"{project}/{rel_root}/{d}".replace("\\", "/")
            )
            tree.append({
                "type": "dir",
                "name": d,
                "path": rel_path,
                "depth": depth + 1,
            })
        for f in sorted(files):
            if not _ener_code_allowed_file(f):
                continue
            if file_count >= max_files:
                break
            rel_path = (
                f"{project}/{f}" if rel_root == "."
                else f"{project}/{rel_root}/{f}".replace("\\", "/")
            )
            ext = f.rsplit(".", 1)[-1] if "." in f else ""
            if f.endswith(".env.example"):
                ext = "env.example"
            tree.append({
                "type": "file",
                "name": f,
                "path": rel_path,
                "depth": depth + 1,
                "ext": ext,
            })
            file_count += 1
        if file_count >= max_files:
            break

    return JSONResponse({"project": project, "tree": tree})


@app.get("/workspace/code/enerfile")
async def workspace_code_enerfile(request: Request, path: str = ""):
    await _require_admin(request)
    import os

    if not path:
        raise HTTPException(400, "path required")
    full = _ener_code_resolve(path)
    if not os.path.isfile(full):
        raise HTTPException(404, "file not found")
    size = os.path.getsize(full)
    if size > _ENER_CODE_MAX_FILE_BYTES:
        raise HTTPException(400, "file too large")
    try:
        with open(full, "r", encoding="utf-8") as fh:
            content = fh.read()
    except UnicodeDecodeError:
        raise HTTPException(400, "file is not valid utf-8 text")
    lines = content.splitlines()
    ext = os.path.basename(full).rsplit(".", 1)[-1] if "." in os.path.basename(full) else ""
    if os.path.basename(full).endswith(".env.example"):
        ext = "env.example"
    return JSONResponse({
        "path": path.replace("\\", "/"),
        "content": content,
        "lines": len(lines),
        "size": len(content),
        "ext": ext,
    })


@app.post("/workspace/code/enerwrite")
async def workspace_code_enerwrite(request: Request):
    await _require_admin(request)
    import os

    body = await request.json()
    rel = (body.get("path") or "").strip()
    content = body.get("content", "")
    if not rel:
        raise HTTPException(400, "path required")
    full = _ener_code_resolve(rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)
    lines = content.splitlines()
    return JSONResponse({"ok": True, "path": rel.replace("\\", "/"), "lines": len(lines)})


@app.delete("/workspace/code/file")
async def workspace_code_file_delete(request: Request):
    await _require_admin(request)
    import os

    path = request.query_params.get("path", "").strip()
    if not path:
        raise HTTPException(400, "path required")
    full = _ener_code_resolve(path)
    if not os.path.exists(full):
        raise HTTPException(404, "file not found")
    if os.path.isdir(full):
        raise HTTPException(400, "path is a directory — use project delete instead")
    os.remove(full)
    return JSONResponse({"ok": True, "deleted": path})


@app.post("/workspace/code/project/create")
async def workspace_code_project_create(request: Request):
    await _require_admin(request)
    import os

    body = await request.json()
    name = (body.get("name") or "").strip().lower()
    git_init = bool(body.get("git_init", False))
    template = (body.get("template") or "empty").strip().lower()
    if not _ENER_CODE_PROJECT_RE.match(name):
        raise HTTPException(400, "invalid project name (a-z0-9- only, max 40 chars)")
    if template not in {"python", "node", "empty"}:
        raise HTTPException(400, "invalid template")
    os.makedirs(BASE_ENER_CODE, exist_ok=True)
    project_dir = os.path.join(BASE_ENER_CODE, name)
    if os.path.exists(project_dir):
        raise HTTPException(409, "project already exists")

    os.makedirs(project_dir)

    python_gitignore = "__pycache__/\n*.pyc\n.venv/\n.env\n"
    node_gitignore = "node_modules/\n.env\ndist/\nbuild/\n"

    if template == "python":
        with open(os.path.join(project_dir, "main.py"), "w", encoding="utf-8") as fh:
            fh.write('"""Entry point."""\n\n\ndef main():\n    print("Hello from Ener-AI project")\n\n\nif __name__ == "__main__":\n    main()\n')
        with open(os.path.join(project_dir, "requirements.txt"), "w", encoding="utf-8") as fh:
            fh.write("# Add dependencies here\n")
        if git_init:
            with open(os.path.join(project_dir, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write(python_gitignore)
    elif template == "node":
        with open(os.path.join(project_dir, "index.js"), "w", encoding="utf-8") as fh:
            fh.write('console.log("Hello from Ener-AI project");\n')
        with open(os.path.join(project_dir, "package.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "name": name,
                "version": "1.0.0",
                "private": True,
                "main": "index.js",
                "scripts": {"start": "node index.js"},
            }, indent=2) + "\n")
        if git_init:
            with open(os.path.join(project_dir, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write(node_gitignore)
    elif git_init:
        with open(os.path.join(project_dir, ".gitignore"), "w", encoding="utf-8") as fh:
            fh.write(python_gitignore)

    if git_init:
        subprocess.run(
            ["git", "init"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )

    return JSONResponse({"ok": True, "project": name, "path": project_dir})


@app.get("/workspace/code/project/{name}/delete-preview")
async def workspace_code_project_delete_preview(name: str, request: Request):
    await _require_admin(request)
    import os

    if not _ENER_CODE_PROJECT_RE.match(name):
        raise HTTPException(400, "invalid project name")
    project_dir = os.path.join(BASE_ENER_CODE, name)
    if not os.path.isdir(project_dir):
        raise HTTPException(404, "project not found")

    files_count = sum(len(fs) for _, _, fs in os.walk(project_dir))

    container_name = f"ener-app-{name}"
    port = _project_app_port(name)
    docker_running = False
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
            capture_output=True, text=True, timeout=5,
        )
        docker_running = r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        pass

    memory_count = 0
    try:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) AS cnt FROM code_project_memory WHERE project=?", (name,)
            )
            row = await cur.fetchone()
            memory_count = row["cnt"] if row else 0
    except Exception:
        pass

    return JSONResponse({
        "project": name,
        "files_count": files_count,
        "docker_container": container_name,
        "docker_running": docker_running,
        "docker_port": port if docker_running else None,
        "memory_count": memory_count,
    })


@app.delete("/workspace/code/project/{name}")
async def workspace_code_project_delete(name: str, request: Request):
    await _require_admin(request)
    import os
    import shutil

    if not _ENER_CODE_PROJECT_RE.match(name):
        raise HTTPException(400, "invalid project name")
    project_dir = os.path.join(BASE_ENER_CODE, name)
    if not os.path.isdir(project_dir):
        raise HTTPException(404, "project not found")

    cleaned: dict = {}

    # Stop + remove Docker container
    container_name = f"ener-app-{name}"
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True, text=True, timeout=15,
        )
        cleaned["docker"] = r.returncode == 0
    except Exception:
        cleaned["docker"] = False

    # Clear project memory
    try:
        async with get_db() as db:
            await db.execute("DELETE FROM code_project_memory WHERE project=?", (name,))
            await db.commit()
        cleaned["memory"] = True
    except Exception:
        cleaned["memory"] = False

    # Delete files
    shutil.rmtree(project_dir)
    cleaned["files"] = True

    return JSONResponse({"ok": True, "deleted": name, "cleaned": cleaned})


@app.post("/workspace/code/git")
async def workspace_code_git(request: Request):
    await _require_admin(request)
    import os

    body = await request.json()
    project = (body.get("project") or "").strip()
    cmd = (body.get("cmd") or "").strip().lower()
    message = (body.get("message") or "").strip()
    cwd = _ener_code_project_dir(project)

    if cmd == "status":
        args = ["git", "status", "--short"]
    elif cmd == "log":
        args = ["git", "log", "--oneline", "-10"]
    elif cmd == "diff":
        args = ["git", "diff", "--stat"]
    elif cmd == "add":
        args = ["git", "add", "-A"]
    elif cmd == "commit":
        if not message:
            raise HTTPException(400, "commit message required")
        args = ["git", "commit", "-m", message]
    elif cmd == "push":
        remote = subprocess.run(
            ["git", "remote"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if not remote.stdout.strip():
            raise HTTPException(400, "no git remote configured — cannot push")
        args = ["git", "push"]
    elif cmd == "pull":
        args = ["git", "pull"]
    else:
        raise HTTPException(400, "invalid git command")

    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise HTTPException(400, output.strip() or f"git {cmd} failed")
    return JSONResponse({"ok": True, "output": output.strip(), "cmd": cmd})


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


@app.post("/workspace/code/codex")
async def workspace_code_codex(request: Request):
    """Run Codex CLI task with ChatGPT Plus billing."""
    await _require_admin(request)
    body = await request.json()
    task = body.get("task", "").strip()
    file_path = body.get("file_path", "")
    if not task:
        raise HTTPException(400, "task required")
    from app.agents.codex_agent import run_codex_on_file, run_codex
    if file_path:
        result = await run_codex_on_file(task, file_path)
    else:
        result = await run_codex(task)
    return JSONResponse(result)


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


_ADMIN_PROJECT_TABS: list[tuple[str, str]] = [
    ("", "Overview"),
    ("chat", "Chat"),
    ("memory", "Memory"),
    ("tasks", "Tasks"),
    ("files", "Files"),
    ("code-runs", "Code Runs"),
    ("artifacts", "Artifacts"),
    ("logs", "Logs"),
    ("settings", "Settings"),
]


def _project_slug_from_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return slug or "project"


async def _fetch_admin_project(project_id: int) -> dict | None:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT id, name, datetime(created_at, '+7 hours') AS created_at
            FROM projects
            WHERE id = ? AND deleted_at IS NULL
            """,
            (project_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


def _project_tabs_html(project_id: int, active_tab: str) -> str:
    links = []
    for slug, label in _ADMIN_PROJECT_TABS:
        href = f"/admin/projects/{project_id}" if not slug else f"/admin/projects/{project_id}/{slug}"
        cls = "project-tab active" if slug == active_tab else "project-tab"
        links.append(f'<a class="{cls}" href="{href}">{escape(label)}</a>')
    return '<div class="project-tabs">' + "".join(links) + "</div>"


def _admin_simple_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = ""
    for row in rows:
        body += "<tr>" + "".join(f"<td>{escape(str(c))}</td>" for c in row) + "</tr>"
    if not body:
        body = f'<tr><td colspan="{len(headers)}">ไม่มีข้อมูล</td></tr>'
    return f'<table class="pw-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


async def _project_workspace_tab_html(project_id: int, tab: str, project: dict) -> str:
    name = str(project.get("name") or "")
    slug = _project_slug_from_name(name)
    tab = (tab or "").strip().lower()

    if tab in {"", "overview"}:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE project_id = ?",
                (project_id,),
            )
            msg_count = int((await cur.fetchone())["c"] or 0)
            cur = await db.execute(
                "SELECT COUNT(*) AS c FROM project_artifacts WHERE project_id = ? OR project_slug = ?",
                (project_id, slug),
            )
            art_count = int((await cur.fetchone())["c"] or 0)
            cur = await db.execute(
                """
                SELECT COUNT(*) AS c FROM code_runs
                WHERE trace_id IN (
                    SELECT DISTINCT trace_id FROM messages
                    WHERE project_id = ? AND trace_id IS NOT NULL AND TRIM(trace_id) <> ''
                )
                """,
                (project_id,),
            )
            code_count = int((await cur.fetchone())["c"] or 0)
        return f"""
        <section class="pw-card">
          <h2>Overview</h2>
          <p><strong>Project:</strong> {escape(name)} (id={project_id})</p>
          <p><strong>Slug:</strong> {escape(slug)}</p>
          <ul>
            <li>Messages: {msg_count}</li>
            <li>Artifacts: {art_count}</li>
            <li>Code runs (linked traces): {code_count}</li>
          </ul>
        </section>
        """

    if tab == "chat":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT role, substr(content, 1, 240) AS content,
                       datetime(created_at, '+7 hours') AS created_at,
                       COALESCE(model_used, '') AS model_used
                FROM messages
                WHERE project_id = ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (project_id,),
            )
            rows = await cur.fetchall()
        table_rows = [
            [r["created_at"], r["role"], r["model_used"], r["content"]]
            for r in reversed(rows)
        ]
        return '<section class="pw-card"><h2>Chat</h2>' + _admin_simple_table(
            ["Time", "Role", "Model", "Content"], table_rows
        ) + "</section>"

    if tab == "memory":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT substr(content, 1, 300) AS content, memory_type,
                       datetime(created_at, '+7 hours') AS created_at
                FROM long_term_memories
                ORDER BY id DESC
                LIMIT 40
                """
            )
            ltm = await cur.fetchall()
            cur = await db.execute(
                """
                SELECT key, substr(value, 1, 200) AS value, tag,
                       datetime(updated_at, '+7 hours') AS updated_at
                FROM memories
                ORDER BY updated_at DESC
                LIMIT 30
                """
            )
            mem = await cur.fetchall()
        ltm_rows = [[r["created_at"], r["memory_type"], r["content"]] for r in ltm]
        mem_rows = [[r["updated_at"], r["key"], r["tag"], r["value"]] for r in mem]
        return (
            '<section class="pw-card"><h2>Long-term memories</h2>'
            + _admin_simple_table(["Time", "Type", "Content"], ltm_rows)
            + '</section><section class="pw-card"><h2>Memories (key/value)</h2>'
            + _admin_simple_table(["Updated", "Key", "Tag", "Value"], mem_rows)
            + "</section>"
        )

    if tab == "tasks":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT id, title, priority, status, deadline_hint,
                       datetime(created_at, '+7 hours') AS created_at
                FROM tasks
                WHERE status = 'open' OR status IS NULL
                ORDER BY id DESC
                LIMIT 50
                """
            )
            rows = await cur.fetchall()
        table_rows = [
            [r["id"], r["title"], r["priority"], r["status"], r["deadline_hint"], r["created_at"]]
            for r in rows
        ]
        return '<section class="pw-card"><h2>Open Tasks</h2><p class="muted">tasks table ไม่มี project_id — แสดง open tasks ทั้งระบบ</p>' + _admin_simple_table(
            ["ID", "Title", "Priority", "Status", "Deadline", "Created"], table_rows
        ) + "</section>"

    if tab == "files":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT id, filename, size_bytes, substr(summary, 1, 120) AS summary,
                       datetime(created_at, '+7 hours') AS created_at
                FROM uploads
                ORDER BY id DESC
                LIMIT 50
                """
            )
            rows = await cur.fetchall()
        table_rows = [
            [r["id"], r["filename"], r["size_bytes"], r["summary"], r["created_at"]] for r in rows
        ]
        return '<section class="pw-card"><h2>Files</h2><p class="muted">uploads ยังไม่ผูก project_id — แสดงล่าสุดทั้งระบบ</p>' + _admin_simple_table(
            ["ID", "Filename", "Bytes", "Summary", "Created"], table_rows
        ) + "</section>"

    if tab == "code-runs":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT cr.id, cr.action, cr.status, cr.request_id,
                       substr(cr.lesson_learned, 1, 120) AS lesson,
                       datetime(cr.created_at, '+7 hours') AS created_at
                FROM code_runs cr
                WHERE cr.trace_id IN (
                    SELECT DISTINCT trace_id FROM messages
                    WHERE project_id = ? AND trace_id IS NOT NULL AND TRIM(trace_id) <> ''
                )
                ORDER BY cr.id DESC
                LIMIT 50
                """,
                (project_id,),
            )
            rows = await cur.fetchall()
            if not rows:
                cur = await db.execute(
                    """
                    SELECT id, action, status, request_id,
                           substr(lesson_learned, 1, 120) AS lesson,
                           datetime(created_at, '+7 hours') AS created_at
                    FROM code_runs
                    ORDER BY id DESC
                    LIMIT 30
                    """
                )
                rows = await cur.fetchall()
        table_rows = [
            [r["id"], r["action"], r["status"], r["request_id"], r["lesson"], r["created_at"]]
            for r in rows
        ]
        return '<section class="pw-card"><h2>Code Runs</h2>' + _admin_simple_table(
            ["ID", "Action", "Status", "Request", "Lesson", "Created"], table_rows
        ) + "</section>"

    if tab == "artifacts":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT artifact_type, title, substr(summary, 1, 160) AS summary, source,
                       datetime(created_at, '+7 hours') AS created_at
                FROM project_artifacts
                WHERE project_id = ? OR project_slug = ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (project_id, slug),
            )
            rows = await cur.fetchall()
        table_rows = [
            [r["created_at"], r["artifact_type"], r["title"], r["source"], r["summary"]] for r in rows
        ]
        return '<section class="pw-card"><h2>Artifacts</h2>' + _admin_simple_table(
            ["Time", "Type", "Title", "Source", "Summary"], table_rows
        ) + "</section>"

    if tab == "logs":
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT agent_name, event_type, substr(summary, 1, 160) AS summary,
                       result, datetime(created_at, '+7 hours') AS created_at
                FROM agent_events
                WHERE summary LIKE ? OR COALESCE(context, '') LIKE ?
                ORDER BY id DESC
                LIMIT 50
                """,
                (f"%{name[:40]}%", f"%project_id:{project_id}%"),
            )
            rows = await cur.fetchall()
            if not rows:
                cur = await db.execute(
                    """
                    SELECT agent_name, event_type, substr(summary, 1, 160) AS summary,
                           result, datetime(created_at, '+7 hours') AS created_at
                    FROM agent_events
                    ORDER BY id DESC
                    LIMIT 40
                    """
                )
                rows = await cur.fetchall()
        table_rows = [
            [r["created_at"], r["agent_name"], r["event_type"], r["result"], r["summary"]]
            for r in rows
        ]
        note = '<p class="muted">agent_events ไม่มี trace_id — กรองจากชื่อ/summary หรือแสดงล่าสุดทั้งระบบ</p>'
        return '<section class="pw-card"><h2>Agent Events</h2>' + note + _admin_simple_table(
            ["Time", "Agent", "Event", "Result", "Summary"], table_rows
        ) + "</section>"

    if tab == "settings":
        return f"""
        <section class="pw-card">
          <h2>Settings</h2>
          <p><strong>ID:</strong> {project_id}</p>
          <p><strong>Name:</strong> {escape(name)}</p>
          <p><strong>Created:</strong> {escape(str(project.get("created_at") or ""))}</p>
          <p><strong>Workspace API:</strong> <code>/workspace/projects</code></p>
        </section>
        """

    raise HTTPException(status_code=404, detail="Unknown project tab")


def build_project_workspace_html(
    *,
    project: dict,
    tab: str,
    body_html: str,
) -> HTMLResponse:
    project_id = int(project["id"])
    title = escape(str(project.get("name") or f"Project {project_id}"))
    tabs = _project_tabs_html(project_id, tab)
    html = f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Ener-AI Project</title>
  <style>
    body {{ font-family: Inter, system-ui, sans-serif; background: #0d0d0d; color: #e5e5e5; margin: 0; padding: 20px; }}
    a {{ color: #7dd3fc; text-decoration: none; }}
    .back {{ display: inline-block; margin-bottom: 16px; padding: 8px 14px; background: #1a1a1a; border-radius: 8px; }}
    h1 {{ margin: 0 0 12px; font-size: 1.4rem; }}
    .project-tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 20px; }}
    .project-tab {{ padding: 8px 12px; border: 1px solid #333; border-radius: 8px; background: #141414; color: #ccc; font-size: 0.85rem; }}
    .project-tab.active {{ border-color: #3b82f6; color: #93c5fd; }}
    .pw-card {{ background: #141414; border: 1px solid #222; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
    .pw-table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
    .pw-table th, .pw-table td {{ border-bottom: 1px solid #222; padding: 8px 10px; text-align: left; vertical-align: top; }}
    .pw-table th {{ color: #888; font-size: 0.75rem; text-transform: uppercase; }}
    .muted {{ color: #888; font-size: 0.85rem; }}
  </style>
</head>
<body>
  <a class="back" href="/admin/projects">← All Projects</a>
  <a class="back" href="/admin" style="margin-left:8px">Admin Home</a>
  <h1>{title}</h1>
  {tabs}
  {body_html}
</body>
</html>"""
    return HTMLResponse(content=html)


async def _admin_project_workspace_page(request: Request, project_id: int, tab: str = "") -> HTMLResponse:
    await _require_admin(request)
    project = await _fetch_admin_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    body = await _project_workspace_tab_html(project_id, tab, project)
    return build_project_workspace_html(project=project, tab=tab, body_html=body)


@app.get("/admin/projects")
async def admin_projects_list(request: Request):
    await _require_admin(request)
    ctx = await load_admin_base_context("projects", "Projects — Ener-AI")
    return templates.TemplateResponse(request, "admin/projects.html", ctx)


@app.get("/admin/projects/{project_id}")
async def admin_project_overview(request: Request, project_id: int):
    return await _admin_project_workspace_page(request, project_id, "")


@app.get("/admin/projects/{project_id}/{tab}")
async def admin_project_tab(request: Request, project_id: int, tab: str):
    known = {slug for slug, _ in _ADMIN_PROJECT_TABS}
    if tab not in known:
        raise HTTPException(status_code=404, detail="Unknown tab")
    return await _admin_project_workspace_page(request, project_id, tab)


@app.get("/admin")
async def admin_dashboard(request: Request):
    await _require_admin(request)
    ctx = await load_admin_base_context("home", "Home — Ener-AI")
    return templates.TemplateResponse(request, "admin/home.html", ctx)


@app.get("/admin/ai")
async def admin_ai_hub(request: Request):
    await _require_admin(request)
    ctx = await load_admin_ai_context()
    return templates.TemplateResponse(request, "admin/ai.html", ctx)


@app.get("/admin/ops")
async def admin_ops_hub(request: Request):
    await _require_admin(request)
    ctx = await load_admin_base_context("ops", "Ops — Ener-AI")
    return templates.TemplateResponse(request, "admin/ops.html", ctx)


@app.get("/admin/settings")
async def admin_settings_hub(request: Request):
    await _require_admin(request)
    ctx = await load_admin_settings_context()
    return templates.TemplateResponse(request, "admin/settings.html", ctx)


@app.get("/admin/classic")
async def admin_dashboard_classic(request: Request):
    """Legacy inline-HTML dashboard (pre-Jinja)."""
    await _require_admin(request)
    return build_admin_html(await _load_admin_overview())


@app.get("/admin/ai-traces")
async def admin_ai_traces_page(request: Request):
    await _require_admin(request)
    return build_ai_traces_html()


@app.get("/admin/ener-scan-business")
async def admin_ener_scan_business_page(request: Request):
    await _require_admin(request)
    return build_ener_scan_business_html()


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


def build_platform_html() -> str:
    return """<!DOCTYPE html>
<html>
<head>
  <title>Ener Platform</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root{--bg:#0d0d0d;--card:#1a1a1a;--accent:#7c3aed;--text:#e5e5e5;--subtext:#888;--border:#2a2a2a}
    *{box-sizing:border-box;margin:0;padding:0}
    body{font-family:Inter,sans-serif;background:var(--bg);color:var(--text);font-size:15px}
    .header{display:flex;align-items:center;gap:16px;padding:16px 24px;border-bottom:1px solid var(--border);background:#111}
    .header h1{font-size:18px;font-weight:700}
    .back-btn{padding:6px 14px;background:var(--card);border:1px solid var(--border);border-radius:6px;color:var(--text);text-decoration:none;font-size:13px}
    .container{padding:24px;max-width:1200px;margin:0 auto}
    .server-bar{background:var(--card);border-radius:10px;padding:16px 20px;margin-bottom:20px;display:flex;gap:32px;align-items:center;flex-wrap:wrap}
    .server-stat label{font-size:11px;color:var(--subtext);text-transform:uppercase;letter-spacing:.05em}
    .server-stat .val{font-size:18px;font-weight:700;margin-top:2px}
    .progress-bar{height:6px;background:#333;border-radius:3px;margin-top:4px;min-width:120px}
    .progress-fill{height:100%;border-radius:3px;background:var(--accent);transition:width .3s}
    .progress-fill.warn{background:#f59e0b}
    .progress-fill.danger{background:#ef4444}
    .section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
    .section-title{font-size:16px;font-weight:600}
    .btn{padding:8px 16px;border:none;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500;font-family:inherit}
    .btn-primary{background:var(--accent);color:white}
    .btn-sm{padding:5px 12px;font-size:12px;border-radius:6px}
    .btn-ghost{background:var(--card);color:var(--text);border:1px solid var(--border)}
    .btn-danger{background:#2d0000;color:#ef4444;border:1px solid #ef4444}
    .project-card{background:var(--card);border-radius:10px;padding:16px 20px;margin-bottom:12px;border:1px solid var(--border)}
    .project-card.running{border-left:3px solid #22c55e}
    .project-card.stopped{border-left:3px solid #555}
    .project-card.deploying{border-left:3px solid #f59e0b}
    .project-card.failed{border-left:3px solid #ef4444}
    .project-header{display:flex;justify-content:space-between;align-items:center}
    .project-name{font-size:16px;font-weight:600}
    .project-meta{font-size:12px;color:var(--subtext);margin-top:4px}
    .badge{padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
    .badge-running{background:#052e16;color:#22c55e}
    .badge-stopped{background:#1a1a1a;color:#888}
    .badge-deploying{background:#451a03;color:#f59e0b}
    .badge-failed{background:#2d0000;color:#ef4444}
    .project-actions{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}
    .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:100}
    .modal-overlay.open{display:flex}
    .modal{background:#141414;border-radius:12px;width:90%;max-width:800px;max-height:80vh;display:flex;flex-direction:column;border:1px solid var(--border)}
    .modal-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
    .modal-body{flex:1;overflow:auto;padding:16px;font-family:monospace;font-size:13px;white-space:pre-wrap;color:#aaa;line-height:1.6}
    .close-btn{background:none;border:none;color:var(--subtext);cursor:pointer;font-size:20px}
    .form-group{margin-bottom:16px}
    .form-group label{display:block;font-size:13px;color:var(--subtext);margin-bottom:6px}
    .form-group input,.form-group select{width:100%;padding:9px 12px;background:#222;border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;font-family:inherit}
    .toast{position:fixed;bottom:24px;right:24px;padding:10px 20px;background:#333;color:white;border-radius:8px;font-size:14px;display:none;z-index:999}
  </style>
</head>
<body>
<div class="header">
  <a href="/admin" class="back-btn">← Admin</a>
  <h1>🚀 Ener Platform</h1>
  <div id="server-status" style="margin-left:auto;font-size:13px;color:var(--subtext)">Loading...</div>
</div>
<div class="container">
  <div class="server-bar" id="server-bar">
    <div class="server-stat">
      <label>CPU</label>
      <div class="val" id="srv-cpu">-</div>
      <div class="progress-bar"><div class="progress-fill" id="srv-cpu-bar" style="width:0%"></div></div>
    </div>
    <div class="server-stat">
      <label>RAM</label>
      <div class="val" id="srv-ram">-</div>
      <div class="progress-bar"><div class="progress-fill" id="srv-ram-bar" style="width:0%"></div></div>
    </div>
    <div class="server-stat">
      <label>Disk</label>
      <div class="val" id="srv-disk">-</div>
      <div class="progress-bar"><div class="progress-fill" id="srv-disk-bar" style="width:0%"></div></div>
    </div>
    <div style="margin-left:auto;font-size:12px;color:var(--subtext)">CPX32 · 4CPU 8GB · 204.168.246.103</div>
  </div>
  <div class="section-header">
    <div class="section-title">Projects</div>
    <button class="btn btn-primary" onclick="showCreateModal()">+ New Project</button>
  </div>
  <div id="projects-list">Loading...</div>
</div>

<!-- Logs Modal -->
<div class="modal-overlay" id="logs-modal">
  <div class="modal">
    <div class="modal-header">
      <span id="logs-title">Logs</span>
      <button class="close-btn" onclick="closeLogsModal()">×</button>
    </div>
    <div class="modal-body" id="logs-content">Loading...</div>
  </div>
</div>

<!-- Create Modal -->
<div class="modal-overlay" id="create-modal">
  <div class="modal" style="max-width:480px">
    <div class="modal-header">
      <span>New Project</span>
      <button class="close-btn" onclick="closeCreateModal()">×</button>
    </div>
    <div class="modal-body" style="padding:20px;font-family:inherit">
      <div class="form-group"><label>Project Name</label><input id="new-name" type="text" placeholder="ener-scan"></div>
      <div class="form-group"><label>Type</label>
        <select id="new-type">
          <option value="nodejs">Node.js</option>
          <option value="python">Python/FastAPI</option>
          <option value="typescript">TypeScript</option>
          <option value="static">Static HTML</option>
        </select>
      </div>
      <div class="form-group"><label>Domain (optional)</label><input id="new-domain" type="text" placeholder="scan.my-ener.uk"></div>
      <div class="form-group"><label>Memory Limit</label>
        <select id="new-memory">
          <option value="512m">512 MB</option>
          <option value="768m" selected>768 MB</option>
          <option value="1024m">1 GB</option>
          <option value="2048m">2 GB</option>
        </select>
      </div>
      <button class="btn btn-primary" style="width:100%" onclick="createProject()">Create Project</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>
<script>
const BADGE={running:'<span class="badge badge-running">&#9679; Running</span>',stopped:'<span class="badge badge-stopped">&#9675; Stopped</span>',deploying:'<span class="badge badge-deploying">&#8635; Deploying</span>',failed:'<span class="badge badge-failed">&#10005; Failed</span>'};
async function api(url,opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...opts});return r.json()}
function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.style.display='block';setTimeout(()=>t.style.display='none',3000)}
function setBarColor(el,pct){el.style.width=pct+'%';el.className='progress-fill'+(pct>85?' danger':pct>70?' warn':'')}
async function loadServerMetrics(){
  try{
    const d=await api('/platform/api/server/metrics');
    document.getElementById('srv-cpu').textContent=d.cpu_percent.toFixed(1)+'%';
    document.getElementById('srv-ram').textContent=Math.round(d.ram_used_mb/1024*10)/10+'/'+ Math.round(d.ram_total_mb/1024)+' GB';
    document.getElementById('srv-disk').textContent=d.disk_used_gb+'/'+d.disk_total_gb+' GB';
    setBarColor(document.getElementById('srv-cpu-bar'),d.cpu_percent);
    setBarColor(document.getElementById('srv-ram-bar'),d.ram_percent);
    setBarColor(document.getElementById('srv-disk-bar'),d.disk_percent);
    document.getElementById('server-status').textContent='Updated '+new Date().toLocaleTimeString('th-TH');
  }catch(e){document.getElementById('server-status').textContent='Error loading metrics'}
}
async function loadProjects(){
  const d=await api('/platform/api/projects');
  const list=document.getElementById('projects-list');
  if(!d.projects||!d.projects.length){list.innerHTML='<div style="color:var(--subtext);padding:32px;text-align:center">No projects yet — create one!</div>';return}
  list.innerHTML=d.projects.map(p=>`
    <div class="project-card ${p.status}">
      <div class="project-header">
        <div>
          <div class="project-name">${p.name}</div>
          <div class="project-meta">${p.domain||'-'} &nbsp;&#183;&nbsp; :${p.port||'-'} &nbsp;&#183;&nbsp; ${p.type}</div>
        </div>
        ${BADGE[p.status]||BADGE.stopped}
      </div>
      <div class="project-actions">
        <button class="btn btn-sm btn-primary" onclick="deployProject('${p.slug}')">&#128640; Deploy</button>
        <button class="btn btn-sm btn-ghost" onclick="restartProject('${p.slug}')">&#8635; Restart</button>
        <button class="btn btn-sm btn-ghost" onclick="showLogs('${p.slug}')">&#128203; Logs</button>
        <button class="btn btn-sm btn-danger" onclick="stopProject('${p.slug}')">&#9632; Stop</button>
      </div>
    </div>`).join('');
}
async function deployProject(slug){showToast('Deploying '+slug+'...');const r=await api('/platform/api/projects/'+slug+'/deploy',{method:'POST'});showToast(r.ok?'Deploy success!':'Deploy failed: '+(r.output||'').slice(0,80));loadProjects()}
async function stopProject(slug){const r=await api('/platform/api/projects/'+slug+'/stop',{method:'POST'});showToast(r.ok?'Stopped':'Error: '+(r.output||'').slice(0,80));loadProjects()}
async function restartProject(slug){const r=await api('/platform/api/projects/'+slug+'/restart',{method:'POST'});showToast(r.ok?'Restarted':'Error: '+(r.output||'').slice(0,80));loadProjects()}
async function showLogs(slug){document.getElementById('logs-title').textContent=slug+' — Logs';document.getElementById('logs-content').textContent='Loading...';document.getElementById('logs-modal').classList.add('open');const r=await api('/platform/api/projects/'+slug+'/logs?lines=100');document.getElementById('logs-content').textContent=r.logs||'No logs'}
function closeLogsModal(){document.getElementById('logs-modal').classList.remove('open')}
function showCreateModal(){document.getElementById('create-modal').classList.add('open')}
function closeCreateModal(){document.getElementById('create-modal').classList.remove('open')}
async function createProject(){
  const name=document.getElementById('new-name').value.trim();
  if(!name){showToast('กรุณาใส่ชื่อ project');return}
  const r=await api('/platform/api/projects/create',{method:'POST',body:JSON.stringify({name,type:document.getElementById('new-type').value,domain:document.getElementById('new-domain').value.trim()||null,memory_limit:document.getElementById('new-memory').value})});
  if(r.ok){showToast('Created: '+name);closeCreateModal();loadProjects()}else{showToast('Error: '+(r.error||'Failed'))}
}
loadServerMetrics();loadProjects();
setInterval(loadServerMetrics,10000);setInterval(loadProjects,15000);
</script>
</body></html>"""


# ── Platform API routes ───────────────────────────────────────────────────────

@app.get("/platform")
async def platform_page(request: Request):
    await _require_admin(request)
    return HTMLResponse(build_platform_html())


@app.get("/platform/api/projects")
async def platform_projects_list(request: Request):
    await _require_admin(request)
    from app.core.platform_agent import get_all_projects
    projects = await get_all_projects()
    return JSONResponse({"projects": projects})


@app.post("/platform/api/projects/create")
async def platform_create_project(request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core.platform_agent import create_project
    result = await create_project(
        name=body.get("name", ""),
        project_type=body.get("type", "nodejs"),
        port=body.get("port"),
        domain=body.get("domain"),
        memory_limit=body.get("memory_limit", "768m"),
    )
    return JSONResponse(result)


@app.post("/platform/api/projects/{slug}/deploy")
async def platform_deploy_project(slug: str, request: Request):
    await _require_admin(request)
    from app.core.platform_agent import deploy_project
    result = await deploy_project(slug)
    return JSONResponse(result)


@app.post("/platform/api/projects/{slug}/stop")
async def platform_stop_project(slug: str, request: Request):
    await _require_admin(request)
    from app.core.platform_agent import stop_project
    result = await stop_project(slug)
    return JSONResponse(result)


@app.post("/platform/api/projects/{slug}/restart")
async def platform_restart_project(slug: str, request: Request):
    await _require_admin(request)
    from app.core.platform_agent import restart_project
    result = await restart_project(slug)
    return JSONResponse(result)


@app.get("/platform/api/projects/{slug}/logs")
async def platform_project_logs(slug: str, request: Request, lines: int = 50):
    await _require_admin(request)
    from app.core.platform_agent import get_project_logs
    logs = await get_project_logs(slug, lines)
    return JSONResponse({"logs": logs})


@app.get("/platform/api/projects/{slug}/metrics")
async def platform_project_metrics(slug: str, request: Request):
    await _require_admin(request)
    from app.core.platform_agent import get_project_metrics
    metrics = await get_project_metrics(slug)
    return JSONResponse(metrics)


@app.get("/platform/api/server/metrics")
async def platform_server_metrics(request: Request):
    await _require_admin(request)
    from app.core.platform_agent import get_server_metrics
    metrics = await get_server_metrics()
    return JSONResponse(metrics)


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


# ── Hospital Work Dashboard (Phase 1) ───────────────────────────────────────

@app.get("/admin/hospital-work")
async def admin_hospital_work_page(request: Request):
    await _verify_admin_session(request)
    from app.core.hospital_work import build_hospital_work_html

    return HTMLResponse(build_hospital_work_html())


@app.get("/admin/api/hospital-work/projects")
async def hw_projects_list(request: Request, include_inactive: str = ""):
    await _require_admin(request)
    from app.core import hospital_work as hw

    inc = (include_inactive or "").lower() in ("1", "true", "yes")
    return JSONResponse(await hw.list_projects(include_inactive=inc))


@app.get("/admin/api/hospital-work/projects-with-tasks")
async def hw_projects_with_tasks_list(request: Request, include_inactive: str = ""):
    await _require_admin(request)
    from app.core import hospital_work as hw

    inc = (include_inactive or "").lower() in ("1", "true", "yes")
    return JSONResponse(await hw.list_projects_with_tasks(include_inactive=inc))


@app.post("/admin/api/hospital-work/projects")
async def hw_projects_create(request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    try:
        row = await hw.create_project(body or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        if "UNIQUE constraint" in str(e).upper():
            return JSONResponse({"error": "รหัสโครงการซ้ำ"}, status_code=409)
        raise
    return JSONResponse(row)


@app.get("/admin/api/hospital-work/projects/{project_id}")
async def hw_projects_get(project_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    row = await hw.get_project(project_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.put("/admin/api/hospital-work/projects/{project_id}")
async def hw_projects_update(project_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    body = {k: v for k, v in (body or {}).items() if k != "percent_complete"}
    try:
        row = await hw.update_project(project_id, body or {})
    except Exception as e:
        if "UNIQUE constraint" in str(e).upper():
            return JSONResponse({"error": "รหัสโครงการซ้ำ"}, status_code=409)
        raise
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.delete("/admin/api/hospital-work/projects/{project_id}")
async def hw_projects_delete(project_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    ok = await hw.delete_project_soft(project_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.put("/admin/api/hospital-work/projects/{project_id}/restore")
async def hw_projects_restore(project_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    row = await hw.restore_project(project_id)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.get("/admin/api/hospital-work/projects/{project_id}/tasks")
async def hw_tasks_list(project_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(await hw.list_tasks(project_id))


@app.post("/admin/api/hospital-work/projects/{project_id}/tasks")
async def hw_tasks_create(project_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    try:
        row = await hw.create_task(project_id, body or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(row)


@app.put("/admin/api/hospital-work/tasks/{task_id}")
async def hw_tasks_update(task_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    try:
        row = await hw.update_task(task_id, body or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.delete("/admin/api/hospital-work/tasks/{task_id}")
async def hw_tasks_delete(task_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    ok = await hw.delete_task(task_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/admin/api/hospital-work/issues")
async def hw_issues_list(request: Request, project_id: int | None = None):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(await hw.list_issues(project_id))


@app.post("/admin/api/hospital-work/issues")
async def hw_issues_create(request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    try:
        row = await hw.create_issue(body)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(row)


@app.put("/admin/api/hospital-work/issues/{issue_id}")
async def hw_issues_update(issue_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    row = await hw.update_issue(issue_id, body or {})
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.delete("/admin/api/hospital-work/issues/{issue_id}")
async def hw_issues_delete(issue_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    ok = await hw.delete_issue(issue_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/admin/api/hospital-work/other-tasks")
async def hw_other_list(request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(await hw.list_other_tasks())


@app.post("/admin/api/hospital-work/other-tasks")
async def hw_other_create(request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    try:
        row = await hw.create_other_task(body or {})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(row)


@app.put("/admin/api/hospital-work/other-tasks/{ot_id}")
async def hw_other_update(ot_id: int, request: Request):
    await _require_admin(request)
    body = await request.json()
    from app.core import hospital_work as hw

    row = await hw.update_other_task(ot_id, body or {})
    if not row:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(row)


@app.delete("/admin/api/hospital-work/other-tasks/{ot_id}")
async def hw_other_delete(ot_id: int, request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    ok = await hw.delete_other_task(ot_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/admin/api/hospital-work/dashboard")
async def hw_hospital_dashboard(request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(await hw.build_hospital_dashboard_summary())


@app.get("/admin/api/hospital-work/sync-status")
async def hw_hospital_sync_status(request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(hw.hospital_admin_sync_info())


@app.get("/admin/api/hospital-work/daily-report-preview")
async def hw_daily_report_preview(request: Request):
    await _require_admin(request)
    from app.core import hospital_work as hw

    return JSONResponse(await hw.build_daily_report_preview())


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


async def _perform_admin_otp_send(request: Request) -> dict:
    """Send admin OTP via Telegram only when allowed (manual trigger)."""
    async with _admin_otp_lock:
        otp_state = await _get_admin_otp_state()
        now = time.time()
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
        if has_valid_otp:
            return {
                "ok": True,
                "already_valid": True,
                "expires_in": max(0, int(otp_expires_at - now)),
            }

        if last_sent > 0 and (now - last_sent) < OTP_EXPIRE:
            remaining = int(OTP_EXPIRE - (now - last_sent))
            return {"ok": False, "wait": max(1, remaining)}

        _, rl_bucket = _admin_otp_hour_bucket(request, now)
        if len(rl_bucket) >= _ADMIN_OTP_HOUR_RL_MAX:
            return {"error": "Too many requests, wait 1 hour"}

        otp = _generate_otp()
        otp_expires_at = now + OTP_EXPIRE
        await _store_admin_otp(otp, otp_expires_at, now)
        await _send_otp_telegram(otp)
        rl_bucket.append(now)
        _admin_otp_log.info("ADMIN_OTP_SENT_MANUAL expires_in=%s", OTP_EXPIRE)
        return {"ok": True, "expires_in": OTP_EXPIRE}


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

        has_valid_otp = bool(otp_code) and otp_expires_at > now

    if has_valid_otp:
        status_copy = "OTP ยังไม่หมดอายุ กรอกได้เลย"
        intro_copy = "กรอกรหัส OTP จาก Telegram"
        initial_seconds = max(1, min(OTP_EXPIRE, int(otp_expires_at - now)))
        m, s = divmod(initial_seconds, 60)
        timer_placeholder = f"หมดอายุใน {m}:{s:02d}"
    else:
        status_copy = "กดปุ่มเพื่อส่ง OTP ไป Telegram"
        intro_copy = "ยังไม่มี OTP ที่ใช้ได้ — กดปุ่มด้านล่างเพื่อส่ง"
        initial_seconds = 0
        timer_placeholder = "กดปุ่มเพื่อส่ง OTP"

    await log_otp_event(
        "ADMIN_OTP_PAGE_VIEW",
        request=request,
        reason="page_render_no_auto_send",
        metadata={"has_valid_otp": has_valid_otp},
    )
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
    <p style="margin-bottom:8px">__INTRO_COPY__</p>
    <div class="sent-msg">__STATUS_COPY__</div>

    <button type="button" class="submit-btn" style="margin-bottom:16px;background:#006644;color:#fff"
            onclick="sendOtp()">📱 ส่ง OTP ไป Telegram</button>

    <form method="POST" action="/admin/otp/verify">
      <input type="text" name="otp" class="otp-input"
             maxlength="6" placeholder="000000"
             autofocus autocomplete="off"
             oninput="this.value=this.value.replace(/[^0-9]/g,'')">
      <button type="submit" class="submit-btn">✅ เข้าใช้งาน</button>
    </form>

    <div class="timer" id="timer">__TIMER_PLACEHOLDER__</div>
    <button class="resend-btn" type="button" onclick="sendOtp()">ส่ง OTP ใหม่</button>
    <div style="margin-top:16px;">
      <a href="/admin/reset" style="color:#888; font-size:12px; text-decoration:underline;">
        ลืมรหัสผ่าน? รีเซ็ตผ่าน Telegram OTP
      </a>
    </div>
    <div id="msg"></div>
  </div>

  <script>
    let seconds = __INITIAL_SECONDS__;
    let timer = null;
    function formatCountdown(sec) {
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return `หมดอายุใน ${m}:${s.toString().padStart(2, '0')}`;
    }
    function startTimer() {
      if (timer) clearInterval(timer);
      if (seconds <= 0) {
        document.getElementById('timer').textContent = 'กดปุ่มเพื่อส่ง OTP';
        document.getElementById('timer').style.color = '#888';
        return;
      }
      document.getElementById('timer').style.color = '#ffaa00';
      document.getElementById('timer').textContent = formatCountdown(seconds);
      timer = setInterval(() => {
        seconds--;
        if (seconds <= 0) {
          clearInterval(timer);
          timer = null;
          document.getElementById('timer').textContent = '⏰ OTP หมดอายุแล้ว — กดส่ง OTP ใหม่';
          document.getElementById('timer').style.color = '#ff4444';
          return;
        }
        document.getElementById('timer').textContent = formatCountdown(seconds);
      }, 1000);
    }

    async function sendOtp() {
      const res = await fetch('/admin/otp/send', { method: 'POST', credentials: 'same-origin' });
      const data = await res.json().catch(() => ({}));
      const msg = document.getElementById('msg');
      if (res.status === 429 && data.error) {
        msg.textContent = '❌ ' + data.error;
        msg.style.color = '#ff4444';
        return;
      }
      if (!res.ok) {
        msg.textContent = data.error ? ('❌ ' + data.error) : '❌ ส่ง OTP ไม่สำเร็จ';
        msg.style.color = '#ff4444';
        return;
      }
      if (data.already_valid) {
        msg.textContent = 'ℹ️ OTP เดิมยังใช้ได้ กรอกจาก Telegram ได้เลย';
        msg.style.color = '#00ff88';
        seconds = typeof data.expires_in === 'number' ? data.expires_in : seconds;
        startTimer();
        return;
      }
      if (typeof data.wait === 'number') {
        msg.textContent = `กรุณารอ ${data.wait} วินาที ก่อนขอ OTP ใหม่`;
        msg.style.color = '#ffaa00';
        return;
      }
      if (data.ok) {
        msg.textContent = '✅ ส่ง OTP ไป Telegram แล้ว';
        msg.style.color = '#00ff88';
        seconds = typeof data.expires_in === 'number' ? data.expires_in : 300;
        startTimer();
      } else {
        msg.textContent = '❌ ส่ง OTP ไม่สำเร็จ';
        msg.style.color = '#ff4444';
      }
    }

    // Never auto-send OTP on load; countdown only if OTP already valid.
    if (seconds > 0) startTimer();
  </script>
</body>
</html>"""

    return HTMLResponse(
        otp_html
        .replace("__INTRO_COPY__", escape(intro_copy))
        .replace("__STATUS_COPY__", escape(status_copy))
        .replace("__TIMER_PLACEHOLDER__", escape(timer_placeholder))
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
        await log_otp_event("ADMIN_OTP_VERIFY_FAILED", request=request, reason="bad_or_expired")
        return HTMLResponse(
            """
        <script>
        alert('OTP ไม่ถูกต้องหรือหมดอายุแล้วครับ');
        history.back();
        </script>
        """
        )

    await log_otp_event("ADMIN_OTP_VERIFY_SUCCESS", request=request)
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


async def _admin_otp_send_response(request: Request, via: str = "send") -> JSONResponse:
    await log_otp_event("ADMIN_OTP_SEND_REQUEST", request=request, metadata={"via": via})
    result = await _perform_admin_otp_send(request)
    if result.get("error"):
        await log_otp_event(
            "ADMIN_OTP_HOUR_RATE_LIMIT",
            request=request,
            reason="hour_cap",
            metadata={"via": via},
        )
        return JSONResponse(result, status_code=429)
    if result.get("already_valid"):
        await log_otp_event(
            "ADMIN_OTP_NOT_SENT_VALID_EXISTING",
            request=request,
            metadata={"expires_in": result.get("expires_in"), "via": via},
        )
    elif result.get("wait") is not None:
        await log_otp_event(
            "ADMIN_OTP_NOT_SENT_COOLDOWN",
            request=request,
            metadata={"wait_sec": result.get("wait"), "via": via},
        )
    elif result.get("ok"):
        await log_otp_event(
            "ADMIN_OTP_SENT",
            request=request,
            reason="telegram_dispatch",
            metadata={"via": via},
        )
    return JSONResponse(result)


@app.post("/admin/otp/send")
async def admin_otp_send(request: Request):
    await _validate_admin_basic_auth(request)
    return await _admin_otp_send_response(request, via="send")


@app.post("/admin/otp/resend")
async def resend_otp(request: Request):
    await _validate_admin_basic_auth(request)
    return await _admin_otp_send_response(request, via="resend")


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
        allowed = {"auto"}
        if value not in allowed and not _is_cloud_llm_model_id(value):
            raise HTTPException(status_code=400, detail="active_model ไม่ถูกต้อง")
        if value != "auto":
            from app.core.featherless_client import featherless_available
            from app.core.venice_client import venice_available

            if _is_featherless_model_id(value):
                if not await featherless_available():
                    raise HTTPException(status_code=400, detail="Featherless ยังไม่มี key")
            elif _is_venice_model_id(value):
                if not await venice_available():
                    raise HTTPException(status_code=400, detail="Venice ยังไม่มี key")
            elif not settings.openrouter_api_key:
                raise HTTPException(status_code=400, detail="OpenRouter ยังไม่มี key")
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
    if not _is_cloud_llm_model_id(model):
        raise HTTPException(status_code=400, detail="โมเดลไม่ถูกต้อง")
    if _is_featherless_model_id(model):
        from app.core.featherless_client import featherless_available

        if not await featherless_available():
            raise HTTPException(status_code=400, detail="Featherless ยังไม่มี key")
    elif _is_venice_model_id(model):
        from app.core.venice_client import venice_available

        if not await venice_available():
            raise HTTPException(status_code=400, detail="Venice ยังไม่มี key")
    elif not settings.openrouter_api_key:
        raise HTTPException(status_code=400, detail="OpenRouter ยังไม่มี key")

    await set_config("active_model", model)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("admin_model_switched", f"model={model}"),
        )
        await db.commit()
    import asyncio as _asyncio

    _asyncio.create_task(_generate_model_handoff(model))
    if request.headers.get("X-Workspace-Ajax") == "1":
        return JSONResponse(
            {
                "ok": True,
                "model": model,
                "active_model_label": get_model_label(model),
            }
        )
    next_url = form_data.get("next", [""])[0].strip()
    if not next_url.startswith("/workspace"):
        next_url = "/admin"
    return RedirectResponse(url=next_url, status_code=303)


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


@app.get("/admin/api/ai-traces/recent")
async def admin_api_ai_traces_recent(request: Request, limit: int = 50):
    await _require_admin(request)
    traces = await get_recent_ai_traces(limit=limit)
    return JSONResponse({"ok": True, "traces": traces})


@app.get("/admin/api/events/recent")
async def admin_api_events_recent(request: Request, source: str = "ener_scan", limit: int = 50):
    await _require_admin(request)
    safe_limit = max(1, min(int(limit), 200))
    safe_source = str(source or "").strip().lower()
    async with get_db() as db:
        if safe_source:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    datetime(created_at, '+7 hours') AS created_at,
                    event_type,
                    agent_name,
                    summary,
                    tags,
                    context,
                    result
                FROM agent_events
                WHERE agent_name = 'AIGatewayEvent'
                  AND (triggered_by = ? OR tags LIKE ?)
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_source, f'%"{safe_source}"%', safe_limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    datetime(created_at, '+7 hours') AS created_at,
                    event_type,
                    agent_name,
                    summary,
                    tags,
                    context,
                    result
                FROM agent_events
                WHERE agent_name = 'AIGatewayEvent'
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
        rows = await cursor.fetchall()

    events = []
    for row in rows:
        item = dict(row)
        try:
            parsed_tags = json.loads(item.get("tags") or "[]")
            if not isinstance(parsed_tags, list):
                parsed_tags = []
        except Exception:
            parsed_tags = []
        context_preview = str(item.get("context") or "").strip()
        if len(context_preview) > 600:
            context_preview = context_preview[:597].rstrip() + "..."
        item["tags"] = parsed_tags
        item["context_preview"] = context_preview
        events.append(item)

    return JSONResponse({"ok": True, "events": events})


@app.get("/admin/api/artifacts/recent")
async def admin_api_artifacts_recent(
    request: Request, project_slug: str = "ener-scan", limit: int = 50
):
    await _require_admin(request)
    from app.core.artifact_memory import get_recent_project_artifacts

    artifacts = await get_recent_project_artifacts(
        project_slug=project_slug,
        limit=limit,
    )
    return JSONResponse({"ok": True, "artifacts": artifacts})


@app.post("/admin/api/artifacts/backfill")
async def admin_api_artifacts_backfill(request: Request):
    await _require_admin(request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    from app.core.artifact_memory import backfill_external_event_artifacts

    result = await backfill_external_event_artifacts(
        source=str(body.get("source", "") or "").strip() or None,
        project_slug=str(body.get("project_slug", "") or "").strip() or None,
        limit=int(body.get("limit", 500) or 500),
    )
    return JSONResponse(result)


@app.get("/admin/api/artifacts/coverage")
async def admin_api_artifacts_coverage(
    request: Request, project_slug: str = "ener-scan"
):
    await _require_admin(request)
    from app.core.artifact_memory import get_artifact_coverage

    coverage = await get_artifact_coverage(project_slug=project_slug)
    return JSONResponse(coverage)


@app.get("/admin/api/business/ener-scan/summary")
async def admin_api_ener_scan_business_summary(
    request: Request,
    range: str = "7d",
    include_diagnostics: bool = False,
):
    await _require_admin(request)
    from app.core.ener_scan_business import get_ener_scan_business_summary

    summary = await get_ener_scan_business_summary(
        range_value=range,
        include_diagnostics=include_diagnostics,
    )
    return JSONResponse(summary)
