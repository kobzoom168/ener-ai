"""Ollama / Qwen local HTTP client."""
from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

QWEN_MODEL_MAP: dict[str, str] = {
    "qwen3b": "qwen2.5:3b",
    "qwen7b": "qwen2.5:7b",
    "qwen 3b": "qwen2.5:3b",
    "qwen 7b": "qwen2.5:7b",
    "qwen-3b": "qwen2.5:3b",
    "qwen-7b": "qwen2.5:7b",
}


def format_ollama_error(exc: BaseException, *, tail: int = 400) -> str:
    """Human-readable error when str(exc) is empty."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        try:
            body = exc.response.text.strip()
            if body:
                return f"HTTP {exc.response.status_code}: {body[:tail]}"
        except Exception:
            pass
        return f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}"

    text = str(exc).strip()
    if text:
        return text[:tail]

    rep = repr(exc).strip()
    if rep and rep not in ("''", '""'):
        return rep[:tail]

    return traceback.format_exc()[-tail:].strip() or type(exc).__name__


def ollama_base_url() -> str:
    """Prefer process env (docker/.env) then pydantic settings."""
    env_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
    url = env_url or str(settings.ollama_base_url or "").strip() or "http://127.0.0.1:11434"
    return url.rstrip("/")


def resolve_ollama_model(model_key: str | None = None) -> str:
    key = str(model_key or "").strip().lower()
    if key in QWEN_MODEL_MAP:
        return QWEN_MODEL_MAP[key]
    if key and ":" in key:
        return key
    env_model = os.environ.get("OLLAMA_MODEL", "").strip()
    fallback = env_model or str(settings.ollama_model or "").strip() or "qwen2.5:3b"
    return QWEN_MODEL_MAP.get(fallback.lower(), fallback)


def parse_chat_response(data: Any) -> str:
    if not isinstance(data, dict):
        raise RuntimeError(f"Ollama invalid response type: {type(data).__name__}")

    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    message = data.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if content is not None and str(content).strip():
            return str(content).strip()

    legacy = data.get("response")
    if legacy is not None and str(legacy).strip():
        return str(legacy).strip()

    snippet = json.dumps(data, ensure_ascii=False)[:400]
    raise RuntimeError(f"Ollama returned empty content: {snippet}")


async def ollama_chat(
    *,
    messages: list[dict[str, str]],
    model_key: str,
    timeout: float = 120.0,
) -> str:
    base = ollama_base_url()
    model_name = resolve_ollama_model(model_key)
    url = f"{base}/api/chat"
    payload = {
        "model": model_name,
        "messages": messages,
        "stream": False,
    }
    logger.info(
        "[OLLAMA] POST %s model=%s (key=%s) messages=%d",
        url,
        model_name,
        model_key,
        len(messages),
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return parse_chat_response(response.json())
    except Exception as exc:
        detail = format_ollama_error(exc)
        logger.warning("[OLLAMA] failed: %s", detail)
        raise RuntimeError(detail) from exc


async def ollama_health_check() -> tuple[bool, str]:
    """Returns (ok, status_message)."""
    base = ollama_base_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base}/api/tags")
            response.raise_for_status()
            data = response.json()
            models = [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]
            names = ", ".join(models[:5]) or "no models"
            return True, f"OK ({base}) — {names}"
    except Exception as exc:
        return False, format_ollama_error(exc)
