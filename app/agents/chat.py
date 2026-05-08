from datetime import date
from app.core.ai import chat, chat_json
from app.core.database import get_db
from app.core.memory import (
    extract_and_store_long_term_memories,
    get_long_term_context,
    get_recent_summaries,
    get_time_context,
)
from app.core.policy import AI_PERSONALITY

_TASK_EXTRACT_SYSTEM = AI_PERSONALITY + """

งานของคุณ: อ่านข้อความผู้ใช้และคำตอบของผู้ช่วย แล้วดึงเฉพาะ task ที่ควรสร้างจริง

กฎ:
- สร้าง task เฉพาะสิ่งที่เป็น action item ชัดเจน
- ถ้าไม่มี task ให้ตอบ tasks เป็น []
- ตอบเป็น JSON เท่านั้น

รูปแบบ:
{
  "tasks": ["task 1", "task 2"]
}"""


async def _extract_tasks(text: str, reply: str) -> list[str]:
    try:
        result = await chat_json(
            f"ข้อความผู้ใช้:\n{text}\n\nคำตอบผู้ช่วย:\n{reply}",
            system=_TASK_EXTRACT_SYSTEM,
            agent="chat",
        )
    except Exception:
        return []

    tasks = []
    seen = set()
    for item in result.get("tasks", []):
        task_text = str(item).strip()
        if not task_text:
            continue
        if task_text in seen:
            continue
        seen.add(task_text)
        tasks.append(task_text)
    return tasks


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
    time_context = get_time_context()
    long_term = await get_long_term_context()
    summaries = await get_recent_summaries()
    system_prompt = AI_PERSONALITY + f"""

{time_context}

{long_term}

=== สรุปบทสนทนาล่าสุด 7 วัน ===
{summaries}

หมายเหตุ: ข้อมูลเหล่านี้คือสิ่งที่กบบอกไว้ก่อนหน้า จำและใช้ตอบได้เลย

หน้าที่:
- คุยกับกบเหมือนผู้ช่วยส่วนตัวแบบ conversational
- ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
- พูดเหมือนคุยกับกบตรงๆ แบบ GPT/Claude ที่เป็นผู้ช่วยส่วนตัว
- ตอบเป็นข้อความธรรมดาเท่านั้น ไม่ต้องตอบเป็น JSON"""
    reply = (
        await chat(
            text,
            system=system_prompt,
            agent="chat",
            messages=history,
        )
    ).strip() or "ยังไม่มีคำตอบตอนนี้"

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

    await extract_and_store_long_term_memories(text, reply)
    created_tasks = await _create_tasks(await _extract_tasks(text, reply))
    if not created_tasks:
        return f"📌 {reply}"

    lines = [f"📌 {reply}", "", "🎯 Task ที่สร้างจากบทสนทนา:"]
    for task_title in created_tasks:
        lines.append(f"· {task_title}")
    return "\n".join(lines)
