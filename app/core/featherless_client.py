"""Featherless.ai OpenAI-compatible API client (uncensored models)."""
from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings

FEATHERLESS_MODELS: dict[str, str] = {
    "featherless-abliterated": "huihui-ai/Qwen2.5-72B-Instruct-abliterated",
}

FEATHERLESS_KEYS = frozenset(FEATHERLESS_MODELS.keys())

FEATHERLESS_LABELS: dict[str, str] = {
    "featherless-abliterated": "Qwen2.5 72B Abliterated (No Filter)",
}

FEATHERLESS_BASE_URL_DEFAULT = "https://api.featherless.ai/v1"


def featherless_base_url() -> str:
    env_url = os.environ.get("FEATHERLESS_BASE_URL", "").strip()
    return (
        env_url
        or str(getattr(settings, "featherless_base_url", "") or "").strip()
        or FEATHERLESS_BASE_URL_DEFAULT
    ).rstrip("/")


async def get_featherless_api_key() -> str:
    from app.core.database import get_config

    key = os.environ.get("FEATHERLESS_API_KEY", "").strip()
    if not key:
        key = str(getattr(settings, "featherless_api_key", "") or "").strip()
    if not key:
        key = str(await get_config("featherless_api_key", "") or "").strip()
    return key


def is_featherless_model(model_key: str) -> bool:
    return str(model_key or "").strip().lower() in FEATHERLESS_KEYS


def resolve_featherless_model_id(model_key: str) -> str:
    key = str(model_key or "").strip().lower()
    if key in FEATHERLESS_MODELS:
        return FEATHERLESS_MODELS[key]
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


def _featherless_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _parse_completion(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Featherless empty choices: {data!s:.400}")
    first = choices[0] or {}
    message = first.get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"Featherless empty content: {data!s:.400}")
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


def _usage_counts(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    return prompt_tokens, completion_tokens


def _apply_usage_from_chunk(
    chunk: dict[str, Any],
    prompt_tokens: int,
    completion_tokens: int,
) -> tuple[int, int]:
    usage = chunk.get("usage") or {}
    if not usage:
        return prompt_tokens, completion_tokens
    if usage.get("prompt_tokens") is not None:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
    if usage.get("completion_tokens") is not None:
        completion_tokens = int(usage.get("completion_tokens") or 0)
    return prompt_tokens, completion_tokens


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


async def featherless_available() -> bool:
    return bool(await get_featherless_api_key())


async def featherless_model_label(model_id: str) -> str:
    key = str(model_id or "").strip().lower()
    if key in FEATHERLESS_LABELS:
        return FEATHERLESS_LABELS[key]
    if key in FEATHERLESS_MODELS.values():
        for alias, mid in FEATHERLESS_MODELS.items():
            if mid == key:
                return FEATHERLESS_LABELS.get(alias, key)
    return FEATHERLESS_LABELS.get(key, key or "Featherless")


async def call_featherless(
    model_key: str,
    prompt: str,
    system: str = "",
    messages: list[dict[str, str]] | None = None,
    *,
    agent: str = "Featherless",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> str:
    from app.core.ai import _log_ai_run

    api_key = await get_featherless_api_key()
    if not api_key:
        raise RuntimeError(
            "Featherless API key not set — add FEATHERLESS_API_KEY to .env on server"
        )

    url = f"{featherless_base_url()}/chat/completions"
    model_id = resolve_featherless_model_id(model_key)
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
                headers=_featherless_headers(api_key),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = _parse_completion(data)
            prompt_tokens, completion_tokens = _usage_counts(data)
        await _log_ai_run(
            agent,
            model_key,
            prompt_tokens,
            completion_tokens,
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
            raise RuntimeError(f"Featherless model not found. {detail}") from exc
        raise RuntimeError(f"Featherless request failed: {detail}") from exc
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


async def stream_featherless(
    model_key: str,
    prompt: str,
    system: str = "",
    messages: list[dict[str, str]] | None = None,
    *,
    agent: str = "Featherless",
    max_tokens: int = 2048,
    temperature: float = 0.7,
) -> AsyncIterator[str]:
    """Stream completion tokens from Featherless.ai (SSE)."""
    from app.core.ai import _log_ai_run

    api_key = await get_featherless_api_key()
    if not api_key:
        raise RuntimeError(
            "Featherless API key not set — add FEATHERLESS_API_KEY to .env on server"
        )

    url = f"{featherless_base_url()}/chat/completions"
    model_id = resolve_featherless_model_id(model_key)
    payload = {
        "model": model_id,
        "messages": _build_messages(prompt, system, messages),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }

    started_at = time.perf_counter()
    prompt_tokens = 0
    completion_tokens = 0
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                url,
                headers=_featherless_headers(api_key),
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
                    prompt_tokens, completion_tokens = _apply_usage_from_chunk(
                        chunk, prompt_tokens, completion_tokens
                    )
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
            prompt_tokens,
            completion_tokens,
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
        raise RuntimeError(
            f"Featherless request failed: {_http_error_detail(exc)}"
        ) from exc
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
