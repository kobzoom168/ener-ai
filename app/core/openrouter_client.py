"""OpenRouter API client (OpenAI-compatible)."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from app.core.config import settings

OPENROUTER_MODELS: dict[str, str] = {
    # NOTE: Dolphin endpoints are not available for this account.
    # Keep key name for UI compatibility, route to a currently-available strong model.
    "dolphin": "deepseek/deepseek-v4-flash",
    "deepseek-v4": "deepseek/deepseek-v4-flash",
    "gemini-flash-lite": "google/gemini-3.1-flash-lite",
    "gemini-3-flash": "google/gemini-3.5-flash",
    "mimo": "xiaomi/mimo-v2.5",
    "hy3": "tencent/hy3-preview",
    "llama-free": "moonshotai/kimi-k2.6:free",
}

OPENROUTER_KEYS = frozenset(OPENROUTER_MODELS.keys())

OPENROUTER_LABELS: dict[str, str] = {
    "dolphin": "Dolphin (No Filter*)",
    "deepseek-v4": "DeepSeek V4 Flash",
    "gemini-flash-lite": "Gemini 2.5 Flash Lite",
    "gemini-3-flash": "Gemini 3 Flash",
    "mimo": "MiMo-V2.5 (Xiaomi)",
    "hy3": "Hunyuan HY3",
    "llama-free": "LLaMA 3.1 (Free)",
}
_MODELS_CACHE_TTL_SEC = 600
_models_cache: tuple[float, list[tuple[str, str]]] = (0.0, [])


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


def _openrouter_model_candidates(model_key: str) -> list[str]:
    key = str(model_key or "").strip().lower()
    primary = resolve_openrouter_model_id(key)
    if key == "dolphin":
        candidates = [
            primary,
            "qwen/qwen3.6-35b-a3b",
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.6:free",
        ]
        out: list[str] = []
        for model in candidates:
            if model not in out:
                out.append(model)
        return out
    return [primary]


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
    first = choices[0] or {}
    provider_error = first.get("error") or {}
    if provider_error:
        code = provider_error.get("code")
        msg = provider_error.get("message")
        raise RuntimeError(f"provider_error(code={code}): {msg}")
    message = first.get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"OpenRouter empty content: {data!s:.400}")
    return str(content).strip()


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    response = exc.response
    status = response.status_code if response is not None else "?"
    if response is None:
        return f"HTTP {status}"
    body = ""
    try:
        body = response.text.strip()
    except Exception:
        body = ""
    if not body:
        return f"HTTP {status}"
    return f"HTTP {status}: {body[:500]}"


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

    url = f"{openrouter_base_url()}/chat/completions"
    model_candidates = _openrouter_model_candidates(model_key)
    base_payload = {
        "messages": _build_messages(prompt, system, messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    started_at = time.perf_counter()
    last_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            for model_id in model_candidates:
                for attempt in (1, 2):
                    payload = {"model": model_id, **base_payload}
                    try:
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
                    except httpx.HTTPStatusError as exc:
                        detail = _http_error_detail(exc)
                        last_error = f"{model_id} -> {detail}"
                        is_transient = exc.response is not None and exc.response.status_code in {429, 500, 502, 503, 504}
                        if attempt == 1 and is_transient:
                            await asyncio.sleep(0.5)
                            continue
                        break
                    except RuntimeError as exc:
                        detail = str(exc)
                        last_error = f"{model_id} -> {detail}"
                        transient_hint = "network connection lost" in detail.lower() or "provider_error(code=502)" in detail.lower()
                        if attempt == 1 and transient_hint:
                            await asyncio.sleep(0.5)
                            continue
                        break
        await _log_ai_run(
            agent,
            model_key,
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            False,
        )
        raise RuntimeError(
            f"OpenRouter failed after retries: {last_error or 'unknown error'}"
        )
    except httpx.HTTPStatusError as exc:
        await _log_ai_run(
            agent,
            model_key,
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            False,
        )
        detail = _http_error_detail(exc)
        if exc.response is not None and exc.response.status_code == 404:
            raise RuntimeError(f"OpenRouter model not found. {detail}") from exc
        raise RuntimeError(f"OpenRouter request failed: {detail}") from exc
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


def _pretty_label(model_id: str) -> str:
    text = str(model_id or "").strip()
    if not text:
        return "unknown"
    if text in OPENROUTER_LABELS:
        return OPENROUTER_LABELS[text]
    if "/" in text:
        return text.split("/", 1)[1]
    return text


async def list_openrouter_models(force_refresh: bool = False) -> list[tuple[str, str]]:
    """Return [(model_id, label)] from OpenRouter /models with cache."""
    global _models_cache
    now = time.time()
    cached_at, cached_models = _models_cache
    if not force_refresh and cached_models and now - cached_at < _MODELS_CACHE_TTL_SEC:
        return cached_models

    api_key = await get_openrouter_api_key()
    if not api_key:
        fallback = [(k, OPENROUTER_LABELS.get(k, k)) for k in OPENROUTER_KEYS]
        _models_cache = (now, fallback)
        return fallback

    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{openrouter_base_url()}/models", headers=headers)
            response.raise_for_status()
            rows = response.json().get("data", [])
        options: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            model_id = str((row or {}).get("id", "")).strip()
            if not model_id or model_id.startswith("~"):
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            options.append((model_id, _pretty_label(model_id)))
        if not options:
            options = [(k, OPENROUTER_LABELS.get(k, k)) for k in OPENROUTER_KEYS]
        _models_cache = (now, options)
        return options
    except Exception:
        fallback = [(k, OPENROUTER_LABELS.get(k, k)) for k in OPENROUTER_KEYS]
        _models_cache = (now, fallback)
        return fallback
