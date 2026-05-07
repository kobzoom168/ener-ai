from datetime import date
from app.core.ai import chat_json
from app.core.database import get_db
from app.core.policy import AI_PERSONALITY

_CHAT_SYSTEM = AI_PERSONALITY + """

หน้าที่:
- คุยกับกบเหมือนผู้ช่วยส่วนตัวแบบ conversational
- ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
- พูดเหมือนคุยกับกบตรงๆ แบบ GPT/Claude ที่เป็นผู้ช่วยส่วนตัว
- ถ้าในคำตอบของคุณมีสิ่งที่ควรทำต่อ ให้ใส่ลง extracted_tasks

ตอบเป็น JSON รูปแบบนี้:
{
  "reply": "ข้อความตอบกลับภาษาไทย",
  "extracted_tasks": ["task 1", "task 2"]
}"""


async def _create_tasks(task_titles: list[str]) -> list[str]:
    created = []
    if not task_titles:
        return created

    async with get_db() as db:
        today = date.today().isoformat()
        for task_title in task_titles:
            await db.execute(
                "INSERT INTO tasks (title) VALUES (?)",
                (task_title,),
            )
            await db.execute(
                "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                (today, "task", f"สร้าง task จาก chat: {task_title}"),
            )
            created.append(task_title)
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("chat_tasks_created", f"count={len(created)}"),
        )
        await db.commit()
    return created


async def run_chat(chat_id: str, text: str) -> str:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (chat_id,),
        )
        rows = await cursor.fetchall()

    history = [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]
    result = await chat_json(
        text,
        system=_CHAT_SYSTEM,
        agent="chat",
        messages=history,
    )
    reply = str(result.get("reply", "ยังไม่มีคำตอบตอนนี้")).strip()
    extracted_tasks = [str(item).strip() for item in result.get("extracted_tasks", []) if str(item).strip()]

    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "user", text),
        )
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "assistant", reply),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("chat_message_saved", f"chat_id={chat_id}"),
        )
        await db.commit()

    created_tasks = await _create_tasks(extracted_tasks)
    if not created_tasks:
        return f"📌 {reply}"

    lines = [f"📌 {reply}", "", "🎯 Task ที่สร้างจากบทสนทนา:"]
    for task_title in created_tasks:
        lines.append(f"· {task_title}")
    return "\n".join(lines)
