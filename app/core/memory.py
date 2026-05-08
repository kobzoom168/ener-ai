from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.ai import chat_json
from app.core.database import get_db
from app.core.policy import BASE_SYSTEM_PROMPT

_BANGKOK = ZoneInfo("Asia/Bangkok")
_MAX_CONTEXT_CHARS = 1800
_SUMMARY_CHAR_LIMIT = 100
_THAI_WEEKDAYS = [
    "วันจันทร์",
    "วันอังคาร",
    "วันพุธ",
    "วันพฤหัสบดี",
    "วันศุกร์",
    "วันเสาร์",
    "วันอาทิตย์",
]
_THAI_MONTHS = [
    "",
    "มกราคม",
    "กุมภาพันธ์",
    "มีนาคม",
    "เมษายน",
    "พฤษภาคม",
    "มิถุนายน",
    "กรกฎาคม",
    "สิงหาคม",
    "กันยายน",
    "ตุลาคม",
    "พฤศจิกายน",
    "ธันวาคม",
]

_MEMORY_EXTRACT_SYSTEM = BASE_SYSTEM_PROMPT + """

งานของคุณ: อ่านข้อความผู้ใช้และคำตอบของผู้ช่วย แล้วดึงเฉพาะข้อมูลระยะยาวที่ควรจำเกี่ยวกับกบ

ให้ดึงเฉพาะข้อมูลที่คงอยู่ข้ามวันและมีประโยชน์จริง เช่น:
- ชื่อ / ที่ทำงาน / ครอบครัว
- ความชอบ / ไม่ชอบ
- เป้าหมาย / แผน
- ข้อมูลธุรกิจ Ener

กฎ:
- ถ้าไม่มีข้อมูลสำคัญ ให้ memories เป็น []
- แต่ละ memory ต้องสั้น กระชับ และเป็นประโยคที่อ่านรู้เรื่องทันที
- ห้ามใส่ข้อมูลชั่วคราวที่หมดอายุเร็ว
- ตอบเป็น JSON เท่านั้น

รูปแบบ:
{
  "memories": [
    {"content": "กบทำงานที่ Bumrungrad เป็น IT PM", "memory_type": "profile"}
  ]
}"""


