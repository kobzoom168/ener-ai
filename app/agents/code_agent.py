from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import get_agent_context, log_event
from app.core.policy import build_system_prompt

SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: ช่วยกบเขียน/review/debug code
- ตอบด้วย code block ที่ใช้ได้ทันที
- อธิบาย 1-2 บรรทัดก่อน code
- ถ้าไม่ระบุภาษา → Python
- ถ้า review → ชี้จุดที่ควรแก้
- ถ้า debug → หา root cause ก่อน""")


@log_agent_run("CodeAgent")
async def run(text: str) -> str:
    context = await get_agent_context("CodeAgent", ["code", "python"])
    system_with_context = SYSTEM + f"\n\n{context}" if context else SYSTEM

    try:
        result = await chat(text, system=system_with_context, agent="code")
        try:
            await log_event(
                agent_name="CodeAgent",
                event_type="task_done",
                summary=f"เขียน code: {text[:80]}",
                tags=["code"],
                result="success",
                learned=None,
            )
        except Exception:
            pass
        return result
    except Exception as exc:
        try:
            await log_event(
                agent_name="CodeAgent",
                event_type="task_failed",
                summary=f"code fail: {text[:80]}",
                tags=["code", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise


async def run_code_change(chat_id: str, goal: str) -> str:
    """Mode B: real file change via gateway + code_agent tools (approval token)."""
    from app.core.ai_gateway import run_ai

    prompt = (goal or "").strip() or "propose code change"
    result = await run_ai(
        source="telegram",
        external_chat_id=str(chat_id),
        text=f"แก้ไฟล์จริง: {prompt}",
        intent="code_agent",
    )
    return str(result.get("reply", "")).strip() or "ยังไม่มีคำตอบตอนนี้"
