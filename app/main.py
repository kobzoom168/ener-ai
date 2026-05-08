import asyncio
import base64
import json
import re
import secrets
import shutil
import subprocess
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


def _format_short_time(raw: str) -> str:
    return str(raw)[11:16] if raw else "--:--"


def _format_full_time(raw: str) -> str:
    return str(raw)[11:19] if raw else "--:--:--"


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
    api_ok = availability.get(active_model, False) if active_model in {"haiku", "groq", "gemini"} else ollama_status == "OK"
    disk_ok = disk_percent < 80
    health_ok_count = sum([1 if sqlite_ok else 0, 1 if api_ok else 0, 1 if disk_ok else 0])
    uptime_delta = datetime.now(_BANGKOK) - _APP_STARTED_AT
    uptime_minutes = int(uptime_delta.total_seconds() // 60)
    uptime_text = f"{uptime_minutes // 60}h {uptime_minutes % 60}m"

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
            "ollama": ollama_status,
            "uptime": uptime_text,
        },
        "last_backup_time": _format_full_time(backup_row["local_created_at"]) if backup_row else "ยังไม่มี",
        "recent_conversations": _build_conversation_pairs(conversation_rows),
    }


async def _load_admin_metrics() -> dict:
    now = datetime.now(_BANGKOK)
    today = now.date().isoformat()
    seven_days = [(now.date() - timedelta(days=offset)) for offset in range(6, -1, -1)]
    history_cutoff = (now - timedelta(hours=10)).strftime("%Y-%m-%d %H:%M:%S")
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
            WHERE date(created_at, '+7 hours') = ?
            GROUP BY model
            ORDER BY calls DESC, model
            """,
            (today,),
        )
        today_calls_rows = await today_calls_cursor.fetchall()

        avg_response_cursor = await db.execute(
            """
            SELECT COALESCE(AVG(response_time_ms), 0) AS avg_response_ms
            FROM ai_runs
            WHERE date(created_at, '+7 hours') = ? AND success = 1
            """,
            (today,),
        )
        avg_response_row = await avg_response_cursor.fetchone()

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

    cost_by_day = {row["local_day"]: float(row["total"]) for row in cost_7d_rows}
    cost_7d_labels = [day.strftime("%d/%m") for day in seven_days]
    cost_7d_values = [cost_by_day.get(day.isoformat(), 0.0) for day in seven_days]

    network_in_mb = 0.0
    network_out_mb = 0.0
    if len(network_rows) >= 2:
        network_in_mb = max(0.0, (network_rows[-1]["net_in_bytes"] - network_rows[0]["net_in_bytes"]) / 1024 / 1024)
        network_out_mb = max(0.0, (network_rows[-1]["net_out_bytes"] - network_rows[0]["net_out_bytes"]) / 1024 / 1024)

    return {
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
                "message": line.strip(),
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
                "message": line.strip(),
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
        text = f"{row['action']} {row['details'] or ''}".strip()
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


def build_admin_html(status: dict, metrics: dict) -> HTMLResponse:
    status_json = json.dumps(status, ensure_ascii=False)
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Admin</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{
      margin: 0;
      background: #0f0f1a;
      color: #f2f3f7;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 18px 14px 40px;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }}
    .title {{
      font-size: 24px;
      font-weight: 700;
    }}
    .top-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .link-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 10px 12px;
      border-radius: 12px;
      background: #22243a;
      border: 1px solid #35385a;
      color: #f2f3f7;
      text-decoration: none;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .main-grid {{
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 14px;
      margin-bottom: 14px;
    }}
    .card {{
      background: #17172a;
      border: 1px solid #2c2d46;
      border-radius: 16px;
      padding: 14px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.22);
      min-height: 0;
    }}
    .card h2 {{
      margin: 0 0 10px;
      font-size: 16px;
    }}
    .summary-label {{
      color: #a5aac5;
      font-size: 12px;
      margin-bottom: 8px;
    }}
    .summary-value {{
      font-size: 28px;
      font-weight: 700;
      margin-bottom: 4px;
    }}
    .muted {{
      color: #a5aac5;
      font-size: 12px;
    }}
    .summary-canvas {{
      width: 100%;
      height: 44px;
      margin-top: 8px;
    }}
    .disk-bar {{
      height: 10px;
      border-radius: 999px;
      background: #252742;
      overflow: hidden;
      margin-top: 10px;
    }}
    .disk-fill {{
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #58d68d, #ffd166);
    }}
    .model-buttons {{
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }}
    .model-buttons form {{
      margin: 0;
    }}
    button {{
      width: 100%;
      border: 1px solid #3b3d63;
      background: #22243a;
      color: #f2f3f7;
      border-radius: 12px;
      padding: 10px 12px;
      cursor: pointer;
      font-family: inherit;
    }}
    button.active {{
      border-color: #58d68d;
      box-shadow: inset 0 0 0 1px #58d68d;
    }}
    button:disabled {{
      cursor: not-allowed;
      opacity: 0.55;
      border-color: #4f536c;
      background: #1a1b2b;
    }}
    .badge {{
      color: #9aa0ba;
      font-size: 11px;
    }}
    .health-list, .stats-list {{
      display: grid;
      gap: 6px;
      font-size: 13px;
    }}
    .health-ok {{
      color: #58d68d;
    }}
    .health-fail {{
      color: #ff7f7f;
    }}
    .chart-card {{
      min-height: 320px;
    }}
    .chart-wrap {{
      position: relative;
      height: 220px;
    }}
    .chart-wrap.small {{
      height: 160px;
      margin-top: 16px;
    }}
    .conversation-card {{
      max-height: 420px;
      overflow: auto;
    }}
    .conversation-list {{
      display: grid;
      gap: 10px;
    }}
    .conversation-item {{
      background: #111224;
      border: 1px solid #2b2d42;
      border-radius: 14px;
      padding: 12px;
    }}
    .conversation-line {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    @media (max-width: 1100px) {{
      .summary-grid {{
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .main-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">📌 Ener-AI Admin</div>
      <div class="top-actions">
        <a class="link-btn" href="/admin/logs">📜 ดู Logs</a>
      </div>
    </div>

    <section class="summary-grid">
      <div class="card">
        <div class="summary-label">CPU</div>
        <div class="summary-value" id="cpu-value">0%</div>
        <div class="muted">โหลดระบบตอนนี้</div>
        <canvas id="cpu-spark" class="summary-canvas"></canvas>
      </div>
      <div class="card">
        <div class="summary-label">RAM</div>
        <div class="summary-value" id="ram-value">0%</div>
        <div class="muted" id="ram-detail">0 / 0 MB</div>
        <canvas id="ram-spark" class="summary-canvas"></canvas>
      </div>
      <div class="card">
        <div class="summary-label">DISK</div>
        <div class="summary-value" id="disk-value">0%</div>
        <div class="muted">พื้นที่เก็บข้อมูล</div>
        <div class="disk-bar"><div id="disk-fill" class="disk-fill"></div></div>
      </div>
      <div class="card">
        <div class="summary-label">MODEL</div>
        <div class="summary-value" id="current-model">-</div>
        <div class="model-buttons">
          <form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="haiku"><button id="btn-haiku" type="submit">Claude Haiku $</button></form>
          <form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="groq"><button id="btn-groq" type="submit">Groq ฟรี ⚡</button></form>
          <form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="gemini"><button id="btn-gemini" type="submit">Gemini Flash ฟรี</button></form>
          <form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="qwen3b"><button id="btn-qwen3b" type="submit">Qwen 3B ฟรี</button></form>
          <form method="post" action="/admin/switch-model"><input type="hidden" name="model" value="qwen7b"><button id="btn-qwen7b" type="submit">Qwen 7B ฟรี</button></form>
        </div>
      </div>
      <div class="card">
        <div class="summary-label">COST</div>
        <div class="summary-value" id="today-cost">฿0.00</div>
        <div class="muted" id="today-calls">วันนี้ 0 calls</div>
        <div class="muted" id="month-cost">เดือนนี้ ฿0.00</div>
      </div>
      <div class="card">
        <div class="summary-label">HEALTH</div>
        <div class="summary-value" id="health-summary">0/3 OK</div>
        <div class="health-list">
          <div id="health-sqlite">SQLite: -</div>
          <div id="health-api">API: -</div>
          <div id="health-disk">Disk: -</div>
          <div id="health-webhook">Webhook: -</div>
          <div id="health-ollama">Ollama: -</div>
        </div>
      </div>
    </section>

    <section class="main-grid">
      <div class="card chart-card">
        <h2>📊 Server Resources (10 ชั่วโมงย้อนหลัง)</h2>
        <div class="chart-wrap"><canvas id="timeline-chart"></canvas></div>
      </div>
      <div class="card chart-card">
        <h2>📈 AI Usage Today</h2>
        <div class="stats-list">
          <div id="ai-total">Total: 0 calls</div>
          <div id="ai-cost">ค่าใช้จ่ายวันนี้: ฿0.00</div>
          <div id="ai-response">response time เฉลี่ย: 0 ms</div>
          <div id="ai-top-model">model ที่ใช้บ่อยสุด: -</div>
          <div id="network-detail">Network วันนี้: ↓0 MB / ↑0 MB</div>
          <div id="backup-detail">Last backup: ยังไม่มี</div>
          <div id="uptime-detail">Uptime: -</div>
        </div>
        <div class="chart-wrap small"><canvas id="calls-chart"></canvas></div>
        <div class="chart-wrap small"><canvas id="cost-chart"></canvas></div>
      </div>
    </section>

    <section class="card conversation-card">
      <h2>💬 บทสนทนาล่าสุด</h2>
      <div id="recent-conversations" class="conversation-list"></div>
    </section>
  </div>

  <script>
    const initialStatus = {status_json};
    const initialMetrics = {metrics_json};
    const modelLabels = {{
      haiku: "Claude Haiku $",
      groq: "Groq ฟรี ⚡",
      gemini: "Gemini Flash ฟรี",
      qwen3b: "Qwen 3B ฟรี",
      qwen7b: "Qwen 7B ฟรี"
    }};
    let timelineChart = null;
    let callsChart = null;
    let costChart = null;
    let cpuSpark = null;
    let ramSpark = null;

    function escapeHtml(text) {{
      return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function createSparkline(el, color, labels, values) {{
      return new Chart(el, {{
        type: "line",
        data: {{
          labels,
          datasets: [{{ data: values, borderColor: color, borderWidth: 2, fill: false, tension: 0.35, pointRadius: 0 }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          plugins: {{ legend: {{ display: false }}, tooltip: {{ enabled: false }} }},
          scales: {{ x: {{ display: false }}, y: {{ display: false }} }}
        }}
      }});
    }}

    function renderStatus(status) {{
      document.getElementById("current-model").textContent = status.active_model_label;
      document.getElementById("today-cost").textContent = `฿${{Number(status.today_cost_thb).toFixed(2)}}`;
      document.getElementById("today-calls").textContent = `วันนี้ ${{status.today_calls}} calls`;
      document.getElementById("month-cost").textContent = `เดือนนี้ ฿${{Number(status.month_cost_thb).toFixed(2)}}`;
      document.getElementById("health-summary").textContent = status.health.summary;
      document.getElementById("health-sqlite").textContent = `SQLite: ${{status.health.sqlite}}`;
      document.getElementById("health-api").textContent = `API: ${{status.health.api}}`;
      document.getElementById("health-disk").textContent = `Disk: ${{status.health.disk}}`;
      document.getElementById("health-webhook").textContent = `Webhook: ${{status.health.webhook}}`;
      document.getElementById("health-ollama").textContent = `Ollama: ${{status.health.ollama}}`;
      document.getElementById("backup-detail").textContent = `Last backup: ${{status.last_backup_time}}`;
      document.getElementById("uptime-detail").textContent = `Uptime: ${{status.health.uptime}}`;

      for (const id of ["haiku", "groq", "gemini", "qwen3b", "qwen7b"]) {{
        const btn = document.getElementById(`btn-${{id}}`);
        const available = !!status.model_availability[id];
        btn.classList.toggle("active", status.active_model === id);
        btn.disabled = !available;
        btn.innerHTML = available
          ? escapeHtml(modelLabels[id])
          : `${{escapeHtml(modelLabels[id])}} <span class="badge">(ไม่มี key)</span>`;
      }}

      const list = document.getElementById("recent-conversations");
      list.innerHTML = "";
      if (!status.recent_conversations.length) {{
        list.innerHTML = "<div class='conversation-item'>ยังไม่มีบทสนทนา</div>";
        return;
      }}
      for (const item of status.recent_conversations) {{
        const box = document.createElement("div");
        box.className = "conversation-item";
        box.innerHTML = `
          <div class="conversation-line"><strong>${{escapeHtml(item.time)}} [${{escapeHtml(item.model_label)}}]</strong> 👤 ${{escapeHtml(item.user || "-")}} / 🤖 ${{escapeHtml(item.assistant || "-")}}</div>
        `;
        list.appendChild(box);
      }}
    }}

    function renderMetrics(metrics) {{
      document.getElementById("cpu-value").textContent = `${{Number(metrics.realtime.cpu_percent).toFixed(0)}}%`;
      document.getElementById("ram-value").textContent = `${{Number(metrics.realtime.ram_percent).toFixed(0)}}%`;
      document.getElementById("ram-detail").textContent = `${{metrics.realtime.ram_used_mb}} / ${{metrics.realtime.ram_total_mb}} MB`;
      document.getElementById("disk-value").textContent = `${{Number(metrics.realtime.disk_percent).toFixed(0)}}%`;
      document.getElementById("disk-fill").style.width = `${{metrics.realtime.disk_percent}}%`;
      document.getElementById("ai-total").textContent = `Total: ${{metrics.ai_usage.total_calls}} calls`;
      document.getElementById("ai-cost").textContent = `ค่าใช้จ่ายวันนี้: ฿${{Number(metrics.ai_usage.total_cost_thb).toFixed(2)}}`;
      document.getElementById("ai-response").textContent = `response time เฉลี่ย: ${{Number(metrics.ai_usage.avg_response_ms).toFixed(0)}} ms`;
      document.getElementById("ai-top-model").textContent = `model ที่ใช้บ่อยสุด: ${{metrics.ai_usage.top_model_label}}`;
      document.getElementById("network-detail").textContent = `Network วันนี้: ↓${{Number(metrics.realtime.network_in_mb).toFixed(2)}} MB / ↑${{Number(metrics.realtime.network_out_mb).toFixed(2)}} MB`;

      if (!cpuSpark) {{
        cpuSpark = createSparkline(document.getElementById("cpu-spark"), "#7bdff2", metrics.history.labels, metrics.history.cpu);
        ramSpark = createSparkline(document.getElementById("ram-spark"), "#f7b267", metrics.history.labels, metrics.history.ram);
      }} else {{
        cpuSpark.data.labels = metrics.history.labels;
        cpuSpark.data.datasets[0].data = metrics.history.cpu;
        cpuSpark.update();
        ramSpark.data.labels = metrics.history.labels;
        ramSpark.data.datasets[0].data = metrics.history.ram;
        ramSpark.update();
      }}

      if (!timelineChart) {{
        timelineChart = new Chart(document.getElementById("timeline-chart"), {{
          type: "line",
          data: {{
            labels: metrics.history.labels,
            datasets: [
              {{ label: "CPU %", data: metrics.history.cpu, borderColor: "#7bdff2", tension: 0.35 }},
              {{ label: "RAM %", data: metrics.history.ram, borderColor: "#f7b267", tension: 0.35 }}
            ]
          }},
          options: {{ responsive: true, maintainAspectRatio: false }}
        }});
      }} else {{
        timelineChart.data.labels = metrics.history.labels;
        timelineChart.data.datasets[0].data = metrics.history.cpu;
        timelineChart.data.datasets[1].data = metrics.history.ram;
        timelineChart.update();
      }}

      if (!callsChart) {{
        callsChart = new Chart(document.getElementById("calls-chart"), {{
          type: "bar",
          data: {{
            labels: metrics.ai_usage.labels,
            datasets: [{{ label: "calls วันนี้", data: metrics.ai_usage.counts, backgroundColor: "#58d68d" }}]
          }},
          options: {{ responsive: true, maintainAspectRatio: false }}
        }});
      }} else {{
        callsChart.data.labels = metrics.ai_usage.labels;
        callsChart.data.datasets[0].data = metrics.ai_usage.counts;
        callsChart.update();
      }}

      if (!costChart) {{
        costChart = new Chart(document.getElementById("cost-chart"), {{
          type: "line",
          data: {{
            labels: metrics.ai_usage.cost_7d_labels,
            datasets: [{{ label: "ค่าใช้จ่าย 7 วัน", data: metrics.ai_usage.cost_7d_values, borderColor: "#ffd166", tension: 0.35 }}]
          }},
          options: {{ responsive: true, maintainAspectRatio: false }}
        }});
      }} else {{
        costChart.data.labels = metrics.ai_usage.cost_7d_labels;
        costChart.data.datasets[0].data = metrics.ai_usage.cost_7d_values;
        costChart.update();
      }}
    }}

    async function refreshStatus() {{
      const response = await fetch("/admin/api/status", {{ cache: "no-store" }});
      if (!response.ok) return;
      renderStatus(await response.json());
    }}

    async function refreshMetrics() {{
      const response = await fetch("/admin/api/metrics", {{ cache: "no-store" }});
      if (!response.ok) return;
      renderMetrics(await response.json());
    }}

    renderStatus(initialStatus);
    renderMetrics(initialMetrics);
    setInterval(refreshStatus, 30000);
    setInterval(refreshMetrics, 60000);
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
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
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
    status, metrics = await asyncio.gather(_load_admin_status(), _load_admin_metrics())
    return build_admin_html(status, metrics)


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
    return JSONResponse(await _load_admin_metrics())


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
