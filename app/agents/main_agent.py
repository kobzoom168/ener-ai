import re

from app.agents import (
    brain,
    brainstorm,
    chat as chat_agent,
    code_agent,
    content_agent,
    cost,
    ener_agent,
    learn,
    memory as memory_agent,
    news as news_agent,
    summary,
    task as task_agent,
    voice,
)
from app.core.ai import chat_json
from app.core.agents import COMMAND_AGENT_MAP, log_agent_run
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

INTENT_SYSTEM = build_system_prompt("""
วิเคราะห์ข้อความ ตอบ JSON:
{
  "agent": "chat|note|task|code|ener|content|think|memory|news",
  "confidence": 0.0,
  "chain": []
}

Rules:
- code/debug/เขียน function/แก้ bug/review → "code"
- พระ/scan/วิเคราะห์พระ/ener report → "ener" chain ["content"] เมื่อมีเจตนาจะขายหรือทำคอนเทนต์ต่อ
- caption/script/content/โพสต์/ขาย → "content"
- brainstorm/คิด/วิเคราะห์ธุรกิจ/pros cons → "think"
- จด/บันทึก/note → "note"
- task/งาน/ต้องทำ/remind → "task"
- ข่าว/news → "news"
- จำ/ค้นหา/memory/เคยบอก → "memory"
- ทั่วไป/คุย/ถาม/อารมณ์ → "chat"
- confidence < 0.7 ให้เลือก "chat"
- chain ต้องเป็น list ของ agent ถัดไปเท่านั้น
- ห้ามตอบนอก JSON
""")


