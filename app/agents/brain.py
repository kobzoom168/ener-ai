from datetime import date
import re
from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.policy import build_system_prompt
from app.agents import task as task_agent


_CLASSIFY_SYSTEM = build_system_prompt("""

งานของพี่ตอนนี้: วิเคราะห์ข้อความที่กบส่งมา แล้วตอบ JSON รูปแบบนี้:
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
- is_confident = false เมื่อไม่แน่ใจ หรือข้อความก้ำกึ่ง""")

_TASK_KEYWORDS = ["ต้อง", "เดี๋ยว", "วันนี้", "พรุ่งนี้", "deadline"]


def _looks_like_task(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in _TASK_KEYWORDS)

def _extract_task_meta(result_text: str, fallback_title: str) -> tuple[str, str]:
    task_id_match = re.search(r"\[(\d+)\]", result_text)
    task_id = task_id_match.group(1) if task_id_match else "?"
    return task_id, fallback_title


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

        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("note_saved", f"category={category}"),
        )
        await db.commit()

    created_task_rows = []
    for task_title in task_titles:
        task_result = await task_agent.create_task(task_title, _agent_triggered_by="agent")
        task_id, parsed_title = _extract_task_meta(task_result, task_title)
        created_task_rows.append((task_id, parsed_title))

    category_emoji = {
        "task": "✅",
        "idea": "💡",
        "question": "❓",
        "feeling": "💭",
        "note": "📝",
    }.get(category, "📝")

    lines = [f"📌 {summary}", "", f"{category_emoji} หมวด: {category}"]

    if created_task_rows:
        lines.append("")
        lines.append("🎯 Task ที่สร้างแล้ว:")
        for i, (tid, title) in enumerate(created_task_rows, 1):
            lines.append(f"  {i}. [{tid}] {title} 🟢")

    return "\n".join(lines)


@log_agent_run("NoteAgent")
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

    if category not in {"task", "idea", "question", "feeling", "note"}:
        category = "note"
    elif not is_confident and category != "task":
        category = "note"

    return await _save_note(text, category, summary, extracted_tasks)
