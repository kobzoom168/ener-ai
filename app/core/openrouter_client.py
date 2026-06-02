"""OpenRouter API client (OpenAI-compatible)."""
from __future__ import annotations

import os
from typing import Any

import httpx

from app.core.config import settings

OPENROUTER_MODELS: dict[str, str] = {
    "dolphin": "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
    "deepseek-v4": "deepseek/deepseek-v4-flash",
    "gemini-flash-lite": "google/gemini-2.5-flash-lite",
    "gemini-3-flash": "google/gemini-3-flash-preview",
    "mimo": "xiaomi/mimo-v2.5",
    "hy3": "tencent/hy3-preview",
    "llama-free": "meta-llama/llama-3.3-70b-instruct:free",
}

OPENROUTER_KEYS = frozenset(OPENROUTER_MODELS.keys())

OPENROUTER_LABELS: dict[str, str] = {
    "dolphin": "Dolphin (No Filter)",
    "deepseek-v4": "DeepSeek V4 Flash",
    "gemini-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-3-flash": "Gemini 3 Flash",
    "mimo": "MiMo-V2.5 (Xiaomi)",
    "hy3": "Hunyuan HY3",
    "llama-free": "LLaMA 3.1 (Free)",
}


def openrouter_base_url() -> str:
    env_url = os.environ.get("OPENROUTER_BASE_URL", "").strip()
    return (
        env_url
        or str(getattr(settings, "openrouter_base_url", "") or "").strip()
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")


async def get_openrouter_api_key() -> str:
    from app.core.database import get_config

    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        key = str(getattr(settings, "openrouter_api_key", "") or "").strip()
    if not key:
        key = str(await get_config("openrouter_api_key", "") or "").strip()
    return key


def is_openrouter_model(model_key: str) -> bool:
    return str(model_key or "").strip().lower() in OPENROUTER_KEYS


def resolve_openrouter_model_id(model_key: str) -> str:
    key = str(model_key or "").strip().lower()
    if key in OPENROUTER_MODELS:
        return OPENROUTER_MODELS[key]
    return key


def _build_messages(
    prompt: str,
    system: str,
    messages: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system.strip():
        msgs.append({"role": "system", "content": system})
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                content = str(message.get("content", "") or "").strip()
                if content:
                    msgs.append({"role": role, "content": content})
    msgs.append({"role": "user", "content": prompt})
    return msgs


def _openrouter_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://my-ener.uk",
        "X-Title": "Ener-AI",
    }


def _parse_completion(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter empty choices: {data!s:.400}")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"OpenRouter empty content: {data!s:.400}")
    return str(content).strip()


async def call_openrouter(
    model_key: str,
    prompt: str,
    system: str = "",
    messages: list[dict[str, str]] | None = None,
    *,
    agent: str = "OpenRouter",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    from app.core.ai import _log_ai_run
    import time

    api_key = await get_openrouter_api_key()
    if not api_key:
        raise RuntimeError(
            "OpenRouter API key not set — add OPENROUTER_API_KEY to .env on server"
        )

    model_id = resolve_openrouter_model_id(model_key)
    url = f"{openrouter_base_url()}/chat/completions"
    payload = {
        "model": model_id,
        "messages": _build_messages(prompt, system, messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                url,
                headers=_openrouter_headers(api_key),
                json=payload,
            )
            response.raise_for_status()
            text = _parse_completion(response.json())
        await _log_ai_run(
            agent,
            model_key,
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            True,
        )
        return text
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


async def openrouter_chat_completions(
    model_key: str,
    chat_messages: list[dict],
    *,
    tools: list[dict] | None = None,
    max_tokens: int = 4096,
) -> dict:
    """Raw OpenAI-compatible completion for tool loops."""
    api_key = await get_openrouter_api_key()
    if not api_key:
        raise RuntimeError("OpenRouter API key not set")

    payload: dict[str, Any] = {
        "model": resolve_openrouter_model_id(model_key),
        "messages": chat_messages,
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{openrouter_base_url()}/chat/completions",
            headers=_openrouter_headers(api_key),
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def openrouter_available() -> bool:
    return bool(await get_openrouter_api_key())
