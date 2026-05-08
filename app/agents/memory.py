from datetime import date
from uuid import uuid4
from app.core.database import get_db
from app.core.memory import (
    forget_long_term_memory,
    list_long_term_memories,
    remember_long_term_memory,
)


async def search_memory(query: str) -> str:
    like_query = f"%{query}%"

    async with get_db() as db:
        notes_cursor = await db.execute(
            """
            SELECT id, content, category
            FROM notes
            WHERE content LIKE ? OR ai_summary LIKE ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (like_query, like_query),
        )
        notes_rows = await notes_cursor.fetchall()

        tasks_cursor = await db.execute(
            """
            SELECT id, title, status, priority
            FROM tasks
            WHERE title LIKE ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (like_query,),
        )
        task_rows = await tasks_cursor.fetchall()

        lessons_cursor = await db.execute(
            """
            SELECT id, mistake, reason, lesson
            FROM lessons_learned
            WHERE mistake LIKE ? OR reason LIKE ? OR lesson LIKE ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (like_query, like_query, like_query),
        )
        lesson_rows = await lessons_cursor.fetchall()

        long_term_cursor = await db.execute(
            """
            SELECT id, content, memory_type
            FROM long_term_memories
            WHERE content LIKE ?
            ORDER BY id DESC
            LIMIT 5
            """,
            (like_query,),
        )
        long_term_rows = await long_term_cursor.fetchall()

        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("memory_searched", f"query={query}"),
        )
        await db.commit()

    if not any([notes_rows, task_rows, lesson_rows, long_term_rows]):
        return f"📌 ไม่เจอข้อมูลที่เกี่ยวกับ '{query}'"

    lines = [f"📌 ผลค้นหาความทรงจำสำหรับ: {query}", ""]

    lines.append(f"📝 NOTES ({len(notes_rows)})")
    if notes_rows:
        for row in notes_rows:
            lines.append(f"· [{row['id']}] ({row['category']}) {row['content']}")
    else:
        lines.append("· ไม่มี")

    lines.extend(["", f"✅ TASKS ({len(task_rows)})"])
    if task_rows:
        for row in task_rows:
            lines.append(f"· [{row['id']}] {row['title']} | {row['status']} | {row['priority']}")
    else:
        lines.append("· ไม่มี")

    lines.extend(["", f"🔁 LESSONS ({len(lesson_rows)})"])
    if lesson_rows:
        for row in lesson_rows:
            detail = f" | เพราะ: {row['reason']}" if row["reason"] else ""
            lines.append(f"· [{row['id']}] {row['mistake']} -> {row['lesson']}{detail}")
    else:
        lines.append("· ไม่มี")

    lines.extend(["", f"🧠 LONG-TERM ({len(long_term_rows)})"])
    if long_term_rows:
        for row in long_term_rows:
            lines.append(f"· [{row['id']}] ({row['memory_type']}) {row['content']}")
    else:
        lines.append("· ไม่มี")

    return "\n".join(lines)


async def park_idea(text: str) -> str:
    memory_key = str(uuid4())

    async with get_db() as db:
        await db.execute(
            "INSERT INTO memories (key, value, tag) VALUES (?, ?, ?)",
            (memory_key, text, "parked"),
        )
        await db.execute(
            "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
            (date.today().isoformat(), "idea", f"parked: {text}"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("idea_parked", f"key={memory_key}"),
        )
        await db.commit()

    return f"📌 เก็บไอเดียไว้แล้ว\n\n💾 key: {memory_key}\n💡 {text}"


async def remember_memory(text: str) -> str:
    return await remember_long_term_memory(text, memory_type="manual")


async def forget_memory(keyword: str) -> str:
    return await forget_long_term_memory(keyword)


async def list_memory() -> str:
    return await list_long_term_memories()
