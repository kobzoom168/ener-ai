"""Build grounded local context before AI call."""
from __future__ import annotations

from app.core.artifact_memory import get_recent_project_artifacts
from app.core.database import get_db

_CONTEXT_LIMIT_CHARS = 6000


def _clip(text: str, max_len: int) -> str:
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _route_hints(route: dict) -> str:
    return " ".join(
        str(route.get(k, "")).strip().lower()
        for k in ("domain", "reason", "complexity", "intent")
    ).strip()


def _should_include_project_artifacts(text: str, route: dict) -> bool:
    route_hint = _route_hints(route)
    if any(
        k in route_hint
        for k in ("ener_scan", "business", "analysis", "content", "strategy")
    ):
        return True
    lowered = str(text or "").lower()
    return any(
        k in lowered
        for k in (
            "scan",
            "report",
            "payment",
            "ener scan",
            "ener-scan",
            "artifact",
            "event",
            "สแกน",
            "รายงาน",
            "ชำระ",
        )
    )


def _extract_keywords(text: str) -> list[str]:
    words = []
    for raw in str(text or "").split():
        w = raw.strip().lower()
        if len(w) >= 3 and w not in words:
            words.append(w)
        if len(words) >= 6:
            break
    return words


async def build_context_v2(
    text: str,
    route: dict,
    conversation_id: str | None = None,
    chat_id: str | None = None,
    project_id: int | None = None,
    limit_messages: int = 8,
) -> dict:
    route_hint = _route_hints(route)
    keywords = _extract_keywords(text)
    sources: list[dict] = []
    sections: list[str] = []
    needs_external = False

    async with get_db() as db:
        # Recent messages (always include, no simple-chat bypass)
        msg_cols_cur = await db.execute("PRAGMA table_info(messages)")
        msg_cols = {str(r["name"]) for r in await msg_cols_cur.fetchall()}
        msg_rows = []
        if conversation_id and "conversation_id" in msg_cols:
            cur = await db.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE conversation_id = ? AND role IN ('user','assistant')
                ORDER BY id DESC
                LIMIT ?
                """,
                (conversation_id, max(1, int(limit_messages))),
            )
            msg_rows = await cur.fetchall()
        elif chat_id:
            cur = await db.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE chat_id = ? AND role IN ('user','assistant')
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, max(1, int(limit_messages))),
            )
            msg_rows = await cur.fetchall()
        if msg_rows:
            msg_lines = []
            for row in reversed(msg_rows):
                role = "User" if str(row["role"]) == "user" else "Assistant"
                preview = _clip(row["content"], 180)
                msg_lines.append(f"- {role}: {preview}")
                sources.append(
                    {"type": "recent_message", "id": str(row["id"]), "preview": _clip(preview, 120)}
                )
            sections.append("## บทสนทนาล่าสุด\n" + "\n".join(msg_lines))

        # Long-term memories by keyword LIKE
        memory_rows = []
        if keywords:
            clauses = " OR ".join(["LOWER(content) LIKE ?"] * len(keywords))
            params = [f"%{k}%" for k in keywords]
            cur = await db.execute(
                f"""
                SELECT id, content
                FROM long_term_memories
                WHERE {clauses}
                ORDER BY id DESC
                LIMIT 5
                """,
                params,
            )
            memory_rows = await cur.fetchall()
        if memory_rows:
            lines = []
            for row in memory_rows:
                preview = _clip(row["content"], 200)
                lines.append(f"- {preview}")
                sources.append({"type": "long_term_memory", "id": str(row["id"]), "preview": _clip(preview, 120)})
            sections.append("## ความจำระยะยาวที่เกี่ยวข้อง\n" + "\n".join(lines))

        # Open tasks
        task_related = any(k in route_hint for k in ("chat", "task", "analysis"))
        text_related = any(k in str(text).lower() for k in ("task", "งาน", "todo", "ติดตาม", "plan", "deadline"))
        if task_related or text_related:
            cur = await db.execute(
                """
                SELECT id, title, priority, status
                FROM tasks
                WHERE status IN ('open', 'in_progress')
                ORDER BY id DESC
                LIMIT 6
                """
            )
            task_rows = await cur.fetchall()
            if task_rows:
                lines = []
                for row in task_rows:
                    preview = f"[{row['priority']}] {row['title']} ({row['status']})"
                    lines.append(f"- {preview}")
                    sources.append({"type": "task", "id": str(row["id"]), "preview": _clip(preview, 120)})
                sections.append("## งานที่ยังต้องติดตาม\n" + "\n".join(lines))

        # Uploaded docs
        docs_related = any(k in route_hint for k in ("hospital", "vendor", "analysis", "code"))
        if docs_related:
            cur = await db.execute(
                """
                SELECT id, filename, summary
                FROM uploads
                WHERE summary IS NOT NULL AND TRIM(summary) <> ''
                ORDER BY id DESC
                LIMIT 4
                """
            )
            upload_rows = await cur.fetchall()
            if upload_rows:
                lines = []
                for row in upload_rows:
                    preview = _clip(row["summary"], 220)
                    lines.append(f"- {row['filename']}: {preview}")
                    sources.append({"type": "doc", "id": str(row["id"]), "preview": _clip(preview, 120)})
                sections.append("## สรุปเอกสารอัปโหลด\n" + "\n".join(lines))

        # Hospital/vendor/infrastructure projects
        project_related = any(k in route_hint for k in ("hospital", "vendor", "analysis", "infrastructure"))
        if project_related:
            proj_sql = """
                SELECT id, name, status, percent_complete, current_status
                FROM standup_projects
                WHERE is_active = 1
            """
            params: tuple = ()
            if project_id is not None:
                proj_sql += " AND id = ?"
                params = (project_id,)
            proj_sql += " ORDER BY sort_order, id LIMIT 5"
            cur = await db.execute(proj_sql, params)
            project_rows = await cur.fetchall()
            if project_rows:
                lines = []
                for row in project_rows:
                    preview = (
                        f"{row['name']} [{row['status']}] {row['percent_complete']}% "
                        f"- {_clip(row['current_status'], 100)}"
                    )
                    lines.append(f"- {preview}")
                    sources.append({"type": "project", "id": str(row["id"]), "preview": _clip(preview, 120)})
                sections.append("## โครงการโรงพยาบาล/วิเคราะห์ที่เกี่ยวข้อง\n" + "\n".join(lines))

        if _should_include_project_artifacts(text, route):
            artifacts = await get_recent_project_artifacts(
                project_slug="ener-scan",
                limit=5,
            )
            if artifacts:
                lines = []
                for art in artifacts:
                    preview = _clip(
                        f"[{art.get('artifact_type')}] {art.get('title')}: {art.get('summary')}",
                        220,
                    )
                    lines.append(f"- {preview}")
                    sources.append(
                        {
                            "type": "project_artifact",
                            "id": str(art.get("id")),
                            "preview": _clip(preview, 120),
                        }
                    )
                sections.append("## เหตุการณ์ Ener Scan ล่าสุด\n" + "\n".join(lines))

    if not sections:
        sections.append("## บริบทท้องถิ่น\n- ยังไม่พบข้อมูลเฉพาะในฐานข้อมูลสำหรับข้อความนี้")

    warning = (
        "ใช้ข้อมูล local context เป็นหลัก ห้ามแต่ง vendor/ราคา/ชื่อ/เบอร์/URL "
        "ถ้าไม่มีใน context หรือ tool result"
    )
    full_text = (
        "=== Local Context V2 ===\n"
        + "\n\n".join(sections)
        + "\n\n⚠️ "
        + warning
        + "\n"
    )
    full_text = _clip(full_text, _CONTEXT_LIMIT_CHARS)

    summary = (
        f"sources={len(sources)} "
        f"recent={sum(1 for s in sources if s['type'] == 'recent_message')} "
        f"memory={sum(1 for s in sources if s['type'] == 'long_term_memory')} "
        f"tasks={sum(1 for s in sources if s['type'] == 'task')}"
    )

    return {
        "text": full_text,
        "sources": sources,
        "summary": summary,
        "needs_external": needs_external,
    }


async def build_context(text: str, route: dict) -> str:
    ctx = await build_context_v2(text=text, route=route)
    return str(ctx.get("text") or "")
