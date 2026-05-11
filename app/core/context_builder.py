"""Pulls grounded context from SQLite before AI call."""
from app.core.database import get_db


async def build_context(text: str, route: dict) -> str:
    t = text.lower()
    complexity = route.get("complexity", "simple")
    reason = route.get("reason", "")
    sections = []

    if complexity == "simple":
        return ""

    async with get_db() as db:
        cur = await db.execute(
            """SELECT title, priority, status FROM tasks
               WHERE status='open' ORDER BY id DESC LIMIT 5"""
        )
        tasks = await cur.fetchall()
    if tasks:
        lines = [f"- [{r['priority']}] {r['title']}" for r in tasks]
        sections.append("📋 Open Tasks:\n" + "\n".join(lines))

    if "hospital" in reason or "vendor" in reason or "analysis" in reason:
        async with get_db() as db:
            cur = await db.execute(
                """SELECT name, percent_complete, status,
                          current_status, due_date
                   FROM standup_projects WHERE is_active=1
                   ORDER BY sort_order LIMIT 5"""
            )
            projects = await cur.fetchall()
        if projects:
            lines = [
                f"- {p['name']} ({p['percent_complete']}%)"
                f" [{p['status']}]: {p['current_status']}"
                f" (due: {p['due_date']})"
                for p in projects
            ]
            sections.append("🏥 Active IT Projects:\n" + "\n".join(lines))

        async with get_db() as db:
            cur = await db.execute(
                """SELECT filename, summary FROM uploads
                   WHERE summary IS NOT NULL
                   ORDER BY id DESC LIMIT 3"""
            )
            uploads = await cur.fetchall()
        if uploads:
            lines = [
                f"- {r['filename']}: {r['summary'][:200]}"
                for r in uploads
            ]
            sections.append("📁 Uploaded Docs:\n" + "\n".join(lines))

    keywords = [w for w in t.split() if len(w) > 3][:5]
    if keywords and complexity in ("complex", "critical"):
        clauses = []
        params = []
        for keyword in keywords:
            clauses.append("content LIKE ?")
            params.append(f"%{keyword}%")
        async with get_db() as db:
            cur = await db.execute(
                f"""SELECT content FROM long_term_memories
                    WHERE {' OR '.join(clauses)}
                    ORDER BY id DESC LIMIT 5""",
                params,
            )
            memories = await cur.fetchall()
        if memories:
            lines = [f"- {r['content']}" for r in memories]
            sections.append("🧠 Related Memory:\n" + "\n".join(lines))

    if not sections:
        return ""

    return (
        "\n\n=== Grounded Context (ข้อมูลจริงของกบ) ===\n"
        + "\n\n".join(sections)
        + "\n\n⚠️ ตอบโดยอ้างอิงข้อมูลด้านบนเท่านั้น"
        " อย่าแต่ง vendor/ราคา/ชื่อ/เบอร์ขึ้นเอง\n"
    )
