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
from app.core.agents import COMMAND_AGENT_MAP, log_agent_run


class MainAgent:
    """
    Central intelligence — รับทุก message จาก Telegram
    ตัดสินใจ route ไป sub-agent ที่เหมาะสม
    """

    async def route_free_text(self, chat_id: str, text: str) -> str:
        return await self.run(chat_id, text)

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

    async def _handle_command(self, normalized_command: str, text: str, chat_id: str) -> str:
        if normalized_command == "note":
            return await brain.process_note(text, chat_id, _agent_triggered_by="user")

        if normalized_command == "task":
            title, priority, deadline_hint = self._parse_task_input(text)
            return await task_agent.create_task(
                title,
                priority=priority,
                deadline_hint=deadline_hint,
                _agent_triggered_by="user",
            )

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
        return await chat_agent.run_chat(chat_id, text, _agent_triggered_by="user")


MAIN_AGENT = MainAgent()
