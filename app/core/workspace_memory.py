"""Workspace chat memory: date index + keyword recall for cross-day context."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.database import get_db, index_message_to_fts

_BANGKOK = ZoneInfo("Asia/Bangkok")
_MAX_CONTEXT_CHARS = 4500
_PREVIEW_LEN = 72
_RECALL_HINTS = (
    "วันไหน",
    "เมื่อไหร่",
    "เมื่อวาน",
    "วานนี้",
    "เคยถาม",
    "เคยพูด",
    "เคยคุย",
    "จำได้",
    "เรื่องอะไร",
    "ถามอะไร",
    "พูดอะไร",
    "คุยอะไร",
    "ข้อมูลเมื่อ",
    "ประวัติ",
    "ย้อนหลัง",
    "วันที่",
    "ตอนนี้มีงาน",
    "งานอะไรบ้าง",
)


def _compact(text: str, limit: int = _PREVIEW_LEN) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _format_day_label(day_key: str) -> str:
    try:
        dt = datetime.strptime(day_key, "%Y-%m-%d")
        months = [
            "",
            "ม.ค.",
            "ก.พ.",
            "มี.ค.",
            "เม.ย.",
            "พ.ค.",
            "มิ.ย.",
            "ก.ค.",
            "ส.ค.",
            "ก.ย.",
            "ต.ค.",
            "พ.ย.",
            "ธ.ค.",
        ]
        return f"{dt.day} {months[dt.month]} {dt.year}"
    except ValueError:
        return day_key


def _extract_keywords(text: str) -> list[str]:
    words: list[str] = []
    for raw in re.split(r"[\s,;:!?。．、]+", str(text or "")):
        w = raw.strip().lower()
        if len(w) < 2:
            continue
        if w in words:
            continue
        words.append(w)
        if len(words) >= 8:
            break
    return words


def _wants_recall_search(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _RECALL_HINTS)


async def _fetch_messages_for_index(chat_id: str, project_id: int | None, days: int = 45) -> list[dict]:
    days_val = max(1, int(days))
    params: list[object] = [chat_id]
    project_sql = ""
    if project_id is not None:
        project_sql = " AND project_id = ?"
        params.append(project_id)
    params.append(f"-{days_val} days")
    async with get_db() as db:
        cur = await db.execute(
            f"""
            SELECT
                id,
                role,
                content,
                datetime(created_at, '+7 hours') AS local_at
            FROM messages
            WHERE chat_id = ?
              {project_sql}
              AND role IN ('user', 'assistant')
              AND created_at >= datetime('now', ?)
            ORDER BY id ASC
            """,
            tuple(params),
        )
        rows = await cur.fetchall()
    return [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "local_at": str(row["local_at"] or ""),
        }
        for row in rows
    ]


def _build_daily_index(messages: list[dict]) -> list[tuple[str, list[str]]]:
    by_day: dict[str, list[str]] = defaultdict(list)
    for row in messages:
        if row["role"] != "user":
            continue
        day = str(row["local_at"])[:10]
        if not day:
            continue
        preview = _compact(row["content"], 90)
        if not preview:
            continue
        topics = by_day[day]
        if preview not in topics:
            topics.append(preview)
    ordered = sorted(by_day.items(), key=lambda item: item[0], reverse=True)
    return [(day, topics[:4]) for day, topics in ordered]


async def _messages_on_date(
    chat_id: str,
    day_key: str,
    *,
    project_id: int | None = None,
    limit: int = 16,
) -> list[dict]:
    rows = await _workspace_history_rows_impl(
        chat_id, project_id=project_id, limit=limit, chat_date=day_key
    )
    return [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "local_at": f"{day_key} 12:00:00",
        }
        for row in rows
    ]


async def _search_related_messages(
    chat_id: str,
    query: str,
    *,
    project_id: int | None = None,
    limit: int = 10,
) -> list[dict]:
    lowered = str(query or "").lower()
    keywords = _extract_keywords(query)
    if not keywords:
        if not _wants_recall_search(query):
            return []
        if "เมื่อวาน" in lowered or "วานนี้" in lowered:
            yesterday = (datetime.now(_BANGKOK) - timedelta(days=1)).strftime("%Y-%m-%d")
            return await _messages_on_date(
                chat_id, yesterday, project_id=project_id, limit=limit
            )
        recent = await _workspace_history_rows_impl(
            chat_id, project_id=project_id, limit=limit, chat_date=None
        )
        return [
            {
                "id": int(row["id"]),
                "role": str(row["role"]),
                "content": str(row["content"] or ""),
                "local_at": "",
            }
            for row in recent[-limit:]
        ]

    clauses = []
    params: list[object] = [chat_id]
    if project_id is not None:
        project_clause = " AND project_id = ?"
        params.insert(1, project_id)
    else:
        project_clause = ""

    for kw in keywords[:6]:
        clauses.append("LOWER(content) LIKE ?")
        params.append(f"%{kw}%")
    if not clauses:
        return []

    where_kw = " OR ".join(clauses)
    params.append(max(1, int(limit)))
    async with get_db() as db:
        cur = await db.execute(
            f"""
            SELECT
                id,
                role,
                content,
                datetime(created_at, '+7 hours') AS local_at
            FROM messages
            WHERE chat_id = ?
              {project_clause}
              AND role IN ('user', 'assistant')
              AND ({where_kw})
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        )
        rows = await cur.fetchall()
    hits = [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
            "local_at": str(row["local_at"] or ""),
        }
        for row in reversed(rows)
    ]
    return hits