def _compact(text: str, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _append_with_budget(lines: list[str], entry: str, used_chars: int) -> int:
    if used_chars + len(entry) + 1 > _MAX_CONTEXT_CHARS:
        return used_chars
    lines.append(entry)
    return used_chars + len(entry) + 1


def get_time_context() -> str:
    now = datetime.now(_BANGKOK)
    weekday = _THAI_WEEKDAYS[now.weekday()]
    month = _THAI_MONTHS[now.month]
    return f"{weekday}ที่ {now.day} {month} {now.year} เวลา {now.hour:02d}:{now.minute:02d} น."


async def get_long_term_context() -> str:
    async with get_db() as db:
        belief_rows = await (
            await db.execute(
                "SELECT topic, belief FROM beliefs ORDER BY created_at DESC LIMIT 20"
            )
        ).fetchall()
        long_term_rows = await (
            await db.execute(
                "SELECT content FROM long_term_memories ORDER BY created_at DESC LIMIT 20"
            )
        ).fetchall()
        task_rows = await (
            await db.execute(
                """
                SELECT title, priority
                FROM tasks
                WHERE status = 'open'
                ORDER BY
                    CASE priority
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    id
                LIMIT 10
                """
            )
        ).fetchall()

    lines = [
        get_time_context(),
        "",
        "=== สิ่งที่รู้เกี่ยวกับกบ ===",
    ]
    used_chars = len("\n".join(lines))

    if belief_rows:
        for row in belief_rows:
            entry = f"- {_compact(row['topic'], 24)}: {_compact(row['belief'], 90)}"
            used_chars = _append_with_budget(lines, entry, used_chars)
    else:
        used_chars = _append_with_budget(lines, "- ยังไม่มี", used_chars)

    used_chars = _append_with_budget(lines, "", used_chars)
    used_chars = _append_with_budget(lines, "=== ความจำระยะยาว ===", used_chars)
    if long_term_rows:
        for row in long_term_rows:
            entry = f"- {_compact(row['content'], 100)}"
            used_chars = _append_with_budget(lines, entry, used_chars)
    else:
        used_chars = _append_with_budget(lines, "- ยังไม่มี", used_chars)

    used_chars = _append_with_budget(lines, "", used_chars)
    used_chars = _append_with_budget(lines, "=== Task ที่ยังค้างอยู่ ===", used_chars)
    if task_rows:
        for row in task_rows:
            entry = f"- [{row['priority']}] {_compact(row['title'], 80)}"
            used_chars = _append_with_budget(lines, entry, used_chars)
    else:
        used_chars = _append_with_budget(lines, "- ไม่มี", used_chars)

    return "\n".join(lines)


async def get_recent_summaries() -> str:
    async with get_db() as db:
        rows = await (
            await db.execute(
                """
                SELECT period_start, content
                FROM digests
                WHERE digest_type = 'daily'
                ORDER BY period_start DESC
                LIMIT 7
                """
            )
        ).fetchall()

    if not rows:
        return "- ยังไม่มี daily summary"

    lines = []
    for row in rows:
        lines.append(f"- {row['period_start']}: {_compact(row['content'], _SUMMARY_CHAR_LIMIT)}")
    return "\n".join(lines)


async def remember_long_term_memory(text: str, memory_type: str = "manual") -> str:
    content = _compact(text, 240)
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO long_term_memories (content, memory_type)
            SELECT ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM long_term_memories WHERE lower(content) = lower(?)
            )
            """,
            (content, memory_type, content),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("long_term_memory_saved", f"type={memory_type}"),
        )
        await db.commit()
    if (cursor.rowcount or 0) <= 0:
        return f"📌 เรื่องนี้มีอยู่ใน memory แล้ว\n\n🧠 {content}"
    return f"📌 จำเรื่องนี้ไว้แล้ว\n\n🧠 {content}"


async def forget_long_term_memory(keyword: str) -> str:
    like_keyword = f"%{keyword}%"
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM long_term_memories WHERE content LIKE ?",
            (like_keyword,),
        )
        deleted = cursor.rowcount or 0
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("long_term_memory_deleted", f"keyword={keyword} count={deleted}"),
        )
        await db.commit()
    if deleted <= 0:
        return f"📌 ไม่เจอ memory ที่มีคำว่า {keyword}"
    return f"📌 ลบ memory ที่มีคำว่า {keyword} แล้ว {deleted} รายการ"


async def list_long_term_memories() -> str:
    async with get_db() as db:
        rows = await (
            await db.execute(
                """
                SELECT id, content, memory_type
                FROM long_term_memories
                ORDER BY created_at DESC, id DESC
                LIMIT 50
                """
            )
        ).fetchall()
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("long_term_memory_viewed", f"count={len(rows)}"),
        )
        await db.commit()

    if not rows:
        return "📌 ยังไม่มี long-term memory"

    lines = [f"📌 Long-term memory ({len(rows)} รายการ)", ""]
    for row in rows:
        lines.append(f"· [{row['id']}] ({row['memory_type']}) {row['content']}")
    return "\n".join(lines)


async def extract_and_store_long_term_memories(text: str, reply: str) -> int:
    try:
        result = await chat_json(
            f"ข้อความผู้ใช้:\n{text}\n\nคำตอบผู้ช่วย:\n{reply}",
            system=_MEMORY_EXTRACT_SYSTEM,
            agent="memory",
        )
    except Exception:
        return 0

    memories = result.get("memories", [])
    saved = 0
    async with get_db() as db:
        for item in memories:
            content = _compact(str(item.get("content", "")).strip(), 240)
            memory_type = _compact(str(item.get("memory_type", "auto")).strip() or "auto", 24)
            if not content:
                continue
            cursor = await db.execute(
                """
                INSERT INTO long_term_memories (content, memory_type)
                SELECT ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM long_term_memories WHERE lower(content) = lower(?)
                )
                """,
                (content, memory_type, content),
            )
            if (cursor.rowcount or 0) > 0:
                saved += 1

        if saved > 0:
            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                ("long_term_memory_auto_saved", f"count={saved}"),
            )
        await db.commit()
    return saved
