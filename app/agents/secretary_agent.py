"""Secretary Agent — orchestrates all other agents for workspace chat."""
from __future__ import annotations

import json
import logging
import re

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.policy import build_system_prompt

logger = logging.getLogger(__name__)

INTENT_SYSTEM = """วิเคราะห์ว่าข้อความนี้ต้องการ agent ไหน ตอบเป็น JSON เท่านั้น:
{"agent": "<ชื่อ>", "query": "<คำถามที่ส่งต่อ>"}

agents ที่มี:
- news: ข่าว, เทคโนโลยี, AI, security
- code: เขียนโปรแกรม, แก้ bug, server, deploy
- ener: พระเครื่อง, วิเคราะห์พลังงาน, ener scan
- content: เขียน script, caption, TikTok, Facebook, โพสต์
- tasks: งานที่ต้องทำ, todo, เพิ่มงาน, ปิดงาน
- monitor: server status, logs, errors, CPU RAM
- memory: จำ, ค้นหาสิ่งที่เคยคุย
- think: brainstorm, วิเคราะห์เชิงลึก, เปรียบเทียบ
- gmail: อีเมล, inbox
- tarot: ไพ่, ดวง, พยากรณ์
- secretary: ทั่วไป, สรุปงาน, ไม่ชัดเจน"""

SECRETARY_PERSONA = build_system_prompt(
    """
=== น้องเอ เลขาส่วนตัวของกบ ===
- เรียกตัวเองว่า "เอ"
- เรียกกบว่า "คุณกบ"
- กระชับ ตรงประเด็น เป็นกันเอง
- เมื่อส่งต่อ agent อื่น → แจ้งสั้นๆ ว่ากำลังจัดการ
- เมื่อตอบเอง → ตอบตรงๆ ไม่อ้อม
"""
)

_KEY_TO_AGENT = {
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
    "secretary": "SecretaryAgent",
}


def agent_key_to_agent_name(key: str) -> str:
    return _KEY_TO_AGENT.get(str(key or "").lower().strip(), "MainChatAgent")


async def _emit_office_event(
    from_agent: str,
    to_agent: str,
    message: str,
    event_type: str = "route",
) -> None:
    from app.core.event_log import log_event

    ctx = json.dumps(
        {"from": from_agent, "to": to_agent, "type": event_type},
        ensure_ascii=False,
    )
    try:
        await log_event(
            agent_name=to_agent,
            event_type=event_type,
            summary=(message or "")[:120],
            context=ctx,
            triggered_by=from_agent,
            result="success",
        )
    except Exception as exc:
        logger.warning("office event emit failed: %s", exc)


_AGENT_LABELS = {
    "news": "ข่าว",
    "code": "โค้ด",
    "ener": "พระ/พลังงาน",
    "content": "คอนเทนต์",
    "tasks": "งาน",
    "monitor": "ระบบ",
    "memory": "ความจำ",
    "think": "วิเคราะห์",
    "gmail": "อีเมล",
    "tarot": "ไพ่",
}


async def _detect_intent(message: str) -> dict[str, str]:
    try:
        raw = await chat(
            message,
            system=INTENT_SYSTEM,
            agent="SecretaryIntent",
            preferred_model="gemini",
        )
        clean = re.sub(r"```json?|```", "", str(raw or "")).strip()
        parsed = json.loads(clean)
        agent = str(parsed.get("agent", "secretary")).lower().strip()
        query = str(parsed.get("query", message) or message).strip()
        return {"agent": agent, "query": query or message}
    except Exception:
        return {"agent": "secretary", "query": message}


def _keyword_agent_hint(message: str) -> str | None:
    """Fast path when intent model picks secretary but message is clearly scoped."""
    t = str(message or "").lower()
    if re.search(r"(อีเมล|อีเมล์|email|e-mail|inbox|gmail|เช็ค\s*mail|เมล)", t, re.I):
        return "gmail"
    if re.search(r"(ข่าว|news|ai\s*วันนี้|เทคโนโลยี)", t, re.I):
        return "news"
    if re.search(
        r"(server|cpu|ram|disk|ระบบ|monitor|log|error|docker|container)",
        t,
        re.I,
    ):
        return "monitor"
    if re.search(r"(งานค้าง|open task|todo|รายการงาน|/tasks?)", t, re.I):
        return "tasks"
    return None


async def _get_context_summary() -> str:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT title, priority FROM tasks WHERE status='open' ORDER BY "
            "CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END LIMIT 5"
        )
        tasks = await cur.fetchall()
    if not tasks:
        return "ไม่มีงานค้าง"
    return "\n".join(f"- [{r['priority']}] {r['title']}" for r in tasks)