async def build_workspace_history_for_ai(
    chat_id: str,
    current_message: str,
    *,
    project_id: int | None = None,
    recent_limit: int = 36,
) -> list[dict[str, str]]:
    """Recent turns plus keyword-related older messages (deduped, chronological)."""
    recent = await _workspace_history_rows_impl(
        chat_id, project_id=project_id, limit=recent_limit, chat_date=None
    )
    related = await _search_related_messages(
        chat_id, current_message, project_id=project_id, limit=12
    )
    merged: dict[int, dict[str, str]] = {}
    for row in related + recent:
        merged[int(row["id"])] = {
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
        }
    ordered = sorted(merged.items(), key=lambda item: item[0])
    history = [{"role": r["role"], "content": r["content"]} for _, r in ordered]
    if len(history) > 60:
        history = history[-60:]
    return history


async def _workspace_history_rows_impl(
    chat_id: str,
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
                SELECT id, role, content
                FROM messages
                WHERE chat_id = ?
                  AND role IN ('user', 'assistant')
                  {date_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, *date_params, limit_value),
            )
        else:
            cursor = await db.execute(
                f"""
                SELECT id, role, content
                FROM messages
                WHERE chat_id = ? AND project_id = ?
                  AND role IN ('user', 'assistant')
                  {date_clause}
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, project_id, *date_params, limit_value),
            )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "role": str(row["role"]),
            "content": str(row["content"] or ""),
        }
        for row in reversed(rows)
    ]


async def build_workspace_conversation_context(
    chat_id: str,
    current_message: str,
    *,
    project_id: int | None = None,
) -> str:
    messages = await _fetch_messages_for_index(chat_id, project_id)
    if not messages:
        return ""

    today = datetime.now(_BANGKOK).strftime("%Y-%m-%d")
    daily_index = _build_daily_index(messages)
    related = await _search_related_messages(
        chat_id, current_message, project_id=project_id, limit=8
    )

    lines = [
        "=== สารบัญบทสนทนาตามวัน (Telegram + Web รวมกัน) ===",
        f"วันนี้ ({_format_day_label(today)}): ใช้ timezone Asia/Bangkok",
        "",
        "กฎสำคัญ:",
        "- กบถามว่าเคยถามเรื่องอะไร / วันไหน → ตอบจากสารบัญและประวัติด้านล่าง",
        "- ห้ามบอกว่าเป็นบทสนทนาแรกหรือไม่มีประวัติ ถ้ามีรายการด้านล่าง",
        "- อ้างวันที่ชัดเจน (เช่น 23 พ.ค. 2026) เมื่อพูดถึงเรื่องเก่า",
        "",
    ]
    used = len("\n".join(lines))

    for day_key, topics in daily_index[:20]:
        label = _format_day_label(day_key)
        topic_text = "; ".join(_compact(t, 70) for t in topics)
        entry = f"- {label} ({day_key}): {topic_text}"
        if used + len(entry) > _MAX_CONTEXT_CHARS:
            break
        lines.append(entry)
        used += len(entry) + 1

    if related:
        lines.append("")
        lines.append("=== ข้อความเก่าที่เกี่ยวข้องกับคำถามนี้ ===")
        for row in related:
            day = str(row["local_at"])[:10]
            role = "กบ" if row["role"] == "user" else "AI"
            entry = (
                f"- [{_format_day_label(day)}] {role}: "
                f"{_compact(row['content'], 160)}"
            )
            if used + len(entry) > _MAX_CONTEXT_CHARS:
                break
            lines.append(entry)
            used += len(entry) + 1

    return "\n".join(lines)


async def index_workspace_message(
    *,
    message_id: int | None,
    chat_id: str,
    role: str,
    content: str,
    project_id: int | None,
) -> None:
    if not message_id or not content.strip():
        return
    await index_message_to_fts(
        source_table="messages",
        source_id=str(message_id),
        project_id=project_id,
        title=f"{role}:{chat_id}",
        content=content,
        tags="workspace;telegram",
    )
