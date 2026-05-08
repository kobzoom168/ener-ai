import json
import re

from app.core.database import get_db

_MAX_SUMMARY_CHARS = 200
_MAX_LEARNED_CHARS = 500
_MAX_CONTEXT_CHARS = 2000
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
]


def _compact(value, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _sanitize_text(value, limit: int) -> str | None:
    text = _compact(value, limit * 2)
    if not text:
        return None
    for pattern in _SENSITIVE_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return _compact(text, limit)


def _normalize_tags(tags: list[str] | None) -> list[str]:
    normalized = []
    seen = set()
    for tag in tags or []:
        clean = " ".join(str(tag or "").split()).strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        normalized.append(clean)
    return normalized


async def log_event(
    agent_name: str,
    event_type: str,
    summary: str,
    tags: list[str] = None,
    context: str = None,
    result: str = "success",
    learned: str = None,
    triggered_by: str = "user",
    related_event_id: int | None = None,
) -> int:
    """บันทึก event ลง agent_events table"""
    safe_tags = _normalize_tags(tags)
    safe_summary = _sanitize_text(summary, _MAX_SUMMARY_CHARS) or "event"
    safe_context = _sanitize_text(context, _MAX_CONTEXT_CHARS)
    safe_learned = _sanitize_text(learned, _MAX_LEARNED_CHARS)

    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO agent_events
            (event_type, agent_name, triggered_by, tags, summary,
             context, result, learned, related_event_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_type or "").strip(),
                str(agent_name or "").strip(),
                str(triggered_by or "user").strip(),
                json.dumps(safe_tags, ensure_ascii=False),
                safe_summary,
                safe_context,
                str(result or "success").strip().lower(),
                safe_learned,
                related_event_id,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid or 0)


async def query_events(
    agent_name: str = None,
    tags: list[str] = None,
    result: str = None,
    limit: int = 20,
) -> list[dict]:
    """query events สำหรับ agent อ่าน"""
    conditions = []
    params = []

    if agent_name:
        conditions.append("agent_name = ?")
        params.append(agent_name)
    if result:
        conditions.append("result = ?")
        params.append(str(result).lower())
    if tags:
        for tag in _normalize_tags(tags):
            conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(int(limit))

    async with get_db() as db:
        cursor = await db.execute(
            f"""
            SELECT event_type, agent_name, tags, summary,
                   context, result, learned,
                   datetime(created_at, '+7 hours') as local_time
            FROM agent_events {where}
            ORDER BY id DESC LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()

    items = []
    for row in rows:
        item = dict(row)
        try:
            item["tags"] = json.loads(item.get("tags") or "[]")
        except Exception:
            item["tags"] = []
        items.append(item)
    return items


async def get_agent_context(agent_name: str, topic_tags: list[str]) -> str:
    """ดึง context สำหรับ agent ก่อนทำงาน"""
    past_events = await query_events(
        agent_name=agent_name,
        tags=topic_tags,
        limit=10,
    )
    failures = await query_events(
        agent_name=agent_name,
        result="failure",
        limit=5,
    )

    if not past_events and not failures:
        return ""

    lines = ["=== Agent Memory ==="]

    if past_events:
        lines.append("เคยทำมาแล้ว:")
        for event in past_events[:5]:
            lines.append(f"  [{event['result']}] {event['summary']}")
            if event.get("learned"):
                lines.append(f"  → เรียนรู้: {event['learned']}")

    if failures:
        lines.append("สิ่งที่เคย fail:")
        for event in failures[:3]:
            lines.append(f"  ❌ {event['summary']}")
            if event.get("learned"):
                lines.append(f"  → แก้ได้ด้วย: {event['learned']}")

    return "\n".join(lines)


async def prune_old_events(days: int = 30) -> int:
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM agent_events WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",),
        )
        deleted = int(cursor.rowcount or 0)
        await db.commit()
    return deleted
