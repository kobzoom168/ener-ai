import re
from datetime import date
from app.core.ai import chat_json
from app.core.database import get_db

_LEARN_SYSTEM = """งานของคุณคือสกัดบทเรียนจากข้อความของกบ แล้วตอบ JSON เท่านั้นในรูปแบบนี้:
{
  "mistake": "สิ่งที่พลาด",
  "reason": "สาเหตุที่พลาด",
  "lesson": "บทเรียนที่ควรจำ"
}

กฎ:
- ตอบเป็นภาษาไทย
- ถ้าข้อความไม่ชัด ให้สรุปให้ดีที่สุดจากบริบท
- mistake และ lesson ต้องไม่ว่าง
- reason ว่างได้ถ้าไม่มีข้อมูลพอ"""


async def record_lesson(text: str) -> str:
    mistake_hint = ""
    reason_hint = ""
    match = re.match(r"^\s*พลาดเรื่อง\s+(.+?)\s+เพราะ\s+(.+?)\s*$", text)
    if match:
        mistake_hint = match.group(1).strip()
        reason_hint = match.group(2).strip()

    prompt = (
        f"ข้อความต้นฉบับ: {text}\n"
        f"mistake_hint: {mistake_hint}\n"
        f"reason_hint: {reason_hint}\n"
        "สกัดบทเรียนจากข้อความนี้"
    )
    try:
        result = await chat_json(prompt, system=_LEARN_SYSTEM, agent="learn")
        mistake = str(result.get("mistake", mistake_hint or text)).strip()
        reason = str(result.get("reason", reason_hint)).strip()
        lesson = str(result.get("lesson", text)).strip()
    except Exception:
        mistake = mistake_hint or text.strip()
        reason = reason_hint
        lesson = f"ครั้งหน้าต้องระวังเรื่อง {mistake}"

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO lessons_learned (mistake, reason, lesson)
            VALUES (?, ?, ?)
            """,
            (mistake, reason, lesson),
        )
        today = date.today().isoformat()
        await db.execute(
            "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
            (today, "mistake", f"{mistake} | บทเรียน: {lesson}"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("lesson_recorded", f"mistake={mistake}"),
        )
        await db.commit()

    lines = [
        "📌 บันทึกบทเรียนเรียบร้อย",
        "",
        f"❌ พลาด: {mistake}",
    ]
    if reason:
        lines.append(f"🧩 เพราะ: {reason}")
    lines.append(f"🔁 บทเรียน: {lesson}")
    return "\n".join(lines)
