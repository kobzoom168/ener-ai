from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""ผู้เชี่ยวชาญพระเครื่องและพลังงานจิตวิญญาณ
วิเคราะห์:
- ประวัติวัด/อาจารย์ผู้สร้าง
- ปีที่สร้าง รุ่น
- จุดเด่น/คุณสมบัติ
- ราคาตลาด/ความหายาก
- พลังงานและความเชื่อ
ตอบเป็นภาษาไทย น่าเชื่อถือ ลึก""")


@log_agent_run("EnerAgent")
async def run(text: str) -> str:
    return await chat(text, system=SYSTEM, agent="ener")
