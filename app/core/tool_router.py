"""
Natural-language routing to monitor / system tools (real psutil/docker data).
Must not steal messages meant for communication follow-up or ChatOps diagnostics.
"""
from __future__ import annotations

import re

from app.core.diagnostics import classify_diagnostic_intent, is_communication_followup_intent


def classify_system_tool_intent(text: str) -> str | None:
    """
    Returns: "server" | "status" | "logs" | "errors" | None
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if is_communication_followup_intent(raw):
        return None
    if classify_diagnostic_intent(raw):
        return None

    t = raw.lower()
    th = raw

    # --- errors (highest priority: user suspects failure) ---
    err_patterns = [
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
    if any(p in th or p.lower() in t for p in err_patterns):
        return "errors"
    if re.search(r"\berrors?\b", t) and any(x in t for x in ("เช็ค", "ดู", "มี", "ล่าสุด", "latest", "any")):
        return "errors"

    # --- logs ---
    log_patterns = [
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
    if any(p.lower() in t or p in th for p in log_patterns):
        if any(x in t for x in ("memorykeeper", "memory curator", "agent ", "ai_runs")):
            return None
        return "logs"
    if re.search(r"\blogs?\b", t) and any(x in t for x in ("ดู", "เช็ค", "show", "tail", "ล่าสุด")):
        if "agent" in t or "memory" in t:
            return None
        return "logs"

    # --- server resources (CPU / RAM / disk / machine) ---
    server_patterns = [
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
    if any(p.lower() in t or p in th for p in server_patterns):
        return "server"
    if re.search(r"\b(cpu|ram|disk)\b", t) and any(x in t for x in ("เช็ค", "ดู", "check", "show", "เท่าไร", "เท่าไหร่")):
        return "server"
    if re.search(r"\b(cpu|ram|disk)\b", t) and any(x in th for x in ("ให้หน่อย", "หน่อย", "ที", "ค่า")):
        return "server"

    # --- overall status (no resource keyword) ---
    status_patterns = [
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
    if any(p.lower() in t or p in th for p in status_patterns):
        return "status"
    if "ener ai" in t and any(x in t for x in ("เป็นไง", "ยังไง", "โอเค", "ok", "how")):
        return "status"
    if "ระบบ" in th and any(x in th for x in ("เป็นไง", "ยังไง", "โอเคไหม", "ดีไหม", "ปกติไหม")):
        if "ไม่ตอบ" not in th:
            return "status"

    return None
