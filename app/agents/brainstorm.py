from app.core.ai import chat, chat_json
from app.core.database import get_db

_AI_A_SYSTEM = "คุณคือนักวิจารณ์ โจมตีไอเดียนี้ หาจุดอ่อนให้หมด"
_AI_B_SYSTEM = "คุณคือผู้แก้ปัญหา รับข้อโจมตีจาก AI_A แล้วแก้ทุกจุด"
_AI_C_SYSTEM = "คุณคือนักวิเคราะห์กลาง สรุปผลจาก A กับ B อย่างเป็นกลาง"
_FINAL_SYSTEM = """คุณคือคนตัดสินไอเดียจากวง brainstorm ตอบ JSON เท่านั้นในรูปแบบนี้:
{
  "summary": "สรุปภาพรวมสั้นๆ ภาษาไทย",
  "verdict": "go|pivot|stop",
  "reason": "เหตุผลสั้นๆ ภาษาไทย"
}

กฎ:
- verdict ต้องเป็น go หรือ pivot หรือ stop เท่านั้น
- summary และ reason ต้องเป็นภาษาไทย"""


async def run_brainstorm(topic: str) -> str:
    history = []
    context = f"หัวข้อเริ่มต้น: {topic}"

    for round_number in range(1, 4):
        prompt_a = (
            f"หัวข้อ: {topic}\n"
            f"บริบทรอบก่อนหน้า:\n{context}\n\n"
            f"นี่คือรอบที่ {round_number}\n"
            "โจมตีไอเดียนี้แบบตรงไปตรงมาและเป็นภาษาไทย"
        )
        ai_a = (await chat(prompt_a, system=_AI_A_SYSTEM)).strip()

        prompt_b = (
            f"หัวข้อ: {topic}\n"
            f"บริบทรอบก่อนหน้า:\n{context}\n\n"
            f"ข้อโจมตีจาก AI_A รอบ {round_number}:\n{ai_a}\n\n"
            "ตอบโต้ทุกประเด็นและเสนอทางแก้เป็นภาษาไทย"
        )
        ai_b = (await chat(prompt_b, system=_AI_B_SYSTEM)).strip()

        prompt_c = (
            f"หัวข้อ: {topic}\n"
            f"ข้อโจมตีจาก AI_A รอบ {round_number}:\n{ai_a}\n\n"
            f"คำตอบจาก AI_B รอบ {round_number}:\n{ai_b}\n\n"
            "สรุปอย่างเป็นกลางว่ารอบนี้เห็นอะไร จุดไหนยังเสี่ยง จุดไหนน่าไปต่อ"
        )
        ai_c = (await chat(prompt_c, system=_AI_C_SYSTEM)).strip()

        history.append(
            {
                "round": round_number,
                "ai_a": ai_a,
                "ai_b": ai_b,
                "ai_c": ai_c,
            }
        )
        context = (
            f"หัวข้อ: {topic}\n"
            + "\n\n".join(
                [
                    f"รอบ {item['round']}\nAI_A:\n{item['ai_a']}\n\nAI_B:\n{item['ai_b']}\n\nAI_C:\n{item['ai_c']}"
                    for item in history
                ]
            )
        )

    final_prompt = (
        f"หัวข้อ brainstorm: {topic}\n\n"
        + "\n\n".join(
            [
                f"รอบ {item['round']}\nAI_A:\n{item['ai_a']}\n\nAI_B:\n{item['ai_b']}\n\nAI_C:\n{item['ai_c']}"
                for item in history
            ]
        )
        + "\n\nตัดสินผลรวมทั้งหมด"
    )
    final_result = await chat_json(final_prompt, system=_FINAL_SYSTEM)
    summary = str(final_result.get("summary", "สรุปไอเดียเสร็จแล้ว")).strip()
    verdict = str(final_result.get("verdict", "pivot")).strip().lower()
    reason = str(final_result.get("reason", "")).strip()

    async with await get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("brainstorm_completed", f"topic={topic} verdict={verdict}"),
        )
        await db.commit()

    verdict_text = {
        "go": "GO",
        "pivot": "PIVOT",
        "stop": "STOP",
    }.get(verdict, "PIVOT")

    lines = [f"📌 brainstorm เสร็จแล้ว: {summary}", ""]
    for item in history:
        lines.extend(
            [
                f"รอบ {item['round']}",
                f"AI_A: {item['ai_a']}",
                f"AI_B: {item['ai_b']}",
                f"AI_C: {item['ai_c']}",
                "",
            ]
        )
    lines.append(f"🎯 คำตัดสิน: {verdict_text}")
    if reason:
        lines.append(f"เหตุผล: {reason}")
    return "\n".join(lines)
