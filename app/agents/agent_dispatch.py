"""Shared explicit dispatch from agent_key → real agent entrypoints."""
from __future__ import annotations

import re

from app.core.department import AgentResult

AGENT_KEY_TO_NAME: dict[str, str] = {
    "news": "NewsAgent",
    "code": "CodeAgent",
    "ener": "EnerAgent",
    "content": "ContentAgent",
    "tasks": "TaskAgent",
    "monitor": "MonitorAgent",
    "memory": "MemoryAgent",
    "think": "ThinkTeam",
    "gmail": "GmailAgent",
    "tarot": "TarotAgent",
    "github": "GithubAgent",
    "digest": "DigestAgent",
    "logs": "LogKeeper",
}


def agent_key_to_agent_name(key: str) -> str:
    return AGENT_KEY_TO_NAME.get(str(key or "").lower().strip(), "MainChatAgent")


async def dispatch_agent(agent_key: str, query: str) -> AgentResult:
    key = str(agent_key or "").lower().strip()
    q = str(query or "").strip()
    agent_name = agent_key_to_agent_name(key)

    try:
        if key == "news":
            from app.agents.news import fetch_and_summarize

            body = await fetch_and_summarize(_agent_triggered_by="user")
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "gmail":
            from app.agents.gmail_agent import summarize_emails

            body = await summarize_emails()
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "code":
            from app.agents import code_agent

            body = await code_agent.run(q)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "ener":
            from app.agents import ener_agent

            body = await ener_agent.run(q)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "content":
            from app.agents import content_agent

            body = await content_agent.run(q)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "tasks":
            from app.agents import task as task_agent

            lowered = q.lower()
            if re.search(r"\b(done|เสร็จ|ปิด)\b", lowered):
                match = re.search(r"\d+", q)
                if match:
                    body = await task_agent.complete_task(int(match.group()))
                    return AgentResult(agent=agent_name, success=True, content=str(body))
            if re.search(r"\b(add|เพิ่ม|สร้าง|new task)\b", lowered):
                title = re.sub(
                    r"^(?:add|เพิ่ม|สร้าง|new task)\s*",
                    "",
                    q,
                    flags=re.IGNORECASE,
                ).strip()
                if title:
                    body = await task_agent.create_task(title)
                    return AgentResult(agent=agent_name, success=True, content=str(body))
            body = await task_agent.list_tasks()
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "monitor":
            from app.agents import monitor_agent

            lowered = q.lower()
            if "log" in lowered:
                lines = 20
                match = re.search(r"\d+", q)
                if match:
                    lines = max(5, min(int(match.group()), 200))
                body = await monitor_agent.cmd_logs(lines=lines)
            elif "error" in lowered or "ผิดพลาด" in lowered:
                body = await monitor_agent.cmd_errors()
            elif any(
                token in lowered
                for token in ("cpu", "ram", "disk", "memory", "server")
            ):
                body = await monitor_agent.format_nl_resource_report(
                    monitor_agent.get_server_stats()
                )
            else:
                body = await monitor_agent.cmd_status()
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "memory":
            from app.agents import memory as memory_agent

            lowered = q.lower()
            if re.search(r"\b(remember|จำ)\b", lowered):
                text = re.sub(
                    r"^(?:remember|จำ)\s*", "", q, flags=re.IGNORECASE
                ).strip()
                body = await memory_agent.remember_memory(text or q)
            elif re.search(r"\b(forget|ลืม)\b", lowered):
                text = re.sub(
                    r"^(?:forget|ลืม)\s*", "", q, flags=re.IGNORECASE
                ).strip()
                body = await memory_agent.forget_memory(text or q)
            elif re.search(r"\b(list|ทั้งหมด|มีอะไร)\b", lowered):
                body = await memory_agent.list_memory()
            else:
                body = await memory_agent.search_memory(q)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "think":
            from app.agents.brainstorm import run_brainstorm

            body = await run_brainstorm(q)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "tarot":
            from app.agents.tarot_agent import read_cards

            spread = "single"
            lowered = q.lower()
            if "3" in q or "สาม" in q or "three" in lowered:
                spread = "three"
            elif "5" in q or "ห้า" in q or "celtic" in lowered:
                spread = "celtic"
            body = await read_cards(question=q, spread=spread)
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "github":
            from app.agents import github_agent

            lowered = q.lower()
            if "pr" in lowered or "pull" in lowered:
                body = await github_agent.list_prs()
            elif "issue" in lowered:
                body = await github_agent.list_issues()
            else:
                body = await github_agent.list_repos()
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "digest":
            from app.core.ai import chat

            body = await chat(q, agent="DigestAgent")
            return AgentResult(agent=agent_name, success=True, content=str(body))

        if key == "logs":
            from app.agents.log_keeper import analyze_agent_health

            body = await analyze_agent_health()
            return AgentResult(agent=agent_name, success=True, content=str(body))

    except ImportError as exc:
        return AgentResult(
            agent=agent_name,
            success=False,
            content=f"⚠️ ยังไม่ได้เชื่อม agent นี้: {exc}",
        )
    except Exception as exc:
        return AgentResult(
            agent=agent_name,
            success=False,
            content=f"⚠️ {key} agent error: {str(exc)[:150]}",
        )

    return AgentResult(
        agent=agent_name,
        success=False,
        content=f"⚠️ ไม่รู้จัก agent '{key}'",
    )
