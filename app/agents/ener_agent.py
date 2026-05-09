from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import get_agent_context, log_event
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: ช่วยกบวิเคราะห์พระเครื่องและพลังงานจิตวิญญาณ
วิเคราะห์:
- ประวัติวัด/อาจารย์ผู้สร้าง
- ปีที่สร้าง รุ่น
- จุดเด่น/คุณสมบัติ
- ราคาตลาด/ความหายาก
- พลังงานและความเชื่อ
ตอบเป็นภาษาไทย น่าเชื่อถือ ลึก""")


@log_agent_run("EnerAgent")
async def run(text: str) -> str:
    context = await get_agent_context("EnerAgent", ["ener", "amulet"])
    system_with_context = SYSTEM + f"\n\n{context}" if context else SYSTEM

    try:
        result = await chat(text, system=system_with_context, agent="ener")
        try:
            await log_event(
                agent_name="EnerAgent",
                event_type="task_done",
                summary=f"วิเคราะห์พระ/ener: {text[:80]}",
                tags=["ener", "amulet"],
                result="success",
            )
        except Exception:
            pass
        return result
    except Exception as exc:
        try:
            await log_event(
                agent_name="EnerAgent",
                event_type="task_failed",
                summary=f"ener fail: {text[:80]}",
                tags=["ener", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise
