"""Universal tool-calling loops for OpenAI-compatible APIs, Gemini, and text/XML fallbacks."""
from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.tool_call_parser import extract_tool_calls, strip_xml_tool_calls
from app.core.tools import execute_tool

CompleteFn = Callable[[list[dict], list[dict]], Awaitable[dict[str, Any]]]

_TOOL_SYSTEM_SUFFIX = (
    "\n\n=== Tool use ===\n"
    "เรียก tools ผ่าน function calling API เท่านั้น "
    "ห้ามพิมพ์ <function_calls> หรือ XML ในข้อความตอบ user"
)


def tool_messages_start(
    system: str,
    messages: list[dict] | None,
    prompt: str,
) -> list[dict]:
    payload: list[dict] = [
        {"role": "system", "content": (system or "") + _TOOL_SYSTEM_SUFFIX},
    ]
    if messages:
        for message in messages:
            role = message.get("role", "user")
            if role in {"user", "assistant"}:
                payload.append({"role": role, "content": message.get("content", "")})
    payload.append({"role": "user", "content": prompt})
    return payload


def parse_openai_chat_response(data: dict) -> tuple[str, list[dict], dict]:
    """Parse OpenAI-compatible chat.completion JSON."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = str(msg.get("content") or "")
    tool_calls: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args)
        except Exception:
            parsed = {}
        tool_calls.append(
            {
                "name": str(fn.get("name") or ""),
                "input": parsed,
                "tool_call_id": tc.get("id"),
            }
        )
    usage = data.get("usage") or {}
    return content, tool_calls, usage


def serialize_openai_tool_calls(tool_calls: list[dict]) -> list[dict]:
    out = []
    for index, tool_call in enumerate(tool_calls):
        fn = tool_call.get("function") or {}
        out.append(
            {
                "id": tool_call.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": fn.get("arguments", "{}"),
                },
            }
        )
    return out


def _call_input(call: dict) -> dict:
    return call.get("input") or call.get("parameters") or {}


async def execute_tool_calls(calls: list[dict]) -> list[str]:
    lines: list[str] = []
    for call in calls:
        name = str(call.get("name", "")).strip()
        if not name:
            continue
        try:
            result = await execute_tool(name, _call_input(call))
            lines.append(f"✅ {name}: {str(result)[:2000]}")
        except Exception as exc:
            lines.append(f"⚠️ {name}: {exc}")
    return lines


async def openai_tool_loop(
    *,
    complete: CompleteFn,
    agent: str,
    log_model: str,
    system: str,
    messages: list[dict] | None,
    prompt: str,
    tools: list[dict],
    max_turns: int = 5,
    log_fn: Callable[..., Awaitable[None]] | None = None,
) -> dict:
    """
    Multi-turn tool loop for any OpenAI-compatible chat.completions API.
    Handles native tool_calls JSON and XML-in-text fallback.
    """
    from app.core.ai import _convert_tools_for_openai

    if not tools:
        return {"text": "", "tool_calls": [], "model": log_model}

    openai_tools = _convert_tools_for_openai(tools)
    current_messages = tool_messages_start(system, messages, prompt)
    final_text = ""
    total_input = 0
    total_output = 0
    started_at = time.perf_counter()

    for turn in range(max_turns):
        data = await complete(current_messages, openai_tools)
        usage = data.get("usage") or {}
        total_input += int(usage.get("prompt_tokens") or 0)
        total_output += int(usage.get("completion_tokens") or 0)

        raw_text, native_calls, _ = parse_openai_chat_response(data)
        parsed_calls, visible_text = extract_tool_calls(raw_text, native_calls)
        if not parsed_calls:
            final_text = visible_text
            break

        assistant_entry: dict = {
            "role": "assistant",
            "content": visible_text or None,
        }
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        if msg.get("tool_calls"):
            assistant_entry["tool_calls"] = msg["tool_calls"]
        else:
            assistant_entry["tool_calls"] = [
                {
                    "id": f"call_{turn}_{index}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(
                            _call_input(call),
                            ensure_ascii=False,
                        ),
                    },
                }
                for index, call in enumerate(parsed_calls)
                if call.get("name")
            ]
        current_messages.append(assistant_entry)

        for index, call in enumerate(parsed_calls):
            name = str(call.get("name", "")).strip()
            if not name:
                continue
            try:
                result = await execute_tool(name, _call_input(call))
                result_text = str(result)[:4000]
            except Exception as exc:
                result_text = f"Error: {exc}"
            tool_call_id = call.get("tool_call_id") or f"call_{turn}_{index}"
            current_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result_text,
                }
            )

    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    if log_fn:
        await log_fn(agent, log_model, total_input, total_output, elapsed_ms, True)

    return {
        "text": final_text.strip() or "ยังไม่มีคำตอบตอนนี้",
        "tool_calls": [],
        "model": log_model,
    }


async def run_tools_from_text(
    reply: str,
    *,
    prompt: str,
    system: str,
    messages: list[dict] | None,
    summarize: Callable[..., Awaitable[str]] | None = None,
) -> str:
    """Execute XML/text tool calls and optionally summarize with a follow-up model call."""
    calls, visible = extract_tool_calls(reply or "", None)
    if not calls:
        return strip_xml_tool_calls(reply or "")

    tool_lines = await execute_tool_calls(calls)
    if summarize and tool_lines:
        try:
            summary = await summarize(
                f"คำถาม: {prompt}\n\nผลจาก tools:\n"
                + "\n".join(tool_lines)
                + "\n\nสรุปคำตอบให้กบเป็นภาษาไทย อ้างอิงผล tool จริงเท่านั้น",
                system=system,
                messages=messages,
            )
            cleaned = strip_xml_tool_calls(str(summary or "").strip())
            if cleaned:
                return cleaned
        except Exception:
            pass

    base = strip_xml_tool_calls(visible)
    joined = "\n".join(tool_lines)
    if base and joined:
        return f"{base}\n\n{joined}"
    return base or joined
