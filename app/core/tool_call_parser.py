"""Parse tool calls from model text (e.g. Groq/LLaMA XML) when native API returns none."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_FUNCTION_CALLS_RE = re.compile(
    r"<function_calls>\s*(.*?)\s*</function_calls>",
    re.DOTALL | re.IGNORECASE,
)


def parse_xml_tool_calls(text: str) -> list[dict]:
    """Parse Groq-style XML tool calls from response text."""
    if not text or "<invoke" not in text.lower():
        return []

    tool_calls: list[dict] = []
    blocks = _FUNCTION_CALLS_RE.findall(text)
    if not blocks and "<invoke" in text.lower():
        blocks = [text]

    for block in blocks:
        wrapped = f"<root>{block}</root>"
        try:
            root = ET.fromstring(wrapped)
        except ET.ParseError:
            tool_calls.extend(_parse_invokes_regex(block))
            continue

        for invoke in root.findall(".//invoke"):
            name = (invoke.get("name") or "").strip()
            if not name:
                continue
            params: dict[str, str] = {}
            for param in invoke.findall("parameter"):
                key = (param.get("name") or "").strip()
                if key:
                    params[key] = (param.text or "").strip()
            tool_calls.append({"name": name, "input": params})

    return tool_calls


def _parse_invokes_regex(block: str) -> list[dict]:
    """Regex fallback when ElementTree cannot parse the block."""
    out: list[dict] = []
    for invoke_match in re.finditer(
        r'<invoke\s+name=["\']([^"\']+)["\']\s*>(.*?)</invoke>',
        block,
        re.DOTALL | re.IGNORECASE,
    ):
        name = invoke_match.group(1).strip()
        body = invoke_match.group(2)
        params: dict[str, str] = {}
        for param_match in re.finditer(
            r'<parameter\s+name=["\']([^"\']+)["\']\s*>(.*?)</parameter>',
            body,
            re.DOTALL | re.IGNORECASE,
        ):
            params[param_match.group(1).strip()] = param_match.group(2).strip()
        if name:
            out.append({"name": name, "input": params})
    return out


def strip_xml_tool_calls(text: str) -> str:
    """Remove XML tool-call markup from visible assistant text."""
    cleaned = _FUNCTION_CALLS_RE.sub("", text or "")
    cleaned = re.sub(
        r"<invoke\b[^>]*>.*?</invoke>",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return cleaned.strip()


def extract_tool_calls(text: str, native: list[dict] | None = None) -> tuple[list[dict], str]:
    """
    Prefer native API tool_calls; fall back to XML in text.
    Returns (calls, visible_text_without_xml).
    """
    calls = list(native or [])
    visible = strip_xml_tool_calls(text or "")
    if not calls:
        calls = parse_xml_tool_calls(text or "")
    elif visible != (text or "").strip():
        pass
    else:
        visible = strip_xml_tool_calls(text or "")
    return calls, visible