class MainAgent:
    """
    Central intelligence — รับทุก message จาก Telegram
    ตัดสินใจ route ไป sub-agent ที่เหมาะสม
    """

    async def route_free_text(self, chat_id: str, text: str) -> str:
        return await self.run(chat_id, text)

    async def detect_intent(self, text: str) -> dict:
        try:
            result = await chat_json(text, system=INTENT_SYSTEM, agent="mainagent")
            agent = str(result.get("agent", "chat")).strip().lower() or "chat"
            confidence = float(result.get("confidence", 0.5) or 0.5)
            chain = result.get("chain", [])
            if not isinstance(chain, list):
                chain = []
            normalized_chain = [str(item).strip().lower() for item in chain if str(item).strip()]
            return {
                "agent": agent,
                "confidence": confidence,
                "chain": normalized_chain,
            }
        except Exception:
            return {"agent": "chat", "confidence": 0.5, "chain": []}

    def _parse_task_input(self, raw: str) -> tuple[str, str, str]:
        text = (raw or "").strip()
        priority = "medium"
        deadline_hint = ""

        if "!!" in text:
            priority = "high"
            text = text.replace("!!", "").strip()
        elif "!" in text:
            priority = "medium"
            text = text.replace("!", "").strip()

        deadline_match = re.search(r"deadline[:\s]+(.+?)(?:\s|$)", text, re.IGNORECASE)
        if deadline_match:
            deadline_hint = deadline_match.group(1).strip()
            text = text[: deadline_match.start()].strip()

        return text, priority, deadline_hint

    async def _dispatch(
        self,
        agent: str,
        chat_id: str,
        text: str,
        *,
        triggered_by: str = "user",
    ) -> str:
        normalized_agent = (agent or "chat").strip().lower()
        try:
            await log_event(
                agent_name="MainAgent",
                event_type="handoff",
                summary=f"route '{text[:60]}' -> {normalized_agent}",
                tags=["routing", normalized_agent],
                triggered_by=triggered_by,
                result="success",
            )
        except Exception:
            pass

        if normalized_agent == "chat":
            return await chat_agent.run_chat(chat_id, text, _agent_triggered_by=triggered_by)

        if normalized_agent == "note":
            return await brain.process_note(text, chat_id, _agent_triggered_by=triggered_by)

        if normalized_agent == "task":
            title, priority, deadline_hint = self._parse_task_input(text)
            return await task_agent.create_task(
                title,
                priority=priority,
                deadline_hint=deadline_hint,
                _agent_triggered_by=triggered_by,
            )

        if normalized_agent == "code":
            return await code_agent.run(text, _agent_triggered_by=triggered_by)

        if normalized_agent == "ener":
            return await ener_agent.run(text, _agent_triggered_by=triggered_by)

        if normalized_agent == "content":
            return await content_agent.run(text, _agent_triggered_by=triggered_by)

        if normalized_agent == "think":
            return await brainstorm.run_brainstorm(text, _agent_triggered_by=triggered_by)

        if normalized_agent == "memory":
            return await memory_agent.search_memory(text, _agent_triggered_by=triggered_by)

        if normalized_agent == "news":
            return await news_agent.fetch_and_summarize(_agent_triggered_by=triggered_by)

        return await chat_agent.run_chat(chat_id, text, _agent_triggered_by=triggered_by)

    async def _handle_command(self, normalized_command: str, text: str, chat_id: str) -> str:
        if normalized_command == "note":
            return await brain.process_note(text, chat_id, _agent_triggered_by="user")

        if normalized_command == "task":
            return await self._dispatch("task", chat_id, text, triggered_by="user")

        if normalized_command == "tasks":
            return await task_agent.list_tasks(_agent_triggered_by="user")

        if normalized_command == "done":
            return await task_agent.complete_task(int(text), _agent_triggered_by="user")

        if normalized_command in {"learn", "mistake"}:
            return await learn.record_lesson(text, _agent_triggered_by="user")

        if normalized_command in {"think", "brainstorm"}:
            return await brainstorm.run_brainstorm(text, _agent_triggered_by="user")

        if normalized_command == "park":
            return await memory_agent.park_idea(text, _agent_triggered_by="user")

        if normalized_command == "search":
            return await memory_agent.search_memory(text, _agent_triggered_by="user")

        if normalized_command == "remember":
            return await memory_agent.remember_memory(text, _agent_triggered_by="user")

        if normalized_command == "forget":
            return await memory_agent.forget_memory(text, _agent_triggered_by="user")

        if normalized_command == "memory":
            return await memory_agent.list_memory(_agent_triggered_by="user")

        if normalized_command == "voice":
            return await voice.handle_voice_command(chat_id, text, _agent_triggered_by="user")

        if normalized_command == "today":
            return await summary.generate_daily_summary(_agent_triggered_by="user")

        if normalized_command == "week":
            return await summary.generate_weekly_summary(_agent_triggered_by="user")

        if normalized_command == "news":
            return await news_agent.fetch_and_summarize(_agent_triggered_by="user")

        if normalized_command == "cost":
            return await cost.get_cost_report(chat_id, _agent_triggered_by="user")

        if normalized_command == "code":
            return await code_agent.run(text, _agent_triggered_by="user")

        if normalized_command == "ener":
            return await ener_agent.run(text, _agent_triggered_by="user")

        if normalized_command == "content":
            return await content_agent.run(text, _agent_triggered_by="user")

        return await chat_agent.run_chat(chat_id, text, _agent_triggered_by="user")

    async def handle(self, command: str, args: str, chat_id: str) -> str:
        normalized_command = (command or "chat").lower().strip().lstrip("/")
        text = (args or "").strip()

        if COMMAND_AGENT_MAP.get(normalized_command):
            return await self._handle_command(normalized_command, text, chat_id)

        return await chat_agent.run_chat(chat_id, text, _agent_triggered_by="user")

    @log_agent_run("MainAgent")
    async def run(self, chat_id: str, text: str) -> str:
        intent = await self.detect_intent(text)
        if float(intent.get("confidence", 0.5) or 0.5) < 0.6:
            return await chat_agent.run_chat(chat_id, text, _agent_triggered_by="user")

        result = await self._dispatch(
            str(intent.get("agent", "chat")),
            chat_id,
            text,
            triggered_by="user",
        )

        for next_agent in intent.get("chain", []):
            result = await self._dispatch(
                str(next_agent),
                chat_id,
                result,
                triggered_by="agent",
            )

        return result


MAIN_AGENT = MainAgent()
