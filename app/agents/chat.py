from app.core.ai import chat
from app.core.database import get_db
from app.core.policy import AI_PERSONALITY

_CHAT_SYSTEM = AI_PERSONALITY + """

หน้าที่:
- คุยกับกบเหมือนผู้ช่วยส่วนตัวแบบ conversational
- ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
- พูดเหมือนคุยกับกบตรงๆ แบบ GPT/Claude ที่เป็นผู้ช่วยส่วนตัว
- ตอบเป็นข้อความธรรมดาเท่านั้น ไม่ต้องตอบเป็น JSON"""


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
    reply = (
        await chat(
        text,
        system=_CHAT_SYSTEM,
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

    return f"📌 {reply}"
