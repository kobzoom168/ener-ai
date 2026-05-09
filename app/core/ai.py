import asyncio
import json
import time
import anthropic
import httpx
from google import genai
from groq import AsyncGroq
from app.core.database import get_db
from app.core.config import settings
from app.core.policy import BASE_SYSTEM_PROMPT, TASK_MODEL_MAP

_PRIMARY_MODEL = "claude-haiku-4-5-20251001"
_ACTIVE_MODEL_KEY = "active_model"
_MODEL_LABELS = {
    "haiku": "Claude Haiku",
    "groq": "Groq",
    "gemini": "Gemini Flash",
    "qwen3b": "Qwen 3B",
    "qwen7b": "Qwen 7B",
}
_OLLAMA_MODEL_MAP = {
    "qwen3b": "qwen2.5:3b",
    "qwen7b": "qwen2.5:7b",
}
_VALID_MODELS = {"haiku", "groq", "gemini", "qwen3b", "qwen7b"}
_FALLBACK_SEQUENCE = ["groq", "haiku", "qwen3b"]


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
        return ""
    model_key = str(row["value"]).strip().lower()
    if model_key in _VALID_MODELS:
        return model_key
    return ""


async def _log_ai_run(
    agent: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    response_time_ms: int,
    success: bool,
):
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO ai_runs (
                agent, model, prompt_tokens, completion_tokens, response_time_ms, estimated_cost_thb, success
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent,
                model,
                prompt_tokens,
                completion_tokens,
                response_time_ms,
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


def _groq_messages(prompt: str, system: str, messages: list[dict[str, str]] | None) -> list[dict[str, str]]:
    payload_messages = [{"role": "system", "content": system}]
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                payload_messages.append({"role": role, "content": message.get("content", "")})
    payload_messages.append({"role": "user", "content": prompt})
    return payload_messages


def _gemini_contents(prompt: str, system: str, messages: list[dict[str, str]] | None) -> str:
    sections = []
    if system:
        sections.append(f"System:\n{system}")
    if messages:
        history_lines = []
        for message in messages:
            role = str(message.get("role", "user")).strip().lower()
            if role not in {"user", "assistant"}:
                continue
            history_lines.append(f"{role}:\n{message.get('content', '')}")
        if history_lines:
            sections.append("Conversation history:\n" + "\n\n".join(history_lines))
    sections.append(f"User:\n{prompt}")
    return "\n\n".join(sections)


def get_model_availability() -> dict[str, bool]:
    return {
        "haiku": bool(settings.anthropic_api_key),
        "groq": bool(settings.groq_api_key),
        "gemini": bool(settings.gemini_api_key),
        "qwen3b": True,
        "qwen7b": True,
    }


def _default_model(availability: dict[str, bool] | None = None) -> str:
    available = availability or get_model_availability()
    if available.get("groq"):
        return "groq"
    if available.get("haiku"):
        return "haiku"
    if available.get("gemini"):
        return "gemini"
    return "qwen3b"


def _resolve_requested_model(agent: str, active_model: str) -> str:
    if active_model in _VALID_MODELS:
        return active_model
    task_default = TASK_MODEL_MAP.get(agent)
    if task_default in _VALID_MODELS:
        return task_default
    return _default_model()


def _model_candidates(requested_model: str) -> list[str]:
    candidates = [requested_model]
    for model in _FALLBACK_SEQUENCE:
        if model not in candidates:
            candidates.append(model)
    if "qwen3b" not in candidates:
        candidates.append("qwen3b")
    return candidates


def _extract_json_payload(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        for block in cleaned.split("```"):
            candidate = block.strip()
            if not candidate:
                continue
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                return candidate
    if cleaned.startswith("{") or cleaned.startswith("["):
        return cleaned

    object_start = cleaned.find("{")
    object_end = cleaned.rfind("}")
    if object_start != -1 and object_end != -1 and object_end > object_start:
        return cleaned[object_start : object_end + 1]

    array_start = cleaned.find("[")
    array_end = cleaned.rfind("]")
    if array_start != -1 and array_end != -1 and array_end > array_start:
        return cleaned[array_start : array_end + 1]

    return cleaned


async def _call_anthropic(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    started_at = time.perf_counter()
    try:
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
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(agent, "haiku", input_tokens, output_tokens, elapsed_ms, True)
        return text
    except Exception:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(agent, "haiku", 0, 0, elapsed_ms, False)
        raise


async def _call_groq(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    started_at = time.perf_counter()
    try:
        client = AsyncGroq(api_key=settings.groq_api_key)
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=_groq_messages(prompt, system, messages),
        )
        usage = response.usage
        await _log_ai_run(
            agent,
            "groq",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            int((time.perf_counter() - started_at) * 1000),
            True,
        )
        return response.choices[0].message.content or ""
    except Exception:
        await _log_ai_run(
            agent,
            "groq",
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            False,
        )
        raise


async def _call_gemini(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    started_at = time.perf_counter()
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        contents = _gemini_contents(prompt, system, messages)

        def _generate():
            return client.models.generate_content(
                model="gemini-1.5-flash",
                contents=contents,
            )

        response = await asyncio.to_thread(_generate)
        usage = getattr(response, "usage_metadata", None)
        await _log_ai_run(
            agent,
            "gemini",
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
            int((time.perf_counter() - started_at) * 1000),
            True,
        )
        return getattr(response, "text", "") or ""
    except Exception:
        await _log_ai_run(
            agent,
            "gemini",
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            False,
        )
        raise


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
    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json=payload,
            )
            response.raise_for_status()
            await _log_ai_run(
                agent,
                model_key,
                0,
                0,
                int((time.perf_counter() - started_at) * 1000),
                True,
            )
            return response.json()["message"]["content"]
    except Exception:
        await _log_ai_run(
            agent,
            model_key,
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            False,
        )
        raise


async def chat(
    prompt: str,
    system: str = BASE_SYSTEM_PROMPT,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
) -> str:
    availability = get_model_availability()
    active_model = (await get_active_model()) or _default_model(availability)
    requested_model = _resolve_requested_model(agent, active_model)
    candidates = _model_candidates(requested_model)
    if active_model in _VALID_MODELS and active_model in candidates:
        candidates.remove(active_model)
        candidates.insert(0, active_model)

    for candidate in candidates:
        if candidate in {"haiku", "groq", "gemini"} and not availability.get(candidate, False):
            continue
        try:
            if candidate == "haiku":
                return await _call_anthropic(prompt, system, messages, agent)
            if candidate == "groq":
                return await _call_groq(prompt, system, messages, agent)
            if candidate == "gemini":
                return await _call_gemini(prompt, system, messages, agent)
            if candidate in {"qwen3b", "qwen7b"}:
                return await _call_ollama(prompt, system, messages, agent, candidate)
        except Exception:
            continue

    default_candidate = _default_model(availability)
    if default_candidate == "haiku":
        return await _call_anthropic(prompt, system, messages, agent)
    if default_candidate == "groq":
        return await _call_groq(prompt, system, messages, agent)
    if default_candidate == "gemini":
        return await _call_gemini(prompt, system, messages, agent)
    return await _call_ollama(prompt, system, messages, agent, default_candidate)


async def chat_json(
    prompt: str,
    system: str = BASE_SYSTEM_PROMPT,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
) -> dict:
    full_system = system + "\n\nตอบเป็น JSON เท่านั้น ไม่มีข้อความอื่น"
    raw = await chat(prompt, system=full_system, agent=agent, messages=messages)
    return json.loads(_extract_json_payload(raw))