async def _call_agent_by_key(agent_key: str, query: str) -> str | None:
    """Explicit routing to each agent's real entry function."""
    key = str(agent_key or "").lower().strip()
    q = str(query or "").strip()

    try:
        if key == "news":
            from app.agents.news import fetch_and_summarize

            return await fetch_and_summarize(_agent_triggered_by="user")

        if key == "gmail":
            from app.agents.gmail_agent import summarize_emails

            return await summarize_emails()

        if key == "code":
            from app.agents import code_agent

            return await code_agent.run(q)

        if key == "ener":
            from app.agents import ener_agent

            return await ener_agent.run(q)

        if key == "content":
            from app.agents import content_agent

            return await content_agent.run(q)

        if key == "tasks":
            from app.agents import task as task_agent

            lowered = q.lower()
            if re.search(r"\b(done|เสร็จ|ปิด)\b", lowered):
                match = re.search(r"\d+", q)
                if match:
                    return await task_agent.complete_task(int(match.group()))
            if re.search(r"\b(add|เพิ่ม|สร้าง|new task)\b", lowered):
                title = re.sub(
                    r"^(?:add|เพิ่ม|สร้าง|new task)\s*",
                    "",
                    q,
                    flags=re.IGNORECASE,
                ).strip()
                if title:
                    return await task_agent.create_task(title)
            return await task_agent.list_tasks()

        if key == "monitor":
            from app.agents import monitor_agent

            lowered = q.lower()
            if "log" in lowered:
                lines = 20
                match = re.search(r"\d+", q)
                if match:
                    lines = max(5, min(int(match.group()), 200))
                return await monitor_agent.cmd_logs(lines=lines)
            if "error" in lowered or "ผิดพลาด" in lowered:
                return await monitor_agent.cmd_errors()
            if any(
                token in lowered
                for token in ("cpu", "ram", "disk", "memory", "server")
            ):
                return await monitor_agent.format_nl_resource_report(
                    monitor_agent.get_server_stats()
                )
            return await monitor_agent.cmd_status()

        if key == "memory":
            from app.agents import memory as memory_agent

            lowered = q.lower()
            if re.search(r"\b(remember|จำ)\b", lowered):
                text = re.sub(
                    r"^(?:remember|จำ)\s*", "", q, flags=re.IGNORECASE
                ).strip()
                return await memory_agent.remember_memory(text or q)
            if re.search(r"\b(forget|ลืม)\b", lowered):
                text = re.sub(
                    r"^(?:forget|ลืม)\s*", "", q, flags=re.IGNORECASE
                ).strip()
                return await memory_agent.forget_memory(text or q)
            if re.search(r"\b(list|ทั้งหมด|มีอะไร)\b", lowered):
                return await memory_agent.list_memory()
            return await memory_agent.search_memory(q)

        if key == "think":
            from app.agents.brainstorm import run_brainstorm

            return await run_brainstorm(q)

        if key == "tarot":
            from app.agents.tarot_agent import read_cards

            spread = "single"
            lowered = q.lower()
            if "3" in q or "สาม" in q or "three" in lowered:
                spread = "three"
            elif "5" in q or "ห้า" in q or "celtic" in lowered:
                spread = "celtic"
            return await read_cards(question=q, spread=spread)

    except ImportError as exc:
        return f"⚠️ ยังไม่ได้เชื่อม agent นี้: {exc}"
    except Exception as exc:
        logger.warning("Secretary agent call failed for %s: %s", key, exc)
        return f"⚠️ {key} agent error: {str(exc)[:150]}"

    return None


@log_agent_run("SecretaryAgent", triggered_by="user")
async def handle_secretary(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return "เอรับทราบแล้วค่ะ บอกได้เลยว่าต้องการอะไร"

    intent = await _detect_intent(text)
    agent_key = str(intent.get("agent", "secretary")).lower().strip()
    query = str(intent.get("query", text) or text).strip()

    keyword_hint = _keyword_agent_hint(text)
    if keyword_hint and agent_key in {"", "secretary", "code"}:
        agent_key = keyword_hint
        query = text

    if agent_key != "secretary":
        target_agent = agent_key_to_agent_name(agent_key)
        await _emit_office_event(
            "SecretaryAgent",
            target_agent,
            f"ส่งงาน: {query[:80]}",
            "route",
        )
        result = await _call_agent_by_key(agent_key, query)
        body = str(result or "").strip()
        if body and not body.startswith("⚠️"):
            await _emit_office_event(
                target_agent,
                "SecretaryAgent",
                "ส่งผลกลับ ✓",
                "complete",
            )
            label = _AGENT_LABELS.get(agent_key, agent_key)
            return f"👩‍💼 เอจัดการเรื่อง{label}ให้แล้วค่ะ\n\n{body}"
        if body.startswith("⚠️"):
            return f"👩‍💼 {body}"

    context = await _get_context_summary()
    system = SECRETARY_PERSONA + f"\n\nงานค้างตอนนี้:\n{context}"
    reply = await chat(query or text, system=system, agent="SecretaryAgent")
    return str(reply or "เอรับทราบแล้วค่ะ")
