"""Shared context loaders for Jinja2 admin templates."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.ai import get_active_model, get_model_availability, get_model_label
from app.core.database import get_all_config, get_db
from app.core.ai_gateway import get_recent_ai_traces

_BANGKOK = ZoneInfo("Asia/Bangkok")


def _audit_log_level(action: str, details: str) -> tuple[str, str]:
    blob = f"{action} {details}".lower()
    if any(k in blob for k in ("error", "fail", "failed", "exception")):
        return "error", "ERR!"
    if any(k in blob for k in ("warn", "timeout", "degraded")):
        return "warn", "WARN"
    if any(k in blob for k in ("success", "done", "completed", "saved", "switched")):
        return "success", "DONE"
    return "info", "INFO"


def _clip_detail(text: str, limit: int = 100) -> str:
    raw = str(text or "").replace("\n", " ").strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 3].rstrip() + "..."


# Imported from main at runtime to avoid circular imports at module load
_main = None


def _bind_main():
    global _main
    if _main is None:
        from app import main as m

        _main = m
    return _main


async def load_admin_base_context(active_nav: str, page_title: str = "Ener-AI Admin") -> dict:
    m = _bind_main()
    overview = await m._load_admin_overview()
    status = await m._load_admin_status()
    realtime = m._realtime_metrics()
    availability = get_model_availability()
    active_model = await get_active_model() or status.get("active_model", "groq")

    open_tasks = 0
    total_ai_calls = 0
    today_tokens = 0
    week_cost = 0.0
    avg_latency_ms = 0
    projects: list[dict] = []
    audit_lines: list[dict] = []
    system_logs: list[dict] = []
    tasks: list[dict] = []

    async with get_db() as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE COALESCE(status, 'open') NOT IN ('done', 'closed')"
            )
        ).fetchone()
        open_tasks = int(row["c"] or 0) if row else 0
        row = await (await db.execute("SELECT COUNT(*) AS c FROM ai_runs")).fetchone()
        total_ai_calls = int(row["c"] or 0) if row else 0
        today = datetime.now(_BANGKOK).date().isoformat()
        week_row = await (
            await db.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_thb), 0) AS cost,
                       COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens
                FROM ai_runs
                WHERE datetime(created_at, '+7 hours') >= datetime('now', '-7 days', '+7 hours')
                """
            )
        ).fetchone()
        week_cost = float(week_row["cost"] or 0) if week_row else 0.0
        tok_row = await (
            await db.execute(
                """
                SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t
                FROM ai_runs WHERE date(created_at, '+7 hours') = ?
                """,
                (today,),
            )
        ).fetchone()
        today_tokens = int(tok_row["t"] or 0) if tok_row else 0
        lat_row = await (
            await db.execute(
                """
                SELECT COALESCE(AVG(duration_ms), 0) AS avg_ms
                FROM agent_runs
                WHERE date(created_at, '+7 hours') = ? AND success = 1
                """,
                (today,),
            )
        ).fetchone()
        avg_latency_ms = int(round(float(lat_row["avg_ms"] or 0))) if lat_row else 0
        cur = await db.execute(
            """
            SELECT id, title, COALESCE(status, 'open') AS status,
                   COALESCE(priority, 'medium') AS priority
            FROM tasks
            ORDER BY
              CASE COALESCE(status, 'open')
                WHEN 'open' THEN 0 WHEN 'in-progress' THEN 1 WHEN 'pending' THEN 2
                WHEN 'done' THEN 9 WHEN 'closed' THEN 10 ELSE 3 END,
              CASE COALESCE(priority, 'medium')
                WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
              id DESC
            LIMIT 8
            """
        )
        for tr in await cur.fetchall():
            st = str(tr["status"] or "open").lower()
            if st in {"done", "closed", "completed"}:
                norm = "completed"
            elif st in {"in-progress", "in_progress", "active"}:
                norm = "in-progress"
            else:
                norm = "pending"
            tasks.append(
                {
                    "id": tr["id"],
                    "title": str(tr["title"] or "")[:80],
                    "status": norm,
                    "priority": str(tr["priority"] or "medium").lower(),
                }
            )
        cur = await db.execute(
            """
            SELECT p.id, p.name, COUNT(m.id) AS message_count,
                   datetime(p.created_at, '+7 hours') AS created_at
            FROM projects p
            LEFT JOIN messages m ON m.project_id = p.id
            WHERE p.deleted_at IS NULL
            GROUP BY p.id, p.name, p.created_at
            ORDER BY p.id DESC
            LIMIT 100
            """
        )
        projects = [dict(r) for r in await cur.fetchall()]
        cur = await db.execute(
            """
            SELECT datetime(created_at, '+7 hours') AS ts, action, details
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 30
            """
        )
        for r in await cur.fetchall():
            action = str(r["action"] or "")
            details = str(r["details"] or "")
            level, level_label = _audit_log_level(action, details)
            ts_raw = str(r["ts"] or "")
            ts_short = ts_raw.split(" ")[-1] if " " in ts_raw else ts_raw[-8:]
            audit_lines.append(
                {
                    "ts": ts_short,
                    "line": f"[{ts_raw}] {action}: {_clip_detail(details)}",
                }
            )
            system_logs.append(
                {
                    "timestamp": ts_short or "--:--:--",
                    "level": level,
                    "level_label": level_label,
                    "message": _clip_detail(f"{action}: {details}", 160),
                }
            )

    tasks_open = sum(1 for t in tasks if t["status"] != "completed")
    tasks_active = sum(1 for t in tasks if t["status"] == "in-progress")
    tasks_done = sum(1 for t in tasks if t["status"] == "completed")

    today_thb = float(status.get("today_cost_thb", 0.0))
    month_thb = float(status.get("month_cost_thb", 0.0))
    cost_grid = [
        {"label": "TODAY", "value": m._format_baht(today_thb), "change": None, "trend": "up"},
        {"label": "THIS_WEEK", "value": m._format_baht(week_cost), "change": None, "trend": "up"},
        {"label": "THIS_MONTH", "value": m._format_baht(month_thb), "change": None, "trend": "up"},
        {
            "label": "TOKENS_USED",
            "value": m._format_number(today_tokens),
            "change": None,
            "trend": "up",
        },
    ]

    _api_meta = {
        "groq": ("GROQ_API", "/v1/chat/completions"),
        "haiku": ("ANTHROPIC_API", "/v1/messages"),
        "gemini": ("GEMINI_API", "/v1/models"),
        "deepseek": ("DEEPSEEK_API", "/v1/chat"),
        "openai": ("OPENAI_API", "/v1/chat/completions"),
    }
    api_services = []
    api_endpoints = []
    online_n = degraded_n = offline_n = 0
    for key, (name, endpoint) in _api_meta.items():
        online = bool(availability.get(key, False))
        st = "online" if online else "offline"
        if st == "online":
            online_n += 1
        else:
            offline_n += 1
        api_services.append(
            {
                "name": name.replace("_API", " API"),
                "online": online,
                "status_label": "ONLINE" if online else "OFFLINE",
            }
        )
        api_endpoints.append(
            {
                "id": key,
                "name": name,
                "endpoint": endpoint,
                "status": st,
                "status_label": "ONLINE" if online else "OFFLINE",
                "latency_display": "—",
                "key_status": "OK" if online else "—",
                "last_check": "cached",
            }
        )
    api_summary = {"online": online_n, "degraded": degraded_n, "offline": offline_n}

    stats = overview.get("stats", [])
    return {
        "active_nav": active_nav,
        "page_title": page_title,
        "now_time": datetime.now(_BANGKOK).strftime("%H:%M"),
        "active_model": str(active_model).upper(),
        "active_model_label": get_model_label(active_model),
        "cost_today": m._format_baht(status.get("today_cost_thb", 0.0)),
        "cost_today_thb": float(status.get("today_cost_thb", 0.0)),
        "month_cost": m._format_baht(status.get("month_cost_thb", 0.0)),
        "today_calls": m._format_number(status.get("today_calls", 0)),
        "total_ai_calls": m._format_number(total_ai_calls),
        "open_tasks": open_tasks,
        "open_tasks_label": f"{open_tasks} OPEN",
        "cpu_percent": int(round(float(realtime.get("cpu_percent", 0.0)))),
        "ram_percent": int(round(float(realtime.get("ram_percent", 0.0)))),
        "disk_percent": int(round(float(realtime.get("disk_percent", 0.0)))),
        "health_summary": status.get("health", {}).get("summary", "0/3 OK"),
        "uptime": status.get("health", {}).get("uptime", "—"),
        "api_services": api_services,
        "stats": stats,
        "timeline": overview.get("timeline", [])[:25],
        "audit_lines": audit_lines,
        "model_panel": overview.get("model_panel", {}),
        "server": overview.get("server", {}),
        "top_agents": overview.get("top_agents", []),
        "errors": overview.get("errors", []),
        "cost_chart": overview.get("cost_chart", {}),
        "projects": projects,
        "cost_grid": cost_grid,
        "tasks": tasks[:5],
        "tasks_open": tasks_open,
        "tasks_active": tasks_active,
        "tasks_done": tasks_done,
        "api_endpoints": api_endpoints,
        "api_summary": api_summary,
        "system_logs": system_logs[:12],
        "avg_latency_ms": avg_latency_ms,
    }


async def load_admin_ai_context() -> dict:
    ctx = await load_admin_base_context("ai", "AI — Ener-AI")
    ctx["ai_links"] = [
        {"href": "/admin/ai-traces", "icon": "◇", "title": "TRACE", "desc": "conversation_id · tools"},
        {"href": "/admin/routing", "icon": "⇄", "title": "ROUTING", "desc": "intent → model"},
        {"href": "/admin/pipeline", "icon": "⚡", "title": "PIPELINE", "desc": "reasoner · checker"},
        {"href": "/admin/metrics", "icon": "▣", "title": "METRICS", "desc": "latency · cost"},
    ]
    try:
        traces = await get_recent_ai_traces(limit=12)
    except Exception:
        traces = []
    ctx["recent_traces"] = traces
    return ctx


async def load_admin_settings_context() -> dict:
    ctx = await load_admin_base_context("settings", "Settings — Ener-AI")
    try:
        configs = await get_all_config()
    except Exception:
        configs = []
    ctx["configs"] = [c for c in configs if not int(c.get("is_secret") or 0)][:20]
    ctx["config_count"] = len(configs)
    return ctx
