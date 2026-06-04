"""Core types and base class for department-based agent orchestration."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

# ─── Typed Context ────────────────────────────────────────────────


class RunContext(BaseModel):
    session_id: str = ""
    user_id: str = "owner"
    query: str
    department: str = ""
    history: list[dict] = Field(default_factory=list)
    is_sensitive: bool = False
    model_tier: Literal[1, 2, 3] = 1
    requires_approval: bool = False
    metadata: dict = Field(default_factory=dict)


class AgentResult(BaseModel):
    agent: str
    success: bool
    content: str
    data: dict = Field(default_factory=dict)


# ─── Risk/Policy Layer ────────────────────────────────────────────

SENSITIVE_KEYWORDS: dict[str, float] = {
    "bot": 0.6,
    "auto": 0.4,
    "automation": 0.4,
    "scrape": 0.5,
    "bypass": 0.9,
    "anti-detection": 0.8,
    "proxy": 0.5,
    "fake": 0.7,
    "ปั๊ม": 0.8,
    "flood": 0.7,
    "spam": 0.6,
    "grey area": 0.6,
    "เทา": 0.5,
    "หาเงิน": 0.3,
    "viral hack": 0.8,
    "engagement boost": 0.7,
    "exploit": 1.0,
    "inject": 0.8,
    "brute force": 0.9,
    "pentest": 0.5,
    "sqlmap": 0.9,
    "metasploit": 0.9,
    "payload": 0.7,
}

SAFE_IN_DEPT: dict[str, list[str]] = {
    "tech": ["scrape", "automation", "bot", "pentest"],
    "hq": ["memory", "session"],
}


async def assess_risk(query: str, department: str = "") -> tuple[float, str]:
    q = query.lower()
    score = 0.0
    reasons: list[str] = []
    safe_kws = SAFE_IN_DEPT.get(department.lower(), [])

    for kw, weight in SENSITIVE_KEYWORDS.items():
        if kw in q and kw not in safe_kws:
            score += weight
            reasons.append(f"keyword:{kw}")

    score = min(score, 1.0)

    if 0.3 <= score <= 0.7:
        try:
            from app.core.ai import chat

            verdict = await chat(
                f"Does this request need uncensored AI? Answer YES or NO only.\n'{query}'",
                system="You are a content classifier. Answer only YES or NO.",
                agent="RiskClassifier",
            )
            if "YES" in str(verdict or "").upper():
                score = max(score, 0.75)
                reasons.append("ai_judge:YES")
        except Exception:
            pass

    reason = ", ".join(reasons) if reasons else "none"
    return score, reason


def choose_model_tier(risk_score: float, task_type: str = "general") -> int:
    if risk_score >= 0.7:
        return 3
    if task_type == "code":
        return 2
    return 1


TIER_MODEL_MAP: dict[int, str] = {
    1: "gemini-flash-lite",
    2: "deepseek-v4",
    3: "featherless-abliterated",
}


async def _audit_log(query: str, score: float, reason: str, model: str) -> None:
    try:
        from app.core.event_log import log_event

        ctx = json.dumps(
            {"score": round(score, 2), "reason": reason, "model": model},
            ensure_ascii=False,
        )
        await log_event(
            agent_name=model,
            event_type="nofilter_audit",
            summary=query[:120],
            context=ctx,
            triggered_by="policy_layer",
            result="success",
        )
    except Exception:
        pass


async def emit_office_route(
    from_agent: str,
    to_agent: str,
    message: str,
    event_type: str = "route",
) -> None:
    """Emit route/complete events for pixel office SSE UI."""
    try:
        from app.core.event_log import log_event

        ctx = json.dumps(
            {"from": from_agent, "to": to_agent, "type": event_type},
            ensure_ascii=False,
        )
        await log_event(
            agent_name=to_agent,
            event_type=event_type,
            summary=(message or "")[:120],
            context=ctx,
            triggered_by=from_agent,
            result="success",
        )
    except Exception:
        pass


# ─── Base Department Head ─────────────────────────────────────────


class BaseDepartmentHead:
    name: str = "base"
    dept_key: str = "base"
    emoji: str = "🏢"
    routing_rules: list[tuple[str, str]] = []
    default_agent: str = ""

    async def route(self, ctx: RunContext) -> str:
        for pattern, agent_key in self.routing_rules:
            if re.search(pattern, ctx.query, re.IGNORECASE):
                return agent_key
        return self.default_agent

    async def call_agent(self, agent_key: str, ctx: RunContext) -> AgentResult:
        from app.agents.agent_dispatch import dispatch_agent

        return await dispatch_agent(agent_key, ctx.query)

    async def synthesize(self, results: list[AgentResult], ctx: RunContext) -> str:
        if len(results) == 1:
            return results[0].content
        parts = [f"**{r.agent}**\n{r.content}" for r in results if r.success]
        return "\n\n---\n\n".join(parts) if parts else "ไม่ได้รับผลลัพธ์"

    async def handle(self, ctx: RunContext) -> str:
        from app.agents.agent_dispatch import agent_key_to_agent_name

        ctx.department = self.dept_key

        risk_score, reason = await assess_risk(ctx.query, self.dept_key)
        ctx.is_sensitive = risk_score >= 0.7
        ctx.model_tier = choose_model_tier(
            risk_score,
            "code" if self.dept_key == "tech" else "general",
        )

        if ctx.is_sensitive:
            await _audit_log(
                ctx.query,
                risk_score,
                reason,
                TIER_MODEL_MAP[ctx.model_tier],
            )

        agent_key = await self.route(ctx)
        target_name = agent_key_to_agent_name(agent_key)

        await emit_office_route(
            "SecretaryAgent",
            target_name,
            f"ส่งงาน: {ctx.query[:80]}",
            "route",
        )

        result = await self.call_agent(agent_key, ctx)

        if result.success and not str(result.content).startswith("⚠️"):
            await emit_office_route(
                target_name,
                "SecretaryAgent",
                "ส่งผลกลับ ✓",
                "complete",
            )

        return result.content
