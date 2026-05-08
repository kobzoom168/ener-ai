from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""Senior Developer ช่วยกบเขียน/review code
- ตอบด้วย code block ที่ใช้ได้ทันที
- อธิบาย 1-2 บรรทัดก่อน code
- ถ้าไม่ระบุภาษา → Python
- ถ้า review → ชี้จุดที่ควรแก้
- ถ้า debug → หา root cause ก่อน""")


@log_agent_run("CodeAgent")
async def run(text: str) -> str:
    return await chat(text, system=SYSTEM, agent="code")
