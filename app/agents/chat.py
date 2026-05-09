from app.core.ai import chat_with_tools
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import get_agent_context, log_event
from app.core.memory import (
    extract_and_store_long_term_memories,
    get_long_term_context,
    get_recent_summaries,
    get_time_context,
)
from app.core.policy import build_system_prompt
from app.core.tools import TOOLS, execute_tool

SAVE_KEYWORDS = ["บันทึก", "จำไว้", "save นี่", "จดไว้", "อย่าลืมว่า"]


async def _get_history(chat_id: str) -> list[dict[str, str]]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (chat_id,),
        )
        rows = await cursor.fetchall()
    return [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(rows)
    ]


async def _build_system_prompt() -> str:
    agent_memory = await get_agent_context("MainChatAgent", ["chat", "conversation", "tools"])
    time_context = get_time_context()
    long_term = await get_long_term_context()
    summaries = await get_recent_summaries()
    return build_system_prompt(f"""

{time_context}

{long_term}

=== สรุปบทสนทนาล่าสุด 7 วัน ===
{summaries}

หมายเหตุ: ข้อมูลเหล่านี้คือสิ่งที่กบบอกไว้ก่อนหน้า จำและใช้ตอบได้เลย

{agent_memory}

หน้าที่:
- คุยกับกบเหมือนผู้ช่วยส่วนตัวแบบ conversational
- ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
- ถ้าต้องบันทึก task, note, memory หรือเรียกความสามารถอื่น ให้ใช้ tools ตามความจำเป็น
- ถ้าไม่จำเป็นต้องใช้ tool ให้ตอบข้อความธรรมดาได้เลย
- ตอบเป็นข้อความธรรมดาเท่านั้น ไม่ต้องตอบเป็น JSON
""")


async def _save_messages(chat_id: str, text: str, reply: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "user", text),
        )
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "assistant", reply),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("chat_message_saved", f"chat_id={chat_id}"),
        )
        await db.commit()


@log_agent_run("MainChatAgent")
async def run_chat(chat_id: str, text: str) -> str:
    history = await _get_history(chat_id)
    system_prompt = await _build_system_prompt()
    try:
        response = await chat_with_tools(
            prompt=text,
            system=system_prompt,
            messages=history,
            tools=TOOLS,
            agent="MainChatAgent",
        )
        reply = str(response.get("text", "")).strip() or "ยังไม่มีคำตอบตอนนี้"
        tool_calls = response.get("tool_calls", []) or []
    except Exception as exc:
        try:
            await log_event(
                agent_name="MainChatAgent",
                event_type="task_failed",
                summary=f"chat fail: {text[:80]}",
                tags=["chat", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise

    tool_results = []
    for tool_call in tool_calls:
        tool_name = str(tool_call.get("name", "")).strip()
        tool_input = tool_call.get("input", {}) or {}
        if not tool_name:
            continue
        try:
            result = await execute_tool(tool_name, tool_input)
            tool_results.append(f"✅ {result}")
            try:
                await log_event(
                    agent_name="MainChatAgent",
                    event_type="task_done",
                    summary=f"tool {tool_name}: {text[:80]}",
                    tags=["chat", "tool", tool_name],
                    context=str(tool_input)[:200],
                    result="success",
                )
            except Exception:
                pass
        except Exception as exc:
            try:
                await log_event(
                    agent_name="MainChatAgent",
                    event_type="task_failed",
                    summary=f"tool fail {tool_name}: {text[:80]}",
                    tags=["chat", "tool", tool_name, "error"],
                    context=str(tool_input)[:200],
                    result="failure",
                    learned=str(exc)[:200],
                )
            except Exception:
                pass
            tool_results.append(f"⚠️ tool {tool_name} ทำงานไม่สำเร็จ")

    await _save_messages(chat_id, text, reply)
    await extract_and_store_long_term_memories(text, reply)
    final_reply = reply if not tool_results else reply + "\n\n" + "\n".join(tool_results)
    lowered_text = text.lower()
    if any(keyword in lowered_text for keyword in SAVE_KEYWORDS):
        from app.agents.memory_keeper import extract_from_recent_messages

        saved = await extract_from_recent_messages(chat_id, limit=20)
        final_reply += f"\n\n📝 บันทึกแล้ว {saved} ความจำครับกบ"
    try:
        await log_event(
            agent_name="MainChatAgent",
            event_type="task_done" if tool_results else "insight",
            summary=f"ตอบแชต: {text[:80]}",
            tags=["chat"] + (["tool-use"] if tool_results else ["conversation"]),
            context=reply[:200],
            result="success",
            learned=f"ใช้ tool {len(tool_results)} ครั้ง" if tool_results else None,
        )
    except Exception:
        pass
    return final_reply
