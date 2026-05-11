import asyncio
import json
import time
import threading
from typing import AsyncGenerator
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
    "sonnet": "Claude Sonnet 4.6",
    "opus": "Claude Opus 4.7",
    "gemini-pro": "Gemini 2.0 Pro",
    "llama4": "Llama 4 Scout (Groq)",
    "grok": "Grok 4.3 (xAI)",
    "deepseek-direct": "DeepSeek V4 Flash",
    "kimi": "Kimi K2 (Moonshot)",
    "gpt-4o-mini": "GPT-4o Mini (OpenAI)",
    "gpt-4o": "GPT-4o (OpenAI)",
}
_OLLAMA_MODEL_MAP = {
    "qwen3b": "qwen2.5:3b",
    "qwen7b": "qwen2.5:7b",
}
_VALID_MODELS = {
    "haiku", "groq", "gemini", "qwen3b", "qwen7b",
    "sonnet", "opus", "gemini-pro", "llama4",
    "grok", "deepseek-direct", "kimi",
    "gpt-4o-mini", "gpt-4o",
}
_FALLBACK_SEQUENCE = ["groq", "haiku", "qwen3b"]


def _estimate_cost_thb(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    normalized = _normalize_model_name(model)
    if normalized != "haiku":
        return 0.0
    usd_cost = (prompt_tokens / 1_000_000 * 0.80) + (completion_tokens / 1_000_000 * 4.00)
    return usd_cost * 33


def _normalize_model_name(model: str) -> str:
    lowered = str(model or "").strip().lower()
    if "haiku" in lowered:
        return "haiku"
    if "groq" in lowered or "llama" in lowered:
        return "groq"
    if "gemini" in lowered:
        return "gemini"
    if "qwen" in lowered:
        if "3b" in lowered:
            return "qwen3b"
        return "qwen7b"
    return str(model or "").strip()


def get_model_label(model_key: str) -> str:
    normalized = _normalize_model_name(model_key)
    return _MODEL_LABELS.get(normalized, "Claude Haiku")


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
    normalized = _normalize_model_name(model)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO ai_runs (
                agent, model, prompt_tokens, completion_tokens, response_time_ms, estimated_cost_thb, success
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent,
                normalized,
                prompt_tokens,
                completion_tokens,
                response_time_ms,
                _estimate_cost_thb(normalized, prompt_tokens, completion_tokens),
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
        "sonnet": bool(settings.anthropic_api_key),
        "opus": bool(settings.anthropic_api_key),
        "gemini-pro": bool(settings.gemini_api_key),
        "llama4": bool(settings.groq_api_key),
        "grok": bool(settings.xai_api_key),
        "deepseek-direct": bool(settings.deepseek_api_key),
        "kimi": bool(settings.moonshot_api_key),
        "gpt-4o-mini": bool(settings.openai_api_key),
        "gpt-4o": bool(settings.openai_api_key),
    }


async def get_model_availability_async() -> dict[str, bool]:
    """Like get_model_availability() but also checks DB config for keys not in .env."""
    from app.core.database import get_config
    xai = settings.xai_api_key or await get_config("xai_api_key", "")
    deepseek = settings.deepseek_api_key or await get_config("deepseek_api_key", "")
    moonshot = settings.moonshot_api_key or await get_config("moonshot_api_key", "")
    openai = settings.openai_api_key or await get_config("openai_api_key", "")
    return {
        "haiku": bool(settings.anthropic_api_key),
        "groq": bool(settings.groq_api_key),
        "gemini": bool(settings.gemini_api_key),
        "qwen3b": True,
        "qwen7b": True,
        "sonnet": bool(settings.anthropic_api_key),
        "opus": bool(settings.anthropic_api_key),
        "gemini-pro": bool(settings.gemini_api_key),
        "llama4": bool(settings.groq_api_key),
        "grok": bool(xai),
        "deepseek-direct": bool(deepseek),
        "kimi": bool(moonshot),
        "gpt-4o-mini": bool(openai),
        "gpt-4o": bool(openai),
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


def _strip_reasoning_block(text: str) -> str:
    import re

    cleaned = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


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


async def _call_deepseek(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    started_at = time.perf_counter()
    try:
        client = AsyncGroq(api_key=settings.groq_api_key)
        response = await client.chat.completions.create(
            model="deepseek-r1-distill-llama-70b",
            messages=_groq_messages(prompt, system, messages),
            max_tokens=4096,
            temperature=0.6,
        )
        usage = response.usage
        await _log_ai_run(
            agent,
            "deepseek",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            int((time.perf_counter() - started_at) * 1000),
            True,
        )
        content = response.choices[0].message.content or ""
        return _strip_reasoning_block(content)
    except Exception:
        await _log_ai_run(
            agent,
            "deepseek",
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


# ── New model call functions ────────────────────────────────────────────────

async def _call_anthropic_sonnet(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Claude Sonnet 4.6 — better than Haiku, cheaper than Opus."""
    started_at = time.perf_counter()
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system or "You are a helpful assistant.",
            messages=_anthropic_messages(prompt, messages),
        )
        text = "".join(getattr(block, "text", "") for block in response.content)
        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        await _log_ai_run(
            agent, "sonnet", input_tokens, output_tokens,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, "sonnet", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_anthropic_opus(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Claude Opus 4.7 — highest quality, use for critical tasks."""
    started_at = time.perf_counter()
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=system or "You are a helpful assistant.",
            messages=_anthropic_messages(prompt, messages),
        )
        text = "".join(getattr(block, "text", "") for block in response.content)
        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        await _log_ai_run(
            agent, "opus", input_tokens, output_tokens,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, "opus", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_gemini_pro(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Gemini 2.0 Flash (gemini-pro alias) — smarter than Flash 1.5."""
    started_at = time.perf_counter()
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        contents = _gemini_contents(prompt, system, messages)

        def _generate():
            return client.models.generate_content(
                model="gemini-2.0-flash",
                contents=contents,
            )

        response = await asyncio.to_thread(_generate)
        usage = getattr(response, "usage_metadata", None)
        await _log_ai_run(
            agent, "gemini-pro",
            getattr(usage, "prompt_token_count", 0) or 0,
            getattr(usage, "candidates_token_count", 0) or 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return getattr(response, "text", "") or ""
    except Exception:
        await _log_ai_run(
            agent, "gemini-pro", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_groq_llama4(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Llama 4 Scout via Groq."""
    started_at = time.perf_counter()
    try:
        client = AsyncGroq(api_key=settings.groq_api_key)
        response = await client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=_groq_messages(prompt, system, messages),
            max_tokens=2048,
            temperature=0.7,
        )
        usage = response.usage
        await _log_ai_run(
            agent, "llama4",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return response.choices[0].message.content or ""
    except Exception:
        await _log_ai_run(
            agent, "llama4", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_grok(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Grok 3 Mini via xAI API (OpenAI-compatible)."""
    from app.core.database import get_config
    api_key = settings.xai_api_key or await get_config("xai_api_key", "")
    if not api_key:
        raise RuntimeError("xAI API key not set")
    started_at = time.perf_counter()
    try:
        msgs = [{"role": "system", "content": system or ""}]
        if messages:
            msgs += [{"role": m["role"], "content": m["content"]}
                     for m in messages[-20:]]
        msgs.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": "grok-4.3", "messages": msgs, "max_tokens": 2048},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        await _log_ai_run(
            agent, "grok", 0, 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, "grok", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_deepseek_direct(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """DeepSeek V3 via direct API (cheaper than Groq relay)."""
    from app.core.database import get_config
    api_key = settings.deepseek_api_key or await get_config("deepseek_api_key", "")
    if not api_key:
        raise RuntimeError("DeepSeek API key not set")
    started_at = time.perf_counter()
    try:
        msgs = [{"role": "system", "content": system or ""}]
        if messages:
            msgs += [{"role": m["role"], "content": m["content"]}
                     for m in messages[-20:]]
        msgs.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": "deepseek-chat", "messages": msgs, "max_tokens": 2048},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        await _log_ai_run(
            agent, "deepseek-direct", 0, 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, "deepseek-direct", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_kimi(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
) -> str:
    """Kimi K2 via Moonshot API (OpenAI-compatible)."""
    from app.core.database import get_config
    api_key = settings.moonshot_api_key or await get_config("moonshot_api_key", "")
    if not api_key:
        raise RuntimeError("Moonshot API key not set")
    started_at = time.perf_counter()
    try:
        msgs = [{"role": "system", "content": system or ""}]
        if messages:
            msgs += [{"role": m["role"], "content": m["content"]}
                     for m in messages[-20:]]
        msgs.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.moonshot.cn/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": "kimi-k2-5", "messages": msgs, "max_tokens": 2048},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        await _log_ai_run(
            agent, "kimi", 0, 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, "kimi", 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


async def _call_openai(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    agent: str,
    model: str = "gpt-4o-mini",
) -> str:
    """GPT-4o / GPT-4o Mini via OpenAI API."""
    from app.core.database import get_config
    api_key = settings.openai_api_key or await get_config("openai_api_key", "")
    if not api_key:
        raise RuntimeError("OpenAI API key not set")
    started_at = time.perf_counter()
    try:
        msgs = [{"role": "system", "content": system or ""}]
        if messages:
            msgs += [{"role": m["role"], "content": m["content"]}
                     for m in messages[-20:]]
        msgs.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": model, "messages": msgs, "max_tokens": 2048},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
        await _log_ai_run(
            agent, model, 0, 0,
            int((time.perf_counter() - started_at) * 1000), True,
        )
        return text
    except Exception:
        await _log_ai_run(
            agent, model, 0, 0,
            int((time.perf_counter() - started_at) * 1000), False,
        )
        raise


# ── End new model call functions ────────────────────────────────────────────


async def _gemini_grounded_search(query: str) -> str:
    """Search the web using Gemini 2.5 Flash with Google Search Grounding."""
    if not settings.gemini_api_key:
        return "⚠️ ยังไม่ได้ตั้งค่า GEMINI_API_KEY"
    try:
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=(
                "ตอบเป็นภาษาไทยเท่านั้น "
                "สรุปผลการค้นหาเป็นรายการสั้นๆ ไม่เกิน 5 รายการ "
                "แต่ละรายการมีแค่: ชื่อ, ที่อยู่สั้นๆ, และลิงก์ (ถ้ามี) "
                "ห้ามอธิบายยาว ห้ามใช้ markdown เช่น ** หรือ ``` "
                "ตอบกระชับ อ่านง่าย เหมือนแนะนำเพื่อน"
            ),
        )
        formatted_query = f"{query} (สรุปสั้นๆ ไม่เกิน 5 รายการ)"

        _GROUNDING_MODELS = [
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-flash",
            "gemini-2.5-pro-preview-05-06",
        ]

        response = None
        last_exc = None
        for model_name in _GROUNDING_MODELS:
            try:
                def _generate(m=model_name):
                    return client.models.generate_content(
                        model=m,
                        contents=formatted_query,
                        config=config,
                    )

                response = await asyncio.to_thread(_generate)
                break
            except Exception as exc:
                last_exc = exc
                continue

        if response is None:
            raise last_exc or RuntimeError("No grounding model available")

        text = getattr(response, "text", "") or ""

        sources = []
        for candidate in (getattr(response, "candidates", []) or []):
            grounding = getattr(candidate, "grounding_metadata", None)
            if not grounding:
                continue
            for chunk in (getattr(grounding, "grounding_chunks", []) or []):
                web = getattr(chunk, "web", None)
                if web:
                    uri = getattr(web, "uri", "").strip()
                    title = getattr(web, "title", "").strip()
                    if uri:
                        sources.append(f"• {title or uri}\n  🔗 {uri}")

        import re as _re

        text = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        text = _re.sub(r"\*(.+?)\*", r"\1", text)
        text = _re.sub(r"```[\s\S]*?```", "", text)
        text = _re.sub(r"`(.+?)`", r"\1", text)
        text = _re.sub(r"#{1,6}\s", "", text)

        sources = sources[:5]
        if sources:
            text += "\n\n📚 แหล่งที่มา:\n" + "\n".join(sources)

        MAX_CHARS = 3500
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rsplit("\n", 1)[0] + "\n\n..."

        return text.strip() or "ไม่พบผลลัพธ์"
    except Exception as exc:
        return f"⚠️ ค้นหาไม่สำเร็จ: {exc}"


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


async def _call_anthropic_with_tools(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    tools: list[dict],
    agent: str,
) -> dict:
    started_at = time.perf_counter()
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        anthropic_tools = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in tools
        ]
        response = await client.messages.create(
            model=_PRIMARY_MODEL,
            max_tokens=2048,
            system=system,
            tools=anthropic_tools,
            messages=_anthropic_messages(prompt, messages),
        )

        text_parts = []
        tool_calls = []
        for block in response.content:
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text_parts.append(getattr(block, "text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}) or {},
                    }
                )

        input_tokens = getattr(response.usage, "input_tokens", 0)
        output_tokens = getattr(response.usage, "output_tokens", 0)
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(agent, "haiku", input_tokens, output_tokens, elapsed_ms, True)
        return {
            "text": "".join(text_parts).strip(),
            "tool_calls": tool_calls,
        }
    except Exception:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(agent, "haiku", 0, 0, elapsed_ms, False)
        raise


async def _call_groq_with_tools(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    tools: list[dict],
    agent: str,
) -> dict:
    started_at = time.perf_counter()
    try:
        client = AsyncGroq(api_key=settings.groq_api_key)
        groq_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in tools
        ]
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=_groq_messages(prompt, system, messages),
            tools=groq_tools,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        text = msg.content or ""
        tool_calls = []
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                raw_arguments = getattr(tool_call.function, "arguments", "") or "{}"
                try:
                    parsed_input = json.loads(raw_arguments)
                except Exception:
                    parsed_input = {}
                tool_calls.append(
                    {
                        "name": getattr(tool_call.function, "name", ""),
                        "input": parsed_input,
                    }
                )

        usage = response.usage
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(
            agent,
            "groq",
            getattr(usage, "prompt_tokens", 0) or 0,
            getattr(usage, "completion_tokens", 0) or 0,
            elapsed_ms,
            True,
        )
        return {"text": text.strip(), "tool_calls": tool_calls}
    except Exception:
        elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        await _log_ai_run(agent, "groq", 0, 0, elapsed_ms, False)
        raise


async def stream_chat_response(
    message: str,
    history: list[dict],
    system_prompt: str,
    model: str = "auto",
    agent: str = "MainChatAgent",
) -> AsyncGenerator[str, None]:
    """Stream response tokens from the preferred model with provider fallbacks."""
    availability = get_model_availability()
    active_model = (await get_active_model()) or _default_model(availability)
    normalized_model = str(model or "auto").strip().lower()
    alias_map = {"claude": "haiku", "ollama": "qwen3b"}
    requested_model = alias_map.get(normalized_model, normalized_model)

    if requested_model == "auto":
        candidates = [active_model] if active_model in _VALID_MODELS else []
        for candidate in ["haiku", "groq", "gemini", "qwen3b", "qwen7b"]:
            if candidate not in candidates:
                candidates.append(candidate)
    elif requested_model in _VALID_MODELS:
        candidates = [requested_model]
        for candidate in ["haiku", "groq", "gemini", "qwen3b", "qwen7b"]:
            if candidate != requested_model and candidate not in candidates:
                candidates.append(candidate)
    else:
        candidates = ["haiku", "groq", "gemini", "qwen3b", "qwen7b"]

    for candidate in candidates:
        if candidate in {"haiku", "groq", "gemini"} and not availability.get(candidate, False):
            continue

        started_at = time.perf_counter()
        emitted = False
        try:
            if candidate == "haiku":
                client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
                stream = client.messages.stream(
                    model=_PRIMARY_MODEL,
                    max_tokens=2048,
                    system=system_prompt,
                    messages=_anthropic_messages(message, history[-20:]),
                )
                async with stream as event_stream:
                    async for text in event_stream.text_stream:
                        if text:
                            emitted = True
                            yield text
                await _log_ai_run(agent, "haiku", 0, 0, int((time.perf_counter() - started_at) * 1000), True)
                return

            if candidate == "groq":
                client = AsyncGroq(api_key=settings.groq_api_key)
                stream = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=_groq_messages(message, system_prompt, history[-20:]),
                    max_tokens=2048,
                    stream=True,
                )
                async for chunk in stream:
                    delta = getattr(chunk.choices[0].delta, "content", None) if getattr(chunk, "choices", None) else None
                    if delta:
                        emitted = True
                        yield delta
                await _log_ai_run(agent, "groq", 0, 0, int((time.perf_counter() - started_at) * 1000), True)
                return

            if candidate == "gemini":
                client = genai.Client(api_key=settings.gemini_api_key)
                queue: asyncio.Queue[str | None] = asyncio.Queue()
                loop = asyncio.get_running_loop()
                error_holder: list[Exception] = []

                def _run_gemini_stream() -> None:
                    try:
                        for chunk in client.models.generate_content_stream(
                            model="gemini-1.5-flash",
                            contents=_gemini_contents(message, system_prompt, history[-20:]),
                        ):
                            text = getattr(chunk, "text", "") or ""
                            if text:
                                asyncio.run_coroutine_threadsafe(queue.put(text), loop).result()
                    except Exception as exc:
                        error_holder.append(exc)
                    finally:
                        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

                threading.Thread(target=_run_gemini_stream, daemon=True).start()
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    emitted = True
                    yield chunk
                if error_holder:
                    raise error_holder[0]
                await _log_ai_run(agent, "gemini", 0, 0, int((time.perf_counter() - started_at) * 1000), True)
                return

            if candidate in {"qwen3b", "qwen7b"}:
                text = await _call_ollama(message, system_prompt, history[-20:], agent, candidate)
                if text:
                    emitted = True
                    yield text
                return
        except Exception:
            if candidate in {"haiku", "groq", "gemini"}:
                await _log_ai_run(agent, candidate, 0, 0, int((time.perf_counter() - started_at) * 1000), False)
            continue

        if emitted:
            return

    result = await chat(
        message,
        system=system_prompt,
        agent=agent,
        messages=history[-20:],
        preferred_model=None if requested_model == "auto" else requested_model,
    )
    if result:
        yield result


async def chat(
    prompt: str,
    system: str = BASE_SYSTEM_PROMPT,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
    preferred_model: str | None = None,
    strict_model: bool = False,
) -> str:
    availability = await get_model_availability_async()
    if preferred_model == "deepseek":
        if settings.groq_api_key:
            return await _call_deepseek(prompt, system, messages, agent)
        if strict_model:
            raise RuntimeError("model unavailable: deepseek")
    active_model = (await get_active_model()) or _default_model(availability)
    requested_model = (
        preferred_model
        if preferred_model in _VALID_MODELS
        else _resolve_requested_model(agent, active_model)
    )
    if strict_model and requested_model in _VALID_MODELS:
        candidates = [requested_model]
    else:
        candidates = _model_candidates(requested_model)
        if active_model in _VALID_MODELS and active_model in candidates and preferred_model not in _VALID_MODELS:
            candidates.remove(active_model)
            candidates.insert(0, active_model)

    for candidate in candidates:
        if not availability.get(candidate, candidate in {"qwen3b", "qwen7b"}):
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
            if candidate == "sonnet":
                return await _call_anthropic_sonnet(prompt, system, messages, agent)
            if candidate == "opus":
                return await _call_anthropic_opus(prompt, system, messages, agent)
            if candidate == "gemini-pro":
                return await _call_gemini_pro(prompt, system, messages, agent)
            if candidate == "llama4":
                return await _call_groq_llama4(prompt, system, messages, agent)
            if candidate == "grok":
                return await _call_grok(prompt, system, messages, agent)
            if candidate == "deepseek-direct":
                return await _call_deepseek_direct(prompt, system, messages, agent)
            if candidate == "kimi":
                return await _call_kimi(prompt, system, messages, agent)
            if candidate == "gpt-4o-mini":
                return await _call_openai(prompt, system, messages, agent, "gpt-4o-mini")
            if candidate == "gpt-4o":
                return await _call_openai(prompt, system, messages, agent, "gpt-4o")
        except Exception:
            continue

    if strict_model and requested_model in _VALID_MODELS:
        raise RuntimeError(f"model unavailable: {requested_model}")

    default_candidate = _default_model(availability)
    if default_candidate == "haiku":
        return await _call_anthropic(prompt, system, messages, agent)
    if default_candidate == "groq":
        return await _call_groq(prompt, system, messages, agent)
    if default_candidate == "gemini":
        return await _call_gemini(prompt, system, messages, agent)
    return await _call_ollama(prompt, system, messages, agent, default_candidate)


async def chat_with_tools(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
    tools: list[dict],
    agent: str = "chat",
    preferred_model: str | None = None,
) -> dict:
    """
    เรียก AI พร้อม tool definitions
    Return: {"text": str, "tool_calls": list}
    """
    availability = get_model_availability()
    if preferred_model == "deepseek":
        text = await chat(
            prompt,
            system=system,
            agent=agent,
            messages=messages,
            preferred_model="deepseek",
        )
        return {"text": text, "tool_calls": []}

    active = (await get_active_model()) or _default_model(availability)
    if preferred_model == "haiku" and availability.get("haiku"):
        return await _call_anthropic_with_tools(prompt, system, messages, tools, agent)
    if preferred_model == "groq" and availability.get("groq"):
        return await _call_groq_with_tools(prompt, system, messages, tools, agent)

    if active == "haiku" and availability.get("haiku"):
        return await _call_anthropic_with_tools(prompt, system, messages, tools, agent)
    if active == "groq" and availability.get("groq"):
        return await _call_groq_with_tools(prompt, system, messages, tools, agent)

    text = await chat(
        prompt,
        system=system,
        agent=agent,
        messages=messages,
        preferred_model=preferred_model,
    )
    return {"text": text, "tool_calls": []}


async def chat_json(
    prompt: str,
    system: str = BASE_SYSTEM_PROMPT,
    agent: str = "general",
    messages: list[dict[str, str]] | None = None,
    preferred_model: str | None = None,
    strict_model: bool = False,
) -> dict:
    full_system = system + "\n\nตอบเป็น JSON เท่านั้น ไม่มีข้อความอื่น"
    raw = await chat(
        prompt,
        system=full_system,
        agent=agent,
        messages=messages,
        preferred_model=preferred_model,
        strict_model=strict_model,
    )
    return json.loads(_extract_json_payload(raw))
