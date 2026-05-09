import re

from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

EXTRACT_SYSTEM = build_system_prompt("""
งานของพี่: อ่าน chat messages แล้วสกัดข้อมูลสำคัญเกี่ยวกับกบ

สกัดเฉพาะ:
- ข้อมูลส่วนตัว (ชื่อ ที่อยู่ ครอบครัว สัตว์เลี้ยง)
- ความชอบ/ไม่ชอบ
- สุขภาพ/อาหาร
- งาน/โปรเจกต์
- ความเชื่อ/ค่านิยม
- เป้าหมาย/แผน

ไม่ต้องสกัด:
- คำถามทั่วไป
- การสนทนาที่ไม่เกี่ยวกับกบ
- ข้อมูลชั่วคราว
- ข้อมูล sensitive เช่น password, token, api key, secret

ตอบ JSON:
{
  "memories": [
    {
      "content": "กบมีหมาบอสตันเทอร์เรียร์ชื่อแบล็คแมน อายุ 1 ปี",
      "category": "personal|preference|health|work|belief|goal",
      "confidence": 0.9
    }
  ]
}
""")

DEDUP_SYSTEM = build_system_prompt("""
งานของพี่: ตรวจสอบ memories ว่ามีซ้ำหรือขัดแย้งกันไหม

ตอบ JSON:
{
  "keep": [1, 2],
  "delete": [3],
  "merge": [{"keep_id": 1, "delete_id": 4, "reason": "ข้อมูลเดียวกัน"}]
}
""")

_SENSITIVE_PATTERNS = [
    r"password",
    r"passwd",
    r"token",
    r"api[_\-\s]?key",
    r"secret",
    r"bearer\s+[a-z0-9\-_\.]+",
    r"otp",
    r"pin",
    r"รหัสผ่าน",
    r"โทเคน",
    r"คีย์ลับ",
]


def _is_sensitive_text(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered, re.IGNORECASE) for pattern in _SENSITIVE_PATTERNS)


def _normalize_category(value: object) -> str:
    category = str(value or "").strip().lower()
    if category in {"personal", "preference", "health", "work", "belief", "goal"}:
        return category
    return "general"


async def _log_memory_keeper_event(
    event_type: str,
    summary: str,
    result: str,
    learned: str | None = None,
) -> None:
    try:
        await log_event(
            agent_name="MemoryKeeper",
            event_type=event_type,
            summary=summary,
            tags=["memory", "memory-keeper"],
            result=result,
            learned=learned,
        )
    except Exception:
        pass


async def extract_from_recent_messages(chat_id: str, limit: int = 50) -> int:
    """สกัด memories จาก messages ล่าสุด"""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = await cursor.fetchall()

    if not rows:
        return 0

    conv_text = "\n".join(
        f"{row['role']}: {str(row['content'])[:200]}"
        for row in reversed(rows)
    )

    try:
        result = await chat_json(
            conv_text,
            system=EXTRACT_SYSTEM,
            agent="memorykeeper",
            preferred_model="groq",
            strict_model=True,
        )
    except Exception as exc:
        await _log_memory_keeper_event(
            "task_failed",
            f"extract recent messages fail: {chat_id}",
            "failure",
            learned=str(exc)[:200],
        )
        return 0

    memories = result.get("memories", [])
    saved = 0

    async with get_db() as db:
        for mem in memories:
            try:
                confidence = float(mem.get("confidence", 0) or 0)
            except Exception:
                confidence = 0
            if confidence < 0.7:
                continue

            content = " ".join(str(mem.get("content", "")).split()).strip()
            if not content or _is_sensitive_text(content):
                continue

            category = _normalize_category(mem.get("category"))

            cursor = await db.execute(
                "SELECT id FROM long_term_memories WHERE content LIKE ? LIMIT 1",
                (f"%{content[:30]}%",),
            )
            existing = await cursor.fetchone()
            if existing:
                continue

            await db.execute(
                """
                INSERT INTO long_term_memories (content, memory_type)
                VALUES (?, ?)
                """,
                (content, category),
            )
            saved += 1

        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("memory_keeper_extract", f"chat_id={chat_id} saved={saved} scanned={len(rows)}"),
        )
        await db.commit()

    if saved > 0:
        await _log_memory_keeper_event(
            "task_done",
            f"extract recent messages: {chat_id}",
            "success",
            learned=f"saved={saved}",
        )
    return saved


async def dedup_memories() -> int:
    """ลบ memories ที่ซ้ำหรือขัดแย้ง"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, content FROM long_term_memories ORDER BY id"
        )
        rows = await cursor.fetchall()

    if len(rows) < 2:
        return 0

    mem_list = "\n".join(f"ID {row['id']}: {row['content']}" for row in rows)

    try:
        result = await chat_json(
            f"memories:\n{mem_list}",
            system=DEDUP_SYSTEM,
            agent="memorykeeper",
            preferred_model="groq",
            strict_model=True,
        )
    except Exception as exc:
        await _log_memory_keeper_event(
            "task_failed",
            "dedup memories fail",
            "failure",
            learned=str(exc)[:200],
        )
        return 0

    raw_delete_ids = []
    raw_delete_ids.extend(result.get("delete", []) or [])
    for merge_item in result.get("merge", []) or []:
        raw_delete_ids.append(merge_item.get("delete_id"))

    delete_ids: list[int] = []
    for value in raw_delete_ids:
        try:
            parsed = int(value)
        except Exception:
            continue
        if parsed not in delete_ids:
            delete_ids.append(parsed)

    if not delete_ids:
        return 0

    async with get_db() as db:
        for delete_id in delete_ids:
            await db.execute(
                "DELETE FROM long_term_memories WHERE id = ?",
                (delete_id,),
            )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("memory_keeper_dedup", f"deleted={len(delete_ids)}"),
        )
        await db.commit()

    await _log_memory_keeper_event(
        "task_done",
        "dedup memories",
        "success",
        learned=f"deleted={len(delete_ids)}",
    )
    return len(delete_ids)


@log_agent_run("MemoryKeeper")
async def run_memory_keeper(chat_id: str) -> str:
    """รัน full cycle: extract + dedup"""
    saved = await extract_from_recent_messages(chat_id)
    deleted = await dedup_memories()
    result = f"บันทึก {saved} ความจำใหม่ ลบซ้ำ {deleted} รายการ"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("memory_keeper_run", f"chat_id={chat_id} saved={saved} deleted={deleted}"),
        )
        await db.commit()

    return result
