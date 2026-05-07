import json
import anthropic
import httpx
from app.core.database import get_db
from app.core.config import settings
from app.core.policy import AI_PERSONALITY

_PRIMARY_MODEL = "claude-haiku-4-5-20251001"
_ACTIVE_MODEL_KEY = "active_model"
_MODEL_LABELS = {
    "haiku": "Claude Haiku",
    "qwen3b": "Qwen 3B",
    "qwen7b": "Qwen 7B",
}
_OLLAMA_MODEL_MAP = {
    "qwen3b": "qwen2.5:3b",
    "qwen7b": "qwen2.5:7b",
}


def _estimate_cost_thb(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    if model != "haiku":
        return 0.0
    usd_cost = (prompt_tokens / 1_000_000 * 0.80) + (completion_tokens / 1_000_000 * 4.00)
    return usd_cost * 33


def get_model_label(model_key: str) -> str:
    return _MODEL_LABELS.get(model_key, "Claude Haiku")


async def get_active_model() -> str:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM memories WHERE key = ? LIMIT 1",
            (_ACTIVE_MODEL_KEY,),
        )
        row = await cursor.fetchone()
    if not row:
        return "haiku"
    model_key = str(row["value"]).strip().lower()
    if model_key in {"haiku", "qwen3b", "qwen7b"}:
        return model_key
    return "haiku"


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
    await _log_ai_run(agent, "haiku", input_tokens, output_tokens, True)
    return text


async def _call_ollama(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
    model_key: str,
) -> str:
    payload_messages = [{"role": "system", "content": system}]
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                payload_messages.append({"role": role, "content": message.get("content", "")})
    payload_messages.append({"role": "user", "content": prompt})
    model_name = _OLLAMA_MODEL_MAP.get(model_key, settings.ollama_model)
    payload = {
        "model": model_name,
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
            await _log_ai_run(agent, model_key, 0, 0, True)
            return response.json()["message"]["content"]
    except Exception:
        await _log_ai_run(agent, model_key, 0, 0, False)
        raise


async def chat(
    prompt: str,
    system: str = AI_PERSONALITY,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
) -> str:
    active_model = await get_active_model()
    if active_model == "haiku":
        if settings.anthropic_api_key:
            try:
                return await _call_anthropic(prompt, system, messages, agent)
            except Exception:
                await _log_ai_run(agent, "haiku", 0, 0, False)
        return await _call_ollama(prompt, system, messages, agent, "qwen7b")
    return await _call_ollama(prompt, system, messages, agent, active_model)


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
