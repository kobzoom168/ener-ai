import base64
import json
import secrets
import shutil
from pathlib import Path
from urllib.parse import parse_qs
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from telegram import Update
from app.core.database import init_db
from app.core.config import settings
from app.bot.router import build_application
from app.scheduler import build_scheduler
from app.core.ai import get_active_model, get_model_availability, get_model_label

telegram_app = build_application()
scheduler = None
_BANGKOK = ZoneInfo("Asia/Bangkok")
_APP_STARTED_AT = datetime.now(_BANGKOK)


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


async def _require_admin(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        encoded = auth_header.split(" ", 1)[1]
        decoded = base64.b64decode(encoded).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not (
        secrets.compare_digest(username, "admin")
        and secrets.compare_digest(password, settings.admin_password)
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


async def _load_admin_status() -> dict:
    from app.core.database import get_db

    active_model = await get_active_model() or "haiku"
    availability = get_model_availability()
    today = datetime.now(_BANGKOK).date().isoformat()
    month_key = datetime.now(_BANGKOK).strftime("%Y-%m")

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
        recent_cursor = await db.execute(
            """
            SELECT agent, model, estimated_cost_thb, datetime(created_at, '+7 hours') AS local_created_at
            FROM ai_runs
            ORDER BY id DESC
            LIMIT 5
            """,
        )
        recent_rows = await recent_cursor.fetchall()
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
            ("7486743496",),
        )
        conversation_rows = await conversation_cursor.fetchall()
        sqlite_ok = True
        try:
            check_cursor = await db.execute("SELECT 1 AS ok")
            check_row = await check_cursor.fetchone()
            sqlite_ok = bool(check_row and check_row["ok"] == 1)
        except Exception:
            sqlite_ok = False

    disk_usage = shutil.disk_usage(_data_dir())
    disk_percent = round(disk_usage.used / disk_usage.total * 100)
    uptime_delta = datetime.now(_BANGKOK) - _APP_STARTED_AT
    uptime_minutes = int(uptime_delta.total_seconds() // 60)
    uptime_text = f"{uptime_minutes // 60}h {uptime_minutes % 60}m"
    ordered_messages = list(reversed(conversation_rows))
    recent_conversations = []
    current_pair: dict[str, str] | None = None
    for row in ordered_messages:
        role = row["role"]
        if role == "user":
            if current_pair and (current_pair.get("user") or current_pair.get("assistant")):
                recent_conversations.append(current_pair)
            current_pair = {
                "time": str(row["local_created_at"])[11:16],
                "model": row["model"] or "haiku",
                "model_label": get_model_label(row["model"] or "haiku"),
                "user": _truncate_text(row["content"]),
                "assistant": "",
            }
        elif role == "assistant":
            if current_pair is None:
                current_pair = {
                    "time": str(row["local_created_at"])[11:16],
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
                    "time": str(row["local_created_at"])[11:16],
                    "model": row["model"] or "haiku",
                    "model_label": get_model_label(row["model"] or "haiku"),
                    "user": "",
                    "assistant": _truncate_text(row["content"]),
                }
    if current_pair and (current_pair.get("user") or current_pair.get("assistant")):
        recent_conversations.append(current_pair)
    recent_conversations = list(reversed(recent_conversations[-10:]))

    return {
        "active_model": active_model,
        "active_model_label": get_model_label(active_model),
        "model_availability": availability,
        "today_cost_thb": float(today_cost["total"]),
        "today_calls": int(today_cost["calls"]),
        "month_cost_thb": float(month_cost["total"]),
        "health": {
            "sqlite": "OK" if sqlite_ok else "FAIL",
            "anthropic": "OK" if settings.anthropic_api_key else "FAIL",
            "disk_percent": disk_percent,
            "uptime": uptime_text,
        },
        "recent_runs": [
            {
                "time": str(row["local_created_at"])[11:16],
                "agent": row["agent"],
                "model": row["model"],
                "model_label": get_model_label(row["model"]),
                "cost": float(row["estimated_cost_thb"]),
            }
            for row in recent_rows
        ],
        "recent_conversations": recent_conversations,
    }


def build_admin_html(status: dict) -> HTMLResponse:
    status_json = json.dumps(status, ensure_ascii=False)
    html = f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ener-AI Admin</title>
  <style>
    body {{
      margin: 0;
      background: #0f0f1a;
      color: #f2f3f7;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    .wrap {{
      max-width: 960px;
      margin: 0 auto;
      padding: 20px 16px 40px;
    }}
    .title {{
      font-size: 24px;
      margin-bottom: 16px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: #17172a;
      border: 1px solid #2c2d46;
      border-radius: 16px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.22);
    }}
    .card h2 {{
      margin: 0 0 12px;
      font-size: 18px;
    }}
    .big {{
      font-size: 28px;
      font-weight: 700;
      margin: 8px 0;
    }}
    .muted {{
      color: #a5aac5;
    }}
    .dot {{
      color: #58d68d;
      font-weight: 700;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin: 6px 0;
      flex-wrap: wrap;
    }}
    .buttons {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }}
    button {{
      border: 1px solid #3b3d63;
      background: #22243a;
      color: #f2f3f7;
      border-radius: 12px;
      padding: 12px 14px;
      cursor: pointer;
      width: 100%;
    }}
    .button-form {{
      flex: 1 1 180px;
    }}
    .active {{
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
      font-size: 12px;
    }}
    ul {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    li {{
      padding: 7px 0;
      border-bottom: 1px solid #2b2d42;
    }}
    li:last-child {{
      border-bottom: none;
    }}
    .full {{
      grid-column: 1 / -1;
    }}
    .conversation-list {{
      display: grid;
      gap: 12px;
    }}
    .conversation-item {{
      background: #111224;
      border: 1px solid #2b2d42;
      border-radius: 14px;
      padding: 12px;
    }}
    .conversation-head {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      color: #d4d7e8;
      flex-wrap: wrap;
    }}
    .conversation-line {{
      margin: 6px 0;
      line-height: 1.45;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="title">📌 Ener-AI Admin</div>
    <div class="grid">
      <section class="card">
        <h2>🤖 AI Model</h2>
        <div id="current-model">ปัจจุบัน: {status["active_model_label"]} <span class="dot">●</span></div>
        <div class="buttons">
          <form class="button-form" method="post" action="/admin/switch-model">
            <input type="hidden" name="model" value="haiku">
            <button id="btn-haiku" type="submit">Claude Haiku $</button>
          </form>
          <form class="button-form" method="post" action="/admin/switch-model">
            <input type="hidden" name="model" value="groq">
            <button id="btn-groq" type="submit">Groq ฟรี ⚡</button>
          </form>
          <form class="button-form" method="post" action="/admin/switch-model">
            <input type="hidden" name="model" value="gemini">
            <button id="btn-gemini" type="submit">Gemini Flash ฟรี</button>
          </form>
          <form class="button-form" method="post" action="/admin/switch-model">
            <input type="hidden" name="model" value="qwen3b">
            <button id="btn-qwen3b" type="submit">Qwen 3B ฟรี</button>
          </form>
          <form class="button-form" method="post" action="/admin/switch-model">
            <input type="hidden" name="model" value="qwen7b">
            <button id="btn-qwen7b" type="submit">Qwen 7B ฟรี</button>
          </form>
        </div>
      </section>
      <section class="card">
        <h2>💰 ค่าใช้จ่ายวันนี้</h2>
        <div class="big" id="today-cost">฿{status["today_cost_thb"]:.2f}</div>
        <div class="muted" id="today-calls">({status["today_calls"]} ครั้ง)</div>
        <div class="row"><span>เดือนนี้:</span><span id="month-cost">฿{status["month_cost_thb"]:.2f}</span></div>
      </section>
      <section class="card">
        <h2>🏥 สถานะระบบ</h2>
        <div class="row"><span>SQLite</span><span id="health-sqlite"><span class="dot">●</span> {status["health"]["sqlite"]}</span></div>
        <div class="row"><span>Anthropic</span><span id="health-anthropic"><span class="dot">●</span> {status["health"]["anthropic"]}</span></div>
        <div class="row"><span>Disk</span><span id="health-disk"><span class="dot">●</span> {status["health"]["disk_percent"]}% ใช้งาน</span></div>
        <div class="row"><span>Uptime</span><span id="health-uptime">{status["health"]["uptime"]}</span></div>
      </section>
      <section class="card">
        <h2>📊 การเรียก AI ล่าสุด</h2>
        <ul id="recent-calls"></ul>
      </section>
      <section class="card full">
        <h2>💬 บทสนทนาล่าสุด</h2>
        <div id="recent-conversations" class="conversation-list"></div>
      </section>
    </div>
  </div>
  <script>
    const initialStatus = {status_json};
    const modelLabels = {{
      haiku: "Claude Haiku $",
      groq: "Groq ฟรี ⚡",
      gemini: "Gemini Flash ฟรี",
      qwen3b: "Qwen 3B ฟรี",
      qwen7b: "Qwen 7B ฟรี"
    }};
    function escapeHtml(text) {{
      return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}
    function render(status) {{
      document.getElementById("current-model").innerHTML = `ปัจจุบัน: ${{escapeHtml(status.active_model_label)}} <span class="dot">●</span>`;
      document.getElementById("today-cost").textContent = `฿${{Number(status.today_cost_thb).toFixed(2)}}`;
      document.getElementById("today-calls").textContent = `(${{status.today_calls}} ครั้ง)`;
      document.getElementById("month-cost").textContent = `฿${{Number(status.month_cost_thb).toFixed(2)}}`;
      document.getElementById("health-sqlite").innerHTML = `<span class="dot">●</span> ${{escapeHtml(status.health.sqlite)}}`;
      document.getElementById("health-anthropic").innerHTML = `<span class="dot">●</span> ${{escapeHtml(status.health.anthropic)}}`;
      document.getElementById("health-disk").innerHTML = `<span class="dot">●</span> ${{status.health.disk_percent}}% ใช้งาน`;
      document.getElementById("health-uptime").textContent = status.health.uptime;
      for (const id of ["haiku", "groq", "gemini", "qwen3b", "qwen7b"]) {{
        const btn = document.getElementById(`btn-${{id}}`);
        const available = !!status.model_availability[id];
        btn.classList.toggle("active", status.active_model === id);
        btn.disabled = !available;
        btn.innerHTML = available
          ? escapeHtml(modelLabels[id])
          : `${{escapeHtml(modelLabels[id])}} <span class="badge">(ไม่มี key)</span>`;
      }}
      const recent = document.getElementById("recent-calls");
      recent.innerHTML = "";
      if (!status.recent_runs.length) {{
        recent.innerHTML = "<li>ยังไม่มีข้อมูล</li>";
        return;
      }}
      for (const run of status.recent_runs) {{
        const item = document.createElement("li");
        item.textContent = `${{run.time}} ${{run.agent}} ${{run.model_label}} ฿${{Number(run.cost).toFixed(2)}}`;
        recent.appendChild(item);
      }}
      const conversations = document.getElementById("recent-conversations");
      conversations.innerHTML = "";
      if (!status.recent_conversations.length) {{
        conversations.innerHTML = "<div class='conversation-item'>ยังไม่มีบทสนทนา</div>";
        return;
      }}
      for (const item of status.recent_conversations) {{
        const box = document.createElement("div");
        box.className = "conversation-item";
        box.innerHTML = `
          <div class="conversation-head">
            <span>${{escapeHtml(item.time)}}</span>
            <span>[${{escapeHtml(item.model_label)}}]</span>
          </div>
          <div class="conversation-line">👤 ${{escapeHtml(item.user || "-")}}</div>
          <div class="conversation-line">🤖 ${{escapeHtml(item.assistant || "-")}}</div>
        `;
        conversations.appendChild(box);
      }}
    }}
    async function refreshStatus() {{
      const response = await fetch("/admin/api/status", {{ cache: "no-store" }});
      if (!response.ok) return;
      const status = await response.json();
      render(status);
    }}
    render(initialStatus);
    setInterval(refreshStatus, 30000);
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
    status = await _load_admin_status()
    return build_admin_html(status)


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
    from app.core.database import get_db

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
