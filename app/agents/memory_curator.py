from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

PROFILE_CATEGORIES = {
    "identity": "ข้อมูลตัวตน ชื่อ วันเกิด",
    "location": "ที่อยู่ บ้าน ที่ทำงาน",
    "work": "งานประจำ ตำแหน่ง บริษัท",
    "business": "ธุรกิจส่วนตัว โปรเจกต์",
    "goal": "เป้าหมาย แผน ความต้องการ",
    "belief": "ความเชื่อ ค่านิยม มุมมองชีวิต",
    "preference": "ความชอบ สไตล์ วิธีคุย",
    "family": "ครอบครัว คนรัก สัตว์เลี้ยง",
    "interest": "ความสนใจ งานอดิเรก",
    "skill": "ทักษะ ความสามารถ",
}

CURATOR_SYSTEM = build_system_prompt("""
งานของพี่: รวบรวม long-term memories ที่กระจัดกระจาย
แล้วจัดใหม่เป็น profile cards ที่ชัดเจน

กฎ:
1. รวม facts เดียวกันเป็น 1 card
2. เลือกข้อมูลล่าสุด/ถูกต้องที่สุด
3. ลบ negation ("ไม่มีข้อมูล")
4. ลบ timestamp/วันที่โดดๆ
5. ลบ activity log ชั่วคราว
6. แต่ละ card ต้องเป็น fact ที่ยังใช้ได้วันนี้

ตอบ JSON:
{
  "cards": [
    {
      "category": "family",
      "key": "dog",
      "content": "แบล็คแมน หมา Boston Terrier อายุ 1 ปี พลังงานสูง",
      "replaces": [36, 35, 47, 66, 67]
    },
    {
      "category": "work",
      "key": "main_job",
      "content": "System Admin / IT PM ที่ Rutnin Eye Hospital ดูแล IT Infrastructure",
      "replaces": [44, 58, 61, 18, 11, 30]
    }
  ],
  "delete_only": [1, 2, 6, 7, 8, 10]
}
""")

_CATEGORY_LABELS = {
    "identity": "👤 ตัวตน",
    "location": "📍 ที่อยู่",
    "work": "💼 งาน",
    "business": "🏢 ธุรกิจ",
    "goal": "🎯 เป้าหมาย",
    "belief": "🌟 ความเชื่อ",
    "preference": "❤️ ความชอบ",
    "family": "🐾 ครอบครัว/สัตว์เลี้ยง",
    "interest": "✨ ความสนใจ",
    "skill": "🛠️ ทักษะ",
    "profile": "👤 Profile",
    "general": "📝 ทั่วไป",
}


def _normalize_category(value: object) -> str:
    category = str(value or "").strip().lower()
    if category in PROFILE_CATEGORIES:
        return category
    return "general"


def _parse_id_list(values: object) -> list[int]:
    parsed: list[int] = []
    if not isinstance(values, list):
        return parsed
    for value in values:
        try:
            item = int(value)
        except Exception:
            continue
        if item not in parsed:
            parsed.append(item)
    return parsed


async def _log_curator_event(
    event_type: str,
    summary: str,
    result: str,
    learned: str | None = None,
) -> None:
    try:
        await log_event(
            agent_name="MemoryCurator",
            event_type=event_type,
            summary=summary,
            tags=["memory", "curator", "profile-cards"],
            result=result,
            learned=learned,
        )
    except Exception:
        pass


@log_agent_run("MemoryCurator")
async def curate_memories() -> str:
    """รัน full curation cycle"""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, content, memory_type FROM long_term_memories ORDER BY id"
        )
        rows = await cursor.fetchall()

    if not rows:
        return "ไม่มี memory ให้ curate"

    mem_text = "\n".join(
        f"[{row['id']}] ({row['memory_type']}) {row['content']}"
        for row in rows
    )

    try:
        result = await chat_json(
            f"memories ทั้งหมด:\n{mem_text}",
            system=CURATOR_SYSTEM,
            agent="memorycurator",
        )  # configured model + provider fallback (haiku pin failed without an Anthropic key)
    except Exception as exc:
        message = f"Curation ล้มเหลว: {exc}"
        await _log_curator_event("task_failed", "memory curation fail", "failure", learned=str(exc)[:200])
        return message

    raw_cards = result.get("cards", []) or []
    delete_only = _parse_id_list(result.get("delete_only", []))

    cards: list[dict] = []
    for card in raw_cards:
        if not isinstance(card, dict):
            continue
        content = " ".join(str(card.get("content", "")).split()).strip()
        if not content:
            continue
        cards.append(
            {
                "category": _normalize_category(card.get("category")),
                "key": str(card.get("key", "")).strip().lower(),
                "content": content,
                "replaces": _parse_id_list(card.get("replaces", [])),
            }
        )

    new_count = 0
    deleted_count = 0
    replace_ids: list[int] = []
    for card in cards:
        for item in card["replaces"]:
            if item not in replace_ids:
                replace_ids.append(item)
    delete_ids = replace_ids[:]
    for item in delete_only:
        if item not in delete_ids:
            delete_ids.append(item)

    try:
        async with get_db() as db:
            for old_id in delete_ids:
                cursor = await db.execute(
                    "DELETE FROM long_term_memories WHERE id = ?",
                    (old_id,),
                )
                deleted_count += cursor.rowcount or 0

            for card in cards:
                await db.execute(
                    """
                    INSERT INTO long_term_memories (content, memory_type)
                    VALUES (?, ?)
                    """,
                    (card["content"], card["category"]),
                )
                new_count += 1

            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                (
                    "memory_curator_run",
                    f"cards={new_count} deleted={deleted_count}",
                ),
            )
            await db.commit()
    except Exception as exc:
        message = f"Curation ล้มเหลว: {exc}"
        await _log_curator_event("task_failed", "memory curator db apply fail", "failure", learned=str(exc)[:200])
        return message

    summary = (
        f"✅ Curation เสร็จครับกบ\n"
        f"สร้าง {new_count} cards ใหม่\n"
        f"ลบ {deleted_count} รายการเก่า"
    )
    await _log_curator_event(
        "task_done",
        "memory curator completed",
        "success",
        learned=f"cards={new_count} deleted={deleted_count}",
    )
    return summary


async def get_curated_context() -> str:
    """ดึง memory แบบ curated สำหรับส่งให้ AI หลัก"""
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT memory_type, content
            FROM long_term_memories
            ORDER BY memory_type, id
            """
        )
        rows = await cursor.fetchall()

    if not rows:
        return ""

    by_category: dict[str, list[str]] = {}
    for row in rows:
        category = str(row["memory_type"] or "general").strip().lower() or "general"
        by_category.setdefault(category, []).append(str(row["content"] or "").strip())

    sections = []
    for category, items in by_category.items():
        label = _CATEGORY_LABELS.get(category, category)
        content = " | ".join(item for item in items if item)
        if content:
            sections.append(f"{label}: {content}")

    if not sections:
        return ""
    return "=== สิ่งที่พี่รู้เกี่ยวกับกบ ===\n" + "\n".join(sections)
