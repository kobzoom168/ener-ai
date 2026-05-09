from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import get_agent_context, log_event
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: ช่วยกบสร้าง content สายมู/พระเครื่อง
สร้าง caption/script สำหรับ TikTok Facebook YouTube
Format:
🪝 Hook: (ดึงดูดใน 3 วินาที)
📖 Body: (เนื้อหาหลัก)
📣 CTA: (call to action)
#hashtag ที่เหมาะสม""")


@log_agent_run("ContentAgent")
async def run(text: str) -> str:
    context = await get_agent_context("ContentAgent", ["content", "marketing"])
    system_with_context = SYSTEM + f"\n\n{context}" if context else SYSTEM

    try:
        result = await chat(text, system=system_with_context, agent="content")
        try:
            await log_event(
                agent_name="ContentAgent",
                event_type="task_done",
                summary=f"สร้าง content: {text[:80]}",
                tags=["content"],
                result="success",
            )
        except Exception:
            pass
        return result
    except Exception as exc:
        try:
            await log_event(
                agent_name="ContentAgent",
                event_type="task_failed",
                summary=f"content fail: {text[:80]}",
                tags=["content", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise
