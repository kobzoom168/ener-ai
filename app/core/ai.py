import json
import anthropic
import httpx
from app.core.database import get_db
from app.core.config import settings
from app.core.policy import AI_PERSONALITY

_PRIMARY_MODEL = "claude-haiku-4-5-20251001"


def _estimate_cost_thb(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model != _PRIMARY_MODEL:
        return 0.0
    usd_cost = (prompt_tokens / 1_000_000 * 0.80) + (completion_tokens / 1_000_000 * 4.00)
    return usd_cost * 33


async def _log_ai_run(
    agent: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    success: bool,
):
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO ai_runs (
                agent, model, prompt_tokens, completion_tokens, estimated_cost_thb, success
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                agent,
                model,
                prompt_tokens,
                completion_tokens,
                _estimate_cost_thb(model, prompt_tokens, completion_tokens),
                1 if success else 0,
            ),
        )
        await db.commit()


def _anthropic_messages(prompt: str, messages: list[dict[str, str]] | None) -> list[dict[str, str]]:
    payload_messages = []
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                payload_messages.append({"role": role, "content": message.get("content", "")})
    payload_messages.append({"role": "user", "content": prompt})
    return payload_messages


async def _call_anthropic(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=_PRIMARY_MODEL,
        max_tokens=2048,
        system=system,
        messages=_anthropic_messages(prompt, messages),
    )
    text = "".join(getattr(block, "text", "") for block in response.content)
    input_tokens = getattr(response.usage, "input_tokens", 0)
    output_tokens = getattr(response.usage, "output_tokens", 0)
    await _log_ai_run(agent, _PRIMARY_MODEL, input_tokens, output_tokens, True)
    return text


async def _call_ollama(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    payload_messages = [{"role": "system", "content": system}]
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                payload_messages.append({"role": role, "content": message.get("content", "")})
    payload_messages.append({"role": "user", "content": prompt})
    payload = {
        "model": settings.ollama_model,
        "messages": payload_messages,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            await _log_ai_run(agent, settings.ollama_model, 0, 0, True)
            return response.json()["message"]["content"]
    except Exception:
        await _log_ai_run(agent, settings.ollama_model, 0, 0, False)
        raise


async def chat(
    prompt: str,
    system: str = AI_PERSONALITY,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
) -> str:
    if settings.anthropic_api_key:
        try:
            return await _call_anthropic(prompt, system, messages, agent)
        except Exception:
            await _log_ai_run(agent, _PRIMARY_MODEL, 0, 0, False)
    return await _call_ollama(prompt, system, messages, agent)


async def chat_json(
    prompt: str,
    system: str = AI_PERSONALITY,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
) -> dict:
    full_system = system + "\n\nตอบเป็น JSON เท่านั้น ไม่มีข้อความอื่น"
    raw = await chat(prompt, system=full_system, agent=agent, messages=messages)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
