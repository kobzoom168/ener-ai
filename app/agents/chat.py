from app.core.agents import log_agent_run
from app.core.ai_gateway import get_recent_history, run_ai
from app.core.diagnostics import user_message_touches_engineering_topics
from app.core.event_log import get_agent_context, log_event
from app.core.memory import (
    extract_and_store_long_term_memories,
    get_long_term_context,
    get_recent_summaries,
    get_time_context,
)
from app.core.policy import build_system_prompt

SAVE_KEYWORDS = ["บันทึก", "จำไว้", "save นี่", "จดไว้", "อย่าลืมว่า"]


async def _get_history(conversation_id: str) -> list[dict[str, str]]:
    if not str(conversation_id or "").strip():
        return []
    return await get_recent_history(conversation_id=conversation_id, limit=20)


async def _get_model_handoff() -> str:
    from app.core.database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT value FROM memories
            WHERE key = 'model_handoff_context'
              AND updated_at > datetime('now', '-1 hour')
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
    return row["value"] if row else ""


async def _build_self_context() -> str:
    from pathlib import Path

    from app.core.agents import COMMAND_AGENT_MAP, SCHEDULER_AGENTS
    from app.core.ai import get_active_model, get_model_label
    from app.core.database import get_system_stats

    stats = await get_system_stats()
    active_model = await get_active_model()
    model_label = get_model_label(active_model or "")
    agents_dir = Path(__file__).resolve().parent
    try:
        agent_files = sorted(
            file_path.stem
            for file_path in agents_dir.glob("*.py")
            if file_path.name != "__init__.py"
        )
    except Exception:
        agent_files = sorted(set(COMMAND_AGENT_MAP.values()))

    return f"""
=== ข้อมูลระบบ Ener-AI (real-time) ===
🤖 Model ที่ใช้อยู่: {model_label}
🏗️ Architecture: FastAPI + SQLite + Telegram + Web Workspace

📦 Agents ({len(agent_files)} ตัว):
{", ".join(agent_files)}

📊 Database Stats:
- Messages: {stats.get("messages", 0)} ข้อความ
- Notes: {stats.get("notes", 0)} notes
- Tasks ทั้งหมด: {stats.get("tasks", 0)} | เปิดอยู่: {stats.get("open_tasks", 0)}
- Memories: {stats.get("memories", 0)} | Long-term: {stats.get("long_term_memories", 0)}
- AI Runs: {stats.get("ai_runs", 0)} ครั้ง
- Files uploaded: {stats.get("uploads", 0)}

⏰ Scheduler Jobs:
- 07:30 จันทร์-ศุกร์: Daily Standup -> Telegram
- 08:00 ทุกวัน: ดึงข่าว + Morning Briefing
- 21:00 ทุกวัน: Daily Digest + Session Log
- จันทร์ 09:00: Weekly Review

🌐 Endpoints:
- Web Workspace: /workspace
- Admin Dashboard: /admin
- Telegram Webhook: /webhook
- Health: /health

💾 Files:
- app/agents/ -> {len(agent_files)} agents
- app/core/ -> ai.py, database.py, policy.py, tools.py, memory.py
- app/bot/router.py -> Telegram handlers
- app/main.py -> FastAPI routes + Web UI
- app/scheduler.py -> Cron jobs

🧭 Registries:
- Command agents: {len(set(COMMAND_AGENT_MAP.values()))}
- Scheduler agents: {len(set(SCHEDULER_AGENTS.values()))}

พี่รู้จักตัวเองครบแล้ว ถ้ากบถามเรื่องระบบตอบได้เลย
""".strip()


