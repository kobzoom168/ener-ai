"""Trim chat payloads to stay within provider limits (e.g. Groq TPM)."""
from __future__ import annotations

_GROQ_MAX_SYSTEM_CHARS = 6_000
_GROQ_MAX_HISTORY_MESSAGES = 6
_GROQ_MAX_MESSAGE_CHARS = 1_200

_LOCAL_MAX_SYSTEM_CHARS = 14_000
_LOCAL_MAX_HISTORY_MESSAGES = 18
_LOCAL_MAX_MESSAGE_CHARS = 2_000

_TRUNCATION_NOTE = "\n\n[... ตัด context ให้สั้นลงเพื่อไม่เกิน limit ของ model ...]"


def trim_chat_context(
    system: str,
    history: list[dict] | None,
    *,
    profile: str = "default",
) -> tuple[str, list[dict]]:
    """Return (system, history) trimmed for the given profile."""
    if profile == "groq":
        max_system = _GROQ_MAX_SYSTEM_CHARS
        max_hist = _GROQ_MAX_HISTORY_MESSAGES
        max_msg = _GROQ_MAX_MESSAGE_CHARS
    elif profile in {"qwen3b", "qwen7b", "local"}:
        max_system = _LOCAL_MAX_SYSTEM_CHARS
        max_hist = _LOCAL_MAX_HISTORY_MESSAGES
        max_msg = _LOCAL_MAX_MESSAGE_CHARS
    else:
        max_system = 10_000
        max_hist = 12
        max_msg = 1_500

    raw_system = str(system or "")
    if len(raw_system) > max_system:
        system_out = raw_system[:max_system].rstrip() + _TRUNCATION_NOTE
    else:
        system_out = raw_system

    hist_out: list[dict] = []
    for message in (history or [])[-max_hist:]:
        role = message.get("role", "user")
        if role not in {"user", "assistant"}:
            continue
        content = str(message.get("content", "") or "")
        if len(content) > max_msg:
            content = content[: max_msg - 20].rstrip() + "…"
        hist_out.append({"role": role, "content": content})
    return system_out, hist_out


def profile_for_model(model: str) -> str:
    key = str(model or "").strip().lower()
    if key == "groq":
        return "groq"
    if key in {"qwen3b", "qwen7b"}:
        return key
    if key in {
        "dolphin",
        "deepseek-v4",
        "gemini-flash-lite",
        "gemini-3-flash",
        "mimo",
        "hy3",
        "llama-free",
    }:
        return "groq"
    return "default"
