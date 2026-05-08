from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""Content creator เชี่ยวชาญสายมู/พระเครื่อง
สร้าง caption/script สำหรับ TikTok Facebook YouTube
Format:
🪝 Hook: (ดึงดูดใน 3 วินาที)
📖 Body: (เนื้อหาหลัก)
📣 CTA: (call to action)
#hashtag ที่เหมาะสม""")


@log_agent_run("ContentAgent")
async def run(text: str) -> str:
    return await chat(text, system=SYSTEM, agent="content")
