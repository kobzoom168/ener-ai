"""Secretary Agent — routes via department heads to specialist agents."""
from __future__ import annotations

import json
import logging
import re

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.department import RunContext
from app.core.policy import build_system_prompt

logger = logging.getLogger(__name__)

INTENT_SYSTEM = """วิเคราะห์ข้อความและตอบเป็น JSON เท่านั้น:
{"dept": "<แผนก>", "query": "<คำถามที่ส่งต่อ>"}

แผนกที่มี:
- tech: code, server, github, deploy, monitor, bot automation, security
- intel: ข่าว, brainstorm, วิเคราะห์, สรุปสัปดาห์
- ops: email, tasks, todo, health log
- ener: พระ, วิเคราะห์พลังงาน, content TikTok, ดวง, ไพ่
- hq: memory, session, ทั่วไป, ไม่แน่ใจ

ถ้าไม่แน่ใจ ใช้ dept=hq"""

SECRETARY_PERSONA = build_system_prompt(
    """
=== น้องเอ เลขาส่วนตัวของกบ ===
- เรียกตัวเองว่า "เอ"
- เรียกกบว่า "คุณกบ"
- กระชับ ตรงประเด็น เป็นกันเอง
- เมื่อส่งต่อแผนกอื่น → แจ้งสั้นๆ ว่ากำลังจัดการ
- เมื่อตอบเอง → ตอบตรงๆ ไม่อ้อม
"""
)

_last_route: dict[str, str] = {}

_DEPT_TO_MAP_ID: dict[str, str] = {
    "tech": "code",
    "intel": "news",
    "ops": "gmail",
    "ener": "ener",
    "hq": "memory",
}

_AGENT_TO_DEPT: dict[str, str] = {
    "news": "intel",
    "think": "intel",
    "digest": "intel",
    "code": "tech",
    "monitor": "tech",
    "github": "tech",
    "gmail": "ops",
    "tasks": "ops",
    "logs": "ops",
    "ener": "ener",
    "content": "ener",
    "tarot": "ener",
    "memory": "hq",
    "secretary": "hq",
    "briefing": "hq",
}

_DEPT_LABELS: dict[str, str] = {
    "tech": "Tech",
    "intel": "Intel",
    "ops": "Ops",
    "ener": "Ener",
    "hq": "HQ",
}

_last_route: dict = {}

_DEPT_TO_MAP_ID: dict[str, str] = {
    "tech": "code",
    "intel": "news",
    "ops": "gmail",
    "ener": "ener",
    "hq": "memory",
}


def agent_key_to_agent_name(key: str) -> str:
    from app.agents.agent_dispatch import agent_key_to_agent_name as _map

    return _map(key)


async def _get_dept_head(dept_key: str):
    key = str(dept_key or "").lower().strip()
    if key == "tech":
        from app.agents.heads.tech_head import TechHead

        return TechHead()
    if key == "intel":
        from app.agents.heads.intel_head import IntelHead

        return IntelHead()
    if key == "ops":
        from app.agents.heads.ops_head import OpsHead

        return OpsHead()
    if key == "ener":
        from app.agents.heads.ener_head import EnerHead

        return EnerHead()
    if key == "hq":
        from app.agents.heads.hq_head import HqHead

        return HqHead()
    return None


def _keyword_dept_hint(message: str) -> str | None:
    t = str(message or "").lower()
    if re.search(r"(อีเมล|อีเมล์|email|e-mail|inbox|gmail|เช็ค\s*mail|เมล)", t, re.I):
        return "ops"
    if re.search(r"(ข่าว|news|ai\s*วันนี้|เทคโนโลยี)", t, re.I):
        return "intel"
    if re.search(
        r"(server|cpu|ram|disk|ระบบ|monitor|docker|container|github|git)",
        t,
        re.I,
    ):
        return "tech"
    if re.search(r"(งานค้าง|open task|todo|รายการงาน|/tasks?)", t, re.I):
        return "ops"
    if re.search(r"(พระ|ener|tarot|ไพ่|ดวง|tiktok|caption)", t, re.I):
        return "ener"
    if re.search(r"(memory|จำ|ลืม|remember)", t, re.I):
        return "hq"
    return None


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
        dept = str(parsed.get("dept", "") or parsed.get("agent", "hq")).lower().strip()
        if dept in _AGENT_TO_DEPT:
            dept = _AGENT_TO_DEPT[dept]
        query = str(parsed.get("query", message) or message).strip()
        return {"dept": dept or "hq", "query": query or message}
    except Exception:
        return {"dept": "hq", "query": message}


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


@log_agent_run("SecretaryAgent", triggered_by="user")
async def handle_secretary(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return "เอรับทราบแล้วค่ะ บอกได้เลยว่าต้องการอะไร"

    intent = await _detect_intent(text)
    dept_key = str(intent.get("dept", "hq")).lower().strip()
    query = str(intent.get("query", text) or text).strip()

    keyword_dept = _keyword_dept_hint(text)
    if keyword_dept and dept_key in {"", "hq", "secretary"}:
        dept_key = keyword_dept
        query = text

    head = await _get_dept_head(dept_key)
    use_head = head and dept_key in {"tech", "intel", "ops", "ener"}
    if dept_key == "hq" and head and re.search(
        r"(remember|จำ|forget|ลืม|memory|ความจำ|ค้นหา|มีอะไร)",
        query,
        re.I,
    ):
        use_head = True

    if use_head and head:
        ctx = RunContext(query=query, department=dept_key)
        body = str(await head.handle(ctx) or "").strip()
        _last_route["dept"] = dept_key
        _last_route["agent"] = _DEPT_TO_MAP_ID.get(dept_key, dept_key)
        if body and not body.startswith("⚠️"):
            label = _DEPT_LABELS.get(dept_key, dept_key)
            return f"👩‍💼 เอส่งเรื่อง{label}ให้แล้วค่ะ\n\n{body}"
        if body.startswith("⚠️"):
            return f"👩‍💼 {body}"

    context = await _get_context_summary()
    system = SECRETARY_PERSONA + f"\n\nงานค้างตอนนี้:\n{context}"
    reply = await chat(query or text, system=system, agent="SecretaryAgent")
    return str(reply or "เอรับทราบแล้วค่ะ")
