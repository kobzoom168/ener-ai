import re

from app.agents import brain, brainstorm, chat, cost, learn, memory, news, summary, task, voice
from app.core.agents import COMMAND_AGENT_MAP


class MainAgent:
    """
    Central intelligence — รับทุก message จาก Telegram
    ตัดสินใจ route ไป sub-agent ที่เหมาะสม
    """

    async def route_free_text(self, chat_id: str, text: str) -> str:
        text_lower = text.lower()

        task_keywords = ["ต้อง", "เดี๋ยว", "วันนี้", "พรุ่งนี้", "deadline", "จะ", "plan"]
        question_keywords = ["ช่วย", "แนะนำ", "คิด", "วิเคราะห์", "brainstorm", "ไอเดีย"]

        if any(keyword in text or keyword in text_lower for keyword in task_keywords):
            return await brain.process_note(text, chat_id, _agent_triggered_by="user")

        if any(keyword in text or keyword in text_lower for keyword in question_keywords):
            return await chat.run_chat(chat_id, text, _agent_triggered_by="user")

        return await chat.run_chat(chat_id, text, _agent_triggered_by="user")

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

    async def handle(self, command: str, args: str, chat_id: str) -> str:
        normalized_command = (command or "chat").lower().strip().lstrip("/")
        agent_name = COMMAND_AGENT_MAP.get(normalized_command, "MainChatAgent")
        text = (args or "").strip()

        if agent_name == "MainChatAgent":
            return await self.route_free_text(chat_id, text)

        if normalized_command == "note":
            return await brain.process_note(text, chat_id, _agent_triggered_by="user")

        if normalized_command == "task":
            title, priority, deadline_hint = self._parse_task_input(text)
            return await task.create_task(
                title,
                priority=priority,
                deadline_hint=deadline_hint,
                _agent_triggered_by="user",
            )

        if normalized_command == "tasks":
            return await task.list_tasks(_agent_triggered_by="user")

        if normalized_command == "done":
            return await task.complete_task(int(text), _agent_triggered_by="user")

        if normalized_command in {"learn", "mistake"}:
            return await learn.record_lesson(text, _agent_triggered_by="user")

        if normalized_command in {"think", "brainstorm"}:
            return await brainstorm.run_brainstorm(text, _agent_triggered_by="user")

        if normalized_command == "park":
            return await memory.park_idea(text, _agent_triggered_by="user")

        if normalized_command == "search":
            return await memory.search_memory(text, _agent_triggered_by="user")

        if normalized_command == "remember":
            return await memory.remember_memory(text, _agent_triggered_by="user")

        if normalized_command == "forget":
            return await memory.forget_memory(text, _agent_triggered_by="user")

        if normalized_command == "memory":
            return await memory.list_memory(_agent_triggered_by="user")

        if normalized_command == "voice":
            return await voice.handle_voice_command(chat_id, text, _agent_triggered_by="user")

        if normalized_command == "today":
            return await summary.generate_daily_summary(_agent_triggered_by="user")

        if normalized_command == "week":
            return await summary.generate_weekly_summary(_agent_triggered_by="user")

        if normalized_command == "news":
            return await news.fetch_and_summarize(_agent_triggered_by="user")

        if normalized_command == "cost":
            return await cost.get_cost_report(chat_id, _agent_triggered_by="user")

        return await chat.run_chat(chat_id, text, _agent_triggered_by="user")


MAIN_AGENT = MainAgent()