async def _build_system_prompt(current_user_message: str = "") -> str:
    agent_memory = await get_agent_context("MainChatAgent", ["chat", "conversation", "tools"])
    time_context = get_time_context()
    long_term = await get_long_term_context()
    summaries = await get_recent_summaries()
    handoff = await _get_model_handoff()
    self_context = await _build_self_context()
    handoff_section = f"\n\n=== Handoff จาก Model ก่อนหน้า ===\n{handoff}" if handoff else ""
    scope_guard = ""
    if (current_user_message or "").strip() and not user_message_touches_engineering_topics(
        current_user_message
    ):
        scope_guard = """

=== ขอบเขตบริบท (สำคัญ) ===
- ถ้าข้อความ **ล่าสุด** ของกบไม่ได้ถามเรื่องฝั่งโปรแกรม/เซิร์ฟเวอร์/SSH/โค้ด/OTP/webhook/repo หรือการดีบักระบบ — **ห้าม** นำบริบทเทคนิคจากรอบก่อนหน้า (เช่น การตรวจสอบแชทบอทหรือรหัส OTP) มาตอบโดยไม่จำเป็น
- คำว่า "ไม่ตอบ" **ไม่ได้** แปลว่าแชทบอทหรือเครื่องรันครับ — ถ้ากบพูดถึงลูกค้า/ทีม/vendor ให้ตีความเป็น **การสื่อสารกับคน**
- ตอบตามคำถามปัจจุบันเป็นหลัก ใช้ summary/handoff เฉพาะส่วนที่เกี่ยวกับคำถามนี้โดยตรง
"""

    return build_system_prompt(f"""

{time_context}

{long_term}

=== สรุปบทสนทนาล่าสุด 7 วัน ===
{summaries}
{handoff_section}

{self_context}
{scope_guard}

หมายเหตุ: ข้อมูลเหล่านี้คือสิ่งที่กบบอกไว้ก่อนหน้า จำและใช้ตอบได้เลย

{agent_memory}

หน้าที่:
- คุยกับกบเหมือนผู้ช่วยส่วนตัวแบบ conversational
- ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
- รู้จักตัวเองและระบบครบ ตอบคำถามเกี่ยวกับระบบได้เลย
- ถ้าต้องบันทึก task, note, memory หรือเรียกความสามารถอื่น ให้ใช้ tools ตามความจำเป็น
- ถ้าไม่จำเป็นต้องใช้ tool ให้ตอบข้อความธรรมดาได้เลย
- ถ้ากบถามตัวเลข CPU/RAM/Disk, logs, หรือ errors ของ Ener-AI โดยตรง แต่คำตอบรอบนี้ยังไม่ได้มาจากเครื่องมือมอนิเตอร์ของบอท — **ห้าม** แนะนำให้รัน `docker stats` / `docker logs` หรือคำสั่ง shell แทนตัวเลขจริง ให้บอกสั้น ๆ ว่าให้พิมพ์เช่น “เช็ค CPU” หรือ “ดูสถานะระบบ” เพื่อให้บอทดึงข้อมูลจริง
- ตอบเป็นข้อความธรรมดาเท่านั้น ไม่ต้องตอบเป็น JSON
""")


@log_agent_run("MainChatAgent")
async def run_chat(chat_id: str, text: str) -> str:
    system_prompt = await _build_system_prompt(text)
    try:
        gateway_result = await run_ai(
            source="telegram",
            external_chat_id=str(chat_id),
            text=text,
            system_prompt=system_prompt,
        )
        reply = str(gateway_result.get("reply", "")).strip() or "ยังไม่มีคำตอบตอนนี้"
        route = gateway_result.get("route") or {}
        pipeline_meta = {
            "complexity": route.get("complexity", "simple"),
            "domain": route.get("domain", "chat"),
            "model_used": gateway_result.get("model_used", route.get("model", "groq")),
            "was_fixed": False,
            "elapsed_ms": int(gateway_result.get("elapsed_ms", 0) or 0),
            "trace_id": gateway_result.get("trace_id", ""),
            "conversation_id": gateway_result.get("conversation_id", ""),
            "context_summary": gateway_result.get("context_summary", ""),
        }
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

    try:
        from app.core.database import get_db

        async with get_db() as db:
            await db.execute(
                "DELETE FROM memories WHERE key = 'model_handoff_context'"
            )
            await db.commit()
    except Exception:
        pass
    await extract_and_store_long_term_memories(text, reply)
    final_reply = reply
    lowered_text = text.lower()
    if any(keyword in lowered_text for keyword in SAVE_KEYWORDS):
        from app.agents.memory_keeper import extract_from_recent_messages

        saved = await extract_from_recent_messages(chat_id, limit=20)
        final_reply += f"\n\n📝 บันทึกแล้ว {saved} ความจำครับกบ"
    try:
        await log_event(
            agent_name="MainChatAgent",
            event_type="task_done" if pipeline_meta.get("was_fixed") else "insight",
            summary=f"ตอบแชต: {text[:80]}",
            tags=["chat", str(pipeline_meta.get("complexity", "simple")), str(pipeline_meta.get("domain", "chat"))],
            context=reply[:200],
            result="success",
            learned=(
                    f"trace_id={pipeline_meta.get('trace_id', '')} "
                    f"conversation_id={pipeline_meta.get('conversation_id', '')} "
                f"pipeline={pipeline_meta.get('model_used', 'groq')} "
                f"fixed={pipeline_meta.get('was_fixed', False)} "
                f"elapsed_ms={pipeline_meta.get('elapsed_ms', 0)}"
            ),
        )
    except Exception:
        pass
    return final_reply
