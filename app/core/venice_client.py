"""Venice.ai OpenAI-compatible API client (uncensored models)."""
from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings

VENICE_MODELS: dict[str, str] = {
    "venice-70b": "llama-3.3-70b",
    "venice-abliterated": "llama-3.1-70b-abliterated",
}

VENICE_KEYS = frozenset(VENICE_MODELS.keys())

VENICE_LABELS: dict[str, str] = {
    "venice-70b": "LLaMA 3.3 70B (Venice)",
    "venice-abliterated": "LLaMA 3.1 70B Abliterated (No Filter)",
}

VENICE_BASE_URL_DEFAULT = "https://api.venice.ai/api/v1"


def venice_base_url() -> str:
    env_url = os.environ.get("VENICE_BASE_URL", "").strip()
    return (
        env_url
        or str(getattr(settings, "venice_base_url", "") or "").strip()
        or VENICE_BASE_URL_DEFAULT
    ).rstrip("/")


async def get_venice_api_key() -> str:
    from app.core.database import get_config

    key = os.environ.get("VENICE_API_KEY", "").strip()
    if not key:
        key = str(getattr(settings, "venice_api_key", "") or "").strip()
    if not key:
        key = str(await get_config("venice_api_key", "") or "").strip()
    return key


def is_venice_model(model_key: str) -> bool:
    return str(model_key or "").strip().lower() in VENICE_KEYS


def resolve_venice_model_id(model_key: str) -> str:
    key = str(model_key or "").strip().lower()
    if key in VENICE_MODELS:
        return VENICE_MODELS[key]
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


def _venice_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _parse_completion(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Venice empty choices: {data!s:.400}")
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"Venice empty content: {data!s:.400}")
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


def _stream_delta_text(delta: dict[str, Any]) -> str:
    content = (delta or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


async def venice_available() -> bool:
    return bool(await get_venice_api_key())


async def venice_model_label(model_id: str) -> str:
    key = str(model_id or "").strip().lower()
    if key in VENICE_LABELS:
        return VENICE_LABELS[key]
    if key in VENICE_MODELS.values():
        for alias, mid in VENICE_MODELS.items():
            if mid == key:
                return VENICE_LABELS.get(alias, key)
    return VENICE_LABELS.get(key, key or "Venice")


async def call_venice(
    model_key: str,
    prompt: str,
    system: str = "",
    messages: list[dict[str, str]] | None = None,
    *,
    agent: str = "Venice",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    from app.core.ai import _log_ai_run

    api_key = await get_venice_api_key()
    if not api_key:
        raise RuntimeError(
            "Venice API key not set — add VENICE_API_KEY to .env on server"
        )

    url = f"{venice_base_url()}/chat/completions"
    model_id = resolve_venice_model_id(model_key)
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
                headers=_venice_headers(api_key),
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
            raise RuntimeError(f"Venice model not found. {detail}") from exc
        raise RuntimeError(f"Venice request failed: {detail}") from exc
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


async def stream_venice(
    model_key: str,
    prompt: str,
    system: str = "",
    messages: list[dict[str, str]] | None = None,
    *,
    agent: str = "Venice",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """Stream completion tokens from Venice.ai (SSE)."""
    from app.core.ai import _log_ai_run

    api_key = await get_venice_api_key()
    if not api_key:
        raise RuntimeError(
            "Venice API key not set — add VENICE_API_KEY to .env on server"
        )

    url = f"{venice_base_url()}/chat/completions"
    model_id = resolve_venice_model_id(model_key)
    payload = {
        "model": model_id,
        "messages": _build_messages(prompt, system, messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    started_at = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                url,
                headers=_venice_headers(api_key),
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    first = choices[0] or {}
                    delta = first.get("delta") or {}
                    text = _stream_delta_text(delta)
                    if text:
                        yield text
        await _log_ai_run(
            agent,
            model_key,
            0,
            0,
            int((time.perf_counter() - started_at) * 1000),
            True,
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
        raise RuntimeError(f"Venice request failed: {_http_error_detail(exc)}") from exc
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
