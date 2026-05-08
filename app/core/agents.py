import time
from functools import wraps

from app.core.database import get_db

COMMAND_AGENT_MAP = {
    "note": "NoteAgent",
    "task": "TaskAgent",
    "tasks": "TaskAgent",
    "done": "TaskAgent",
    "code": "CodeAgent",
    "ener": "EnerAgent",
    "content": "ContentAgent",
    "health": "LogKeeper",
    "today": "DigestAgent",
    "week": "DigestAgent",
    "news": "NewsAgent",
    "think": "ThinkTeam",
    "brainstorm": "ThinkTeam",
    "learn": "LessonAgent",
    "mistake": "LessonAgent",
    "park": "MemoryAgent",
    "search": "MemoryAgent",
    "voice": "VoiceAgent",
    "remember": "MemoryAgent",
    "forget": "MemoryAgent",
    "memory": "MemoryAgent",
    "cost": "CostAgent",
    "chat": "MainChatAgent",
}

SCHEDULER_AGENTS = {
    "news_fetch": "NewsAgent",
    "daily_digest": "DigestAgent",
    "weekly_review": "DigestAgent",
    "health_check": "HealthAgent",
    "backup": "BackupAgent",
    "metrics": "MetricsAgent",
    "session": "SessionAgent",
    "log_keeper": "LogKeeper",
}

_AGENT_AI_MAP = {
    "NoteAgent": "brain",
    "MainChatAgent": "chat",
    "MainAgent": "mainagent",
    "NewsAgent": "news",
    "DigestAgent": "summary",
    "LessonAgent": "learn",
    "ThinkTeam": "brainstorm",
    "MemoryAgent": "memory",
    "SessionAgent": "session",
    "CodeAgent": "code",
    "ContentAgent": "content",
    "EnerAgent": "ener",
    "LogKeeper": "logkeeper",
}


def _compact_summary(value, limit: int = 100) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _pick_input_summary(args: tuple, kwargs: dict) -> str:
    override = kwargs.get("_agent_input_summary")
    if override:
        return _compact_summary(override)

    for key in ["text", "query", "topic", "title", "args", "keyword"]:
        value = kwargs.get(key)
        if value:
            return _compact_summary(value)

    for value in args:
        if isinstance(value, str) and value and not value.isdigit():
            return _compact_summary(value)

    if args:
        return _compact_summary(args[0])
    return ""


async def _ai_usage_snapshot(agent_name: str, after_id: int) -> tuple[str, float]:
    ai_agent = _AGENT_AI_MAP.get(agent_name)
    if not ai_agent:
        return "", 0.0

    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT model, COALESCE(SUM(estimated_cost_thb), 0) AS total_cost
            FROM ai_runs
            WHERE id > ? AND agent = ?
            GROUP BY model
            ORDER BY MAX(id)
            """,
            (after_id, ai_agent),
        )
        rows = await cursor.fetchall()

    if not rows:
        return "", 0.0

    models = ", ".join(str(row["model"]) for row in rows if row["model"])
    total_cost = sum(float(row["total_cost"] or 0) for row in rows)
    return models, total_cost


async def _max_ai_run_id() -> int:
    async with get_db() as db:
        cursor = await db.execute("SELECT COALESCE(MAX(id), 0) AS last_id FROM ai_runs")
        row = await cursor.fetchone()
    return int(row["last_id"] or 0) if row else 0


async def _insert_agent_run(
    agent_name: str,
    triggered_by: str,
    input_summary: str,
    output_summary: str,
    model_used: str,
    duration_ms: int,
    success: bool,
    error_msg: str | None,
    cost_thb: float,
) -> None:
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO agent_runs (
                agent_name,
                triggered_by,
                input_summary,
                output_summary,
                model_used,
                duration_ms,
                success,
                error_msg,
                cost_thb
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_name,
                triggered_by,
                input_summary,
                output_summary,
                model_used,
                duration_ms,
                1 if success else 0,
                error_msg,
                cost_thb,
            ),
        )
        await db.commit()


def log_agent_run(agent_name: str, triggered_by: str = "user"):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            runtime_triggered_by = str(kwargs.pop("_agent_triggered_by", triggered_by) or triggered_by)
            input_summary = _pick_input_summary(args, kwargs)
            started_at = time.perf_counter()
            ai_run_id_before = 0
            error_msg = None
            result = None
            success = False

            try:
                ai_run_id_before = await _max_ai_run_id()
            except Exception:
                ai_run_id_before = 0

            try:
                result = await func(*args, **kwargs)
                success = True
                return result
            except Exception as exc:
                error_msg = _compact_summary(exc)
                raise
            finally:
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                output_summary = _compact_summary(result)
                try:
                    model_used, cost_thb = await _ai_usage_snapshot(agent_name, ai_run_id_before)
                    await _insert_agent_run(
                        agent_name=agent_name,
                        triggered_by=runtime_triggered_by,
                        input_summary=input_summary,
                        output_summary=output_summary,
                        model_used=model_used,
                        duration_ms=duration_ms,
                        success=success,
                        error_msg=error_msg,
                        cost_thb=cost_thb,
                    )
                except Exception:
                    pass

        return wrapper

    return decorator
