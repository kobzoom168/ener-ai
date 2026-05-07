from datetime import date
from app.core.ai import chat_json, chat
from app.core.database import get_db
from app.core.policy import AI_PERSONALITY


_CLASSIFY_SYSTEM = AI_PERSONALITY + """

งานของคุณ: วิเคราะห์ข้อความที่กบส่งมา แล้วตอบ JSON รูปแบบนี้:
{
  "category": "task|idea|question|feeling|random",
  "summary": "สรุป 1 ประโยค",
  "extracted_tasks": ["task 1", "task 2"]
}

กฎ:
- category = task ถ้าข้อความมีสิ่งที่ต้องทำ
- category = idea ถ้าเป็นไอเดียหรือแผน
- category = question ถ้าเป็นคำถามที่ยังไม่มีคำตอบ
- category = feeling ถ้าเป็นความรู้สึก
- category = random ถ้าไม่เข้าพวกไหน
- extracted_tasks = list ของสิ่งที่ต้องทำ (ถ้าไม่มีให้เป็น [])"""


async def process_note(text: str) -> str:
    try:
        result = await chat_json(text, system=_CLASSIFY_SYSTEM)
        category = result.get("category", "random")
        summary = result.get("summary", text[:50])
        extracted_tasks: list[str] = result.get("extracted_tasks", [])
    except Exception:
        category = "random"
        summary = text[:80]
        extracted_tasks = []

    async with await get_db() as db:
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
        for task_title in extracted_tasks:
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
        "task": "✅", "idea": "💡", "question": "❓",
        "feeling": "💭", "random": "📝",
    }.get(category, "📝")

    lines = [f"📌 {summary}", f"", f"{category_emoji} หมวด: {category}"]

    if extracted_tasks:
        lines.append("")
        lines.append("🎯 Task ที่สร้างแล้ว:")
        for i, (tid, title) in enumerate(zip(task_ids, extracted_tasks), 1):
            lines.append(f"  {i}. [{tid}] {title} 🟢")

    return "\n".join(lines)
