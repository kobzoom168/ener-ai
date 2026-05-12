"""
Natural-language routing to monitor tools and multi-intent orchestration.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.core.diagnostics import list_stakeholder_no_response_phrases

_MAX_INTENTS = 4

_ERR_PATTERNS = [
    "มี error",
    "มี error ไหม",
    "เช็ค error",
    "เช็คerror",
    "error ล่าสุด",
    "errors ล่าสุด",
    "พังตรงไหน",
    "มี traceback",
    "traceback",
    "มีปัญหาไหม",
    "มีบั๊กไหม",
]

_LOG_PATTERNS = [
    "ดู log",
    "ดู logs",
    "เช็ค log",
    "เช็ค logs",
    "logs ล่าสุด",
    "log ล่าสุด",
    "ดู log ล่าสุด",
    "ดู logs ล่าสุด",
    "ล่าสุด log",
]

_SERVER_PATTERNS = [
    "เช็ค cpu",
    "ดู cpu",
    "cpu ตอนนี้",
    "cpu ของระบบ",
    "ดู ram",
    "เช็ค ram",
    "ดู disk",
    "เช็ค disk",
    "เช็ค server",
    "ดู server",
    "server เป็นไง",
    "เครื่องเป็นไง",
    "resource",
    "ทรัพยากร",
    "หน่วยความจำ",
    "disk usage",
    "memory usage",
]

_STATUS_PATTERNS = [
    "ระบบเป็นไง",
    "ระบบตอนนี้เป็นไง",
    "ระบบตอนนี้",
    "status ระบบ",
    "เช็คระบบ",
    "สถานะระบบ",
    "check system",
    "ener ai เป็นไง",
    "ener-ai เป็นไง",
    "ระบบ ener ai เป็นไง",
    "ระบบ ener-ai เป็นไง",
    "ระบบ ener ai",
    "สุขภาพระบบ",
]

_BOT_PATTERNS = [
    "bot ไม่ตอบ",
    "บอทไม่ตอบ",
    "telegram ไม่ตอบ",
    "webhook",
    "ระบบไม่ตอบ",
    "ener ไม่ตอบ",
]

_AGENT_PATTERNS = [
    "memorykeeper",
    "memory curator",
    "memorycurator",
    "agent ล้ม",
    "ระบบล่ม",
]

_OTP_KW = [
    "otp",
    "รหัส otp",
    "ส่ง otp",
    "otp วน",
    "otp loop",
    "otp ส่ง",
    "เช็ค otp",
    "เช็คotp",
    "ทำไม otp",
    "otp ซ้ำ",
    "otp มารัว",
    "otp ตลอด",
    "รหัส otp",
    "รหัสotp",
]


def _find(line: str, needle: str) -> int:
    return line.lower().find(needle.lower())


def _min_pos_for_patterns(line: str, patterns: Iterable[str]) -> int | None:
    best: int | None = None
    for p in patterns:
        pos = _find(line, p)
        if pos >= 0:
            best = pos if best is None else min(best, pos)
    return best


def _line_matches_diag_otp(line: str) -> bool:
    t = line.lower()
    th = line
    if any(k in t for k in _OTP_KW):
        return True
    if "otp" in t and any(x in t for x in ("ส่ง", "วน", "ทำไม", "เช็ค", "ซ้ำ", "loop", "รัว", "ตลอด", "รหัส")):
        return True
    if "รหัสotp" in t.replace(" ", ""):
        return True
    return "otp" in t and "รหัส" in th


def _pos_diag_otp(line: str) -> int | None:
    if not _line_matches_diag_otp(line):
        return None
    t = line.lower()
    positions: list[int] = []
    for k in _OTP_KW:
        pos = _find(line, k)
        if pos >= 0:
            positions.append(pos)
    if "otp" in t:
        positions.append(t.find("otp"))
    return min(positions) if positions else None


def _pos_diag_bot(line: str) -> int | None:
    return _min_pos_for_patterns(line, _BOT_PATTERNS)


def _pos_diag_agent(line: str) -> int | None:
    return _min_pos_for_patterns(line, _AGENT_PATTERNS)


def _pos_communication(line: str) -> int | None:
    best: int | None = None
    for p in list_stakeholder_no_response_phrases():
        pos = _find(line, p)
        if pos >= 0:
            best = pos if best is None else min(best, pos)
    return best


def _line_matches_system_logs(line: str) -> bool:
    t = line.lower()
    if any(_find(line, p) >= 0 for p in _LOG_PATTERNS):
        if any(x in t for x in ("memorykeeper", "memory curator", "agent ", "ai_runs")):
            return False
        return True
    if re.search(r"\blogs?\b", t) and any(x in t for x in ("ดู", "เช็ค", "show", "tail", "ล่าสุด")):
        if "agent" in t or "memory" in t:
            return False
        return True
    return False


def _pos_system_logs(line: str) -> int | None:
    if not _line_matches_system_logs(line):
        return None
    pos = _min_pos_for_patterns(line, _LOG_PATTERNS)
    if pos is not None:
        return pos
    m = re.search(r"\blogs?\b", line.lower())
    return m.start() if m else None


def _line_matches_system_errors(line: str) -> bool:
    t = line.lower()
    th = line
    if any(p in th or _find(line, p) >= 0 for p in _ERR_PATTERNS):
        return True
    return bool(re.search(r"\berrors?\b", t) and any(x in t for x in ("เช็ค", "ดู", "มี", "ล่าสุด", "latest", "any")))


def _pos_system_errors(line: str) -> int | None:
    if not _line_matches_system_errors(line):
        return None
    pos = _min_pos_for_patterns(line, _ERR_PATTERNS)
    if pos is not None:
        return pos
    m = re.search(r"\berrors?\b", line.lower())
    return m.start() if m else None


def _line_matches_system_server(line: str) -> bool:
    t = line.lower()
    th = line
    if any(_find(line, p) >= 0 for p in _SERVER_PATTERNS):
        return True
    if re.search(r"\b(cpu|ram|disk)\b", t) and any(x in t for x in ("เช็ค", "ดู", "check", "show", "เท่าไร", "เท่าไหร่")):
        return True
    if re.search(r"\b(cpu|ram|disk)\b", t) and any(x in th for x in ("ให้หน่อย", "หน่อย", "ที", "ค่า")):
        return True
    return False


def _pos_system_server(line: str) -> int | None:
    if not _line_matches_system_server(line):
        return None
    pos = _min_pos_for_patterns(line, _SERVER_PATTERNS)
    if pos is not None:
        return pos
    m = re.search(r"\b(cpu|ram|disk)\b", line.lower())
    return m.start() if m else None


def _line_matches_system_status(line: str) -> bool:
    t = line.lower()
    th = line
    if any(_find(line, p) >= 0 for p in _STATUS_PATTERNS):
        return True
    if "ener ai" in t and any(x in t for x in ("เป็นไง", "ยังไง", "โอเค", "ok", "how")):
        return True
    if "ระบบ" in th and any(x in th for x in ("เป็นไง", "ยังไง", "โอเคไหม", "ดีไหม", "ปกติไหม")):
        if "ไม่ตอบ" not in th:
            return True
    return False


def _pos_system_status(line: str) -> int | None:
    if not _line_matches_system_status(line):
        return None
    pos = _min_pos_for_patterns(line, _STATUS_PATTERNS)
    if pos is not None:
        return pos
    t = line.lower()
    if "ener ai" in t:
        return t.find("ener ai")
    if "ระบบ" in line:
        return line.find("ระบบ")
    return None


def _intents_ordered_for_line(line: str) -> list[str]:
    """Collect intents that apply to this line, ordered by first match position."""
    pairs: list[tuple[int, str]] = []

    def add(pos: int | None, intent: str) -> None:
        if pos is not None and pos >= 0:
            pairs.append((pos, intent))

    add(_pos_communication(line), "communication")
    add(_pos_diag_otp(line), "diag_otp")
    add(_pos_diag_bot(line), "diag_bot")
    add(_pos_diag_agent(line), "diag_agent")
    add(_pos_system_errors(line), "system_errors")
    add(_pos_system_logs(line), "system_logs")
    add(_pos_system_server(line), "system_server")
    add(_pos_system_status(line), "system_status")

    # Tie-break: stable sort then dedupe intent (keep earliest position)
    pairs.sort(key=lambda x: (x[0], x[1]))
    out: list[str] = []
    seen: set[str] = set()
    for _, intent in pairs:
        if intent in seen:
            continue
        seen.add(intent)
        out.append(intent)
    return out


def classify_message_intents(text: str) -> list[str]:
    """
    Ordered list of intents (max 4). Splits on newlines; each line contributes
    intents in order of appearance, merged with dedupe across lines.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        lines = [raw]

    ordered: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for intent in _intents_ordered_for_line(line):
            if intent in seen:
                continue
            seen.add(intent)
            ordered.append(intent)
            if len(ordered) >= _MAX_INTENTS:
                return ordered
    return ordered


def classify_system_tool_intent(text: str) -> str | None:
    """
    Back-compat: first system_* intent in appearance order, or None.
    """
    mapping = {
        "system_server": "server",
        "system_status": "status",
        "system_logs": "logs",
        "system_errors": "errors",
    }
    for intent in classify_message_intents(text):
        if intent in mapping:
            return mapping[intent]  # type: ignore[return-value]
    return None
