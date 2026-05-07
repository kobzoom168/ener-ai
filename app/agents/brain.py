from datetime import date
from app.core.ai import chat_json
from app.core.database import get_db
from app.core.policy import AI_PERSONALITY


_CLASSIFY_SYSTEM = AI_PERSONALITY + """

งานของคุณ: วิเคราะห์ข้อความที่กบส่งมา แล้วตอบ JSON รูปแบบนี้:
{
  "category": "task|idea|question|feeling|random",
  "summary": "สรุป 1 ประโยค",
  "extracted_tasks": ["task 1", "task 2"],
  "is_confident": true
}

กฎ:
- category = task ถ้าข้อความมีสิ่งที่ต้องทำ
- category = idea ถ้าเป็นไอเดียหรือแผน
- category = question ถ้าเป็นคำถามที่ยังไม่มีคำตอบ
- category = feeling ถ้าเป็นความรู้สึก
- category = random ถ้าไม่เข้าพวกไหน
- extracted_tasks = list ของสิ่งที่ต้องทำ (ถ้าไม่มีให้เป็น [])
- is_confident = true เมื่อจัดหมวดได้ชัดเจนจริงๆ
- is_confident = false เมื่อไม่แน่ใจ หรือข้อความก้ำกึ่ง"""

_TASK_KEYWORDS = ["ต้อง", "เดี๋ยว", "วันนี้", "พรุ่งนี้", "deadline"]


def _looks_like_task(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in _TASK_KEYWORDS)


def _format_clarification(summary: str) -> str:
    return (
        f"📌 {summary}\n\n"
        "นี่คืออะไรครับ?\n"
        "1️⃣ task — ต้องทำ\n"
        "2️⃣ idea — ไอเดีย\n"
        "3️⃣ note — จดไว้เฉยๆ\n"
        "4️⃣ ไม่สำคัญ ข้ามได้"
    )


def _parse_clarification_reply(text: str) -> str | None:
    normalized = text.strip().lower()
    mapping = {
        "1": "task",
        "1️⃣": "task",
        "task": "task",
        "ต้องทำ": "task",
        "2": "idea",
        "2️⃣": "idea",
        "idea": "idea",
        "ไอเดีย": "idea",
        "3": "note",
        "3️⃣": "note",
        "note": "note",
        "จดไว้": "note",
        "จดไว้เฉยๆ": "note",
        "4": "skip",
        "4️⃣": "skip",
        "ข้าม": "skip",
        "ไม่สำคัญ": "skip",
    }
    return mapping.get(normalized)


async def get_pending_clarification(chat_id: str):
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT id, raw_text, detected_category
            FROM pending_clarification
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        )
        return await cursor.fetchone()


async def clear_pending_clarification(chat_id: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM pending_clarification WHERE chat_id = ?",
            (chat_id,),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("clarification_cleared", f"chat_id={chat_id}"),
        )
        await db.commit()


async def _save_note(text: str, category: str, summary: str, extracted_tasks: list[str]) -> str:
    task_titles = extracted_tasks[:]
    if category == "task" and not task_titles:
        task_titles = [summary]

    async with get_db() as db:
        await db.execute(
            "INSERT INTO notes (content, category, ai_summary) VALUES (?, ?, ?)",
            (text, category, summary),
        )

        today = date.today().isoformat()
        await db.execute(
            "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
            (today, "note", f"[{category}] {summary}"),
        )

        task_ids = []
        for task_title in task_titles:
            cursor = await db.execute(
                "INSERT INTO tasks (title) VALUES (?)",
                (task_title,),
            )
            task_ids.append(cursor.lastrowid)
            await db.execute(
                "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                (today, "task", f"สร้าง task: {task_title}"),
            )

        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("note_saved", f"category={category}"),
        )
        await db.commit()

    category_emoji = {
        "task": "✅",
        "idea": "💡",
        "question": "❓",
        "feeling": "💭",
        "note": "📝",
    }.get(category, "📝")

    lines = [f"📌 {summary}", "", f"{category_emoji} หมวด: {category}"]

    if task_titles:
        lines.append("")
        lines.append("🎯 Task ที่สร้างแล้ว:")
        for i, (tid, title) in enumerate(zip(task_ids, task_titles), 1):
            lines.append(f"  {i}. [{tid}] {title} 🟢")

    return "\n".join(lines)


async def _create_pending_clarification(chat_id: str, text: str, detected_category: str, summary: str) -> str:
    async with get_db() as db:
        await db.execute("DELETE FROM pending_clarification WHERE chat_id = ?", (chat_id,))
        await db.execute(
            """
            INSERT INTO pending_clarification (chat_id, raw_text, detected_category)
            VALUES (?, ?, ?)
            """,
            (chat_id, text, detected_category),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("clarification_requested", f"chat_id={chat_id} detected={detected_category}"),
        )
        await db.commit()
    return _format_clarification(summary)


async def process_note(text: str, chat_id: str) -> str:
    try:
        result = await chat_json(text, system=_CLASSIFY_SYSTEM, agent="brain")
        category = str(result.get("category", "random")).strip().lower()
        summary = str(result.get("summary", text[:50])).strip()
        extracted_tasks: list[str] = result.get("extracted_tasks", [])
        is_confident = bool(result.get("is_confident", category != "random"))
    except Exception:
        category = "random"
        summary = text[:80]
        extracted_tasks = []
        is_confident = False

    if _looks_like_task(text):
        category = "task"
        is_confident = True
        if not extracted_tasks:
            extracted_tasks = [summary]

    if category in {"task", "idea", "question", "feeling"} and is_confident:
        return await _save_note(text, category, summary, extracted_tasks)

    return await _create_pending_clarification(chat_id, text, category, summary)


async def handle_pending_reply(chat_id: str, reply_text: str) -> str | None:
    row = await get_pending_clarification(chat_id)
    if not row:
        return None

    action = _parse_clarification_reply(reply_text)
    if not action:
        return "📌 ตอบ 1, 2, 3 หรือ 4 ได้เลย\n\n1️⃣ task\n2️⃣ idea\n3️⃣ note\n4️⃣ ข้าม"

    if action == "skip":
        async with get_db() as db:
            await db.execute("DELETE FROM pending_clarification WHERE id = ?", (row["id"],))
            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                ("clarification_skipped", f"chat_id={chat_id}"),
            )
            await db.commit()
        return "📌 โอเค ข้ามข้อความนี้ให้แล้ว"

    summary = row["raw_text"][:80]
    result = await _save_note(row["raw_text"], action, summary, [summary] if action == "task" else [])

    async with get_db() as db:
        await db.execute("DELETE FROM pending_clarification WHERE id = ?", (row["id"],))
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("clarification_resolved", f"chat_id={chat_id} category={action}"),
        )
        await db.commit()

    return result
