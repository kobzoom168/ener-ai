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
- ข้อความที่เป็น negation เช่น "ไม่มีข้อมูล..." หรือ "ไม่ทราบ..."
- เวลา วันที่ หรือตัวเลขโดดๆ
- log หรือ system message
- คำถามที่ยังไม่มีคำตอบ
- ข้อความที่ไม่เกี่ยวกับตัวกบโดยตรง

บันทึกเฉพาะ:
- fact ที่ชัดเจนเกี่ยวกับกบ
- confidence >= 0.8 เท่านั้น

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
_JUNK_PATTERNS = [
    r"ไม่มีข้อมูล",
    r"ไม่ทราบ",
    r"^\d{2}:\d{2}",
    r"^เวลา",
    r"มีกี่",
    r"ผู้ใช้ต้องการ",
    r"^log",
]
_ENTITY_KEYWORDS = [
    "หมา",
    "สุนัข",
    "dog",
    "แมว",
    "cat",
    "สัตว์เลี้ยง",
    "บ้าน",
    "ที่อยู่",
    "งาน",
    "โรงพยาบาล",
    "โปรเจกต์",
    "อาหาร",
    "สุขภาพ",
    "ครอบครัว",
    "ลูก",
    "แฟน",
    "ภรรยา",
]
_STOPWORDS = {
    "กบ",
    "พี่",
    "เป็น",
    "มี",
    "อยู่",
    "และ",
    "ของ",
    "ที่",
    "นี้",
    "นั้น",
    "ครับ",
    "ค่ะ",
    "นะ",
    "เลย",
}


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


def _is_junk(content: str) -> bool:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return True
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _JUNK_PATTERNS):
        return True
    if re.fullmatch(r"[\d\s:/\-\.]+", text):
        return True
    if text.endswith("?") or text.endswith("？"):
        return True
    return False


def _normalize_text(content: str) -> str:
    return " ".join(str(content or "").strip().lower().split())


def _entity_keys(content: str) -> set[str]:
    lowered = _normalize_text(content)
    keys = {keyword for keyword in _ENTITY_KEYWORDS if keyword in lowered}
    for token in re.findall(r"[a-zA-Z0-9ก-๙]+", lowered):
        if len(token) < 3 or token in _STOPWORDS or token.isdigit():
            continue
        keys.add(token)
    return keys


def _should_replace_existing(existing_content: str, new_content: str) -> bool:
    existing_norm = _normalize_text(existing_content)
    new_norm = _normalize_text(new_content)
    if existing_norm == new_norm:
        return False
    if existing_norm in new_norm and len(new_norm) > len(existing_norm):
        return True
    if len(new_norm) > len(existing_norm) + 8:
        return True
    return False


async def _find_semantic_match(db, content: str):
    cursor = await db.execute(
        "SELECT id, content, memory_type FROM long_term_memories ORDER BY id DESC"
    )
    rows = await cursor.fetchall()

    content_norm = _normalize_text(content)
    content_keys = _entity_keys(content)
    if not content_keys:
        return None

    best_row = None
    best_score = 0
    for row in rows:
        existing_content = str(row["content"] or "")
        existing_norm = _normalize_text(existing_content)
        if not existing_norm:
            continue
        if existing_norm == content_norm:
            return row

        existing_keys = _entity_keys(existing_content)
        overlap = len(content_keys & existing_keys)
        if overlap <= 0:
            continue

        score = overlap
        if existing_norm in content_norm or content_norm in existing_norm:
            score += 3
        if score > best_score:
            best_row = row
            best_score = score

    return best_row if best_score >= 2 else None


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
            if confidence < 0.8:
                continue

            content = " ".join(str(mem.get("content", "")).split()).strip()
            if not content or _is_sensitive_text(content) or _is_junk(content):
                continue

            category = _normalize_category(mem.get("category"))
            semantic_match = await _find_semantic_match(db, content)

            if semantic_match:
                if _should_replace_existing(str(semantic_match["content"] or ""), content):
                    await db.execute(
                        """
                        UPDATE long_term_memories
                        SET content = ?, memory_type = ?
                        WHERE id = ?
                        """,
                        (content, category, semantic_match["id"]),
                    )
                    saved += 1
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
