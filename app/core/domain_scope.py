"""
Domain scope: hospital work vs personal business vs Ener-AI ops vs personal life.
Used for NL routing context, hospital snapshots, and work_query replies.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from app.core.database import get_db
from app.core.diagnostics import is_work_update_message

DOMAIN_HOSPITAL_WORK = "hospital_work"
DOMAIN_PERSONAL_BUSINESS = "personal_business"
DOMAIN_ENER_AI_SYSTEM = "ener_ai_system"
DOMAIN_PERSONAL_LIFE = "personal_life"
DOMAIN_UNKNOWN = "unknown"

_DOMAIN_CTX_PREFIX = "enerai_domain_ctx"

_HOSPITAL_KW = (
    "งานโรงบาล",
    "rutnin",
    "list today",
    "project 1",
    "project 2",
    "cloud contact center",
    "cloud pbx",
    "backup solution",
    "storage",
    "host vm resource",
    "improvement new network",
    "dicom",
    "จัดซื้อ",
    "cem",
    "yip",
    "purchase order",
    " po ",
)

_PERSONAL_BUSINESS_SIGNALS = (
    "งานส่วนตัว",
    "พระ",
    "ถ่ายรูปพระ",
    "ขายพระ",
    "tiktok",
    "youtube",
    "facebook",
    "caption",
    "content",
    "ener scan",
    "amulet",
)

_ENER_AI_SIGNALS = (
    "งานระบบ",
    "ener-ai",
    "ener ai",
    "enerai",
    "memorykeeper",
    "memorycurator",
    "memory curator",
    "/status",
    "/diag",
    "github",
    "code",
    " webhook",
    "webhook",
    "docker",
    " deploy",
    "commit",
    " otp",
    "cpu",
    " ram",
    " logs",
    "logs ",
    " bot ",
    "บอท",
)

_PERSONAL_LIFE_SIGNALS = (
    "เหนื่อย",
    "เครียด",
    "ง่วง",
    "ป่วย",
    "รู้สึก",
    "ชีวิต",
)

_WORK_QUERY_TRIGGERS = (
    "ตอนนี้มีงานอะไรบ้าง",
    "วันนี้มีงานอะไร",
    "งานค้างมีอะไร",
    "สรุปงานล่าสุด",
    "งานโรงบาลล่าสุดมีอะไร",
    "มีงานอะไรบ้าง",
    "มีงานอะไร",
    "งานอะไรบ้าง",
    "งานส่วนตัวมีอะไร",
    "งานระบบมีอะไร",
    "งานระบบมีอะไรค้าง",
)

_LAST_DOMAIN_TTL_SEC = 86400.0


def _tl(text: str) -> str:
    return (text or "").lower()


def _score_keywords(tl: str, raw: str, keywords: tuple[str, ...]) -> int:
    n = 0
    for kw in keywords:
        k = kw.lower().strip()
        if not k:
            continue
        if any("\u0e00" <= c <= "\u0e7f" for c in kw):
            if kw in raw:
                n += 1
        elif k in tl:
            n += 1
    return n


_DOMAIN_TIE_ORDER = (
    DOMAIN_HOSPITAL_WORK,
    DOMAIN_ENER_AI_SYSTEM,
    DOMAIN_PERSONAL_BUSINESS,
    DOMAIN_PERSONAL_LIFE,
)


def detect_domain_scope(text: str, chat_context: dict[str, Any] | None = None) -> str:
    """
    Coarse domain for the message. chat_context reserved for future (e.g. pinned domain).
    Returns: hospital_work | personal_business | ener_ai_system | personal_life | unknown
    """
    if chat_context:
        pinned = chat_context.get("pinned_domain")
        if pinned in (
            DOMAIN_HOSPITAL_WORK,
            DOMAIN_PERSONAL_BUSINESS,
            DOMAIN_ENER_AI_SYSTEM,
            DOMAIN_PERSONAL_LIFE,
        ):
            return str(pinned)
    raw = (text or "").strip()
    if not raw:
        return DOMAIN_UNKNOWN
    tl = _tl(raw)

    scores: dict[str, int] = {
        DOMAIN_HOSPITAL_WORK: _score_keywords(tl, raw, _HOSPITAL_KW),
        DOMAIN_PERSONAL_BUSINESS: _score_keywords(tl, raw, _PERSONAL_BUSINESS_SIGNALS),
        DOMAIN_ENER_AI_SYSTEM: _score_keywords(tl, raw, _ENER_AI_SIGNALS),
        DOMAIN_PERSONAL_LIFE: _score_keywords(tl, raw, _PERSONAL_LIFE_SIGNALS),
    }
    if is_work_update_message(raw):
        scores[DOMAIN_HOSPITAL_WORK] += 8
    if re.search(r"\bproject\s+\d+", tl):
        scores[DOMAIN_HOSPITAL_WORK] += 2
    if re.search(r"\b(?:tor|boq)\b", tl) and ("pbx" in tl or "cloud" in tl or "vendor" in tl):
        scores[DOMAIN_HOSPITAL_WORK] += 2
    if re.search(r"\b(?:otp|github|docker|webhook|cpu|ram)\b", tl):
        scores[DOMAIN_ENER_AI_SYSTEM] += 1

    mx = max(scores.values())
    if mx <= 0:
        return DOMAIN_UNKNOWN
    cands = [d for d, v in scores.items() if v == mx]
    for d in _DOMAIN_TIE_ORDER:
        if d in cands:
            return d
    return cands[0]


def is_work_query_message(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or is_work_update_message(raw):
        return False
    tl = _tl(raw)
    if any(t in tl for t in ("ทำไม otp", "otp ส่ง", "otp วน", "เช็ค otp", "รหัส otp")):
        return False
    for trig in _WORK_QUERY_TRIGGERS:
        if trig.lower() in tl:
            return True
    return False


def _infer_work_query_domain(text: str) -> str | None:
    """Explicit domain hint inside a work_query message, or None."""
    tl = _tl(text)
    if "โรงบาล" in text or "งานโรงบาล" in text:
        return DOMAIN_HOSPITAL_WORK
    if "งานส่วนตัว" in text or "ธุรกิจส่วนตัว" in text:
        return DOMAIN_PERSONAL_BUSINESS
    if "งานระบบ" in text or "ener" in tl or "ener-ai" in tl:
        return DOMAIN_ENER_AI_SYSTEM
    return None


def parse_hospital_work_snapshot(text: str) -> dict[str, Any]:
    """Light structure from a hospital standup paste (no LLM)."""
    raw = (text or "").strip()
    preview: list[str] = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s:
            continue
        l2 = s.lower()
        if any(
            k in l2
            for k in (
                "project",
                "current status",
                "% complete",
                "list today",
                "migration",
                "backup",
                "host vm",
                "สิ่งที่ต้องทำ",
            )
        ) or any(k in s for k in ("งานโรงบาล", "หัวข้อที่ต้องการสื่อสาร")):
            preview.append(s[:400])
        if len(preview) >= 24:
            break
    return {
        "saved_at": time.time(),
        "preview_lines": preview,
        "excerpt": raw[:2000],
    }


def _ctx_key(chat_id: str) -> str:
    return f"{_DOMAIN_CTX_PREFIX}:{chat_id}"


async def _load_ctx(chat_id: str) -> dict[str, Any]:
    key = _ctx_key(chat_id)
    async with get_db() as db:
        cur = await db.execute("SELECT value FROM memories WHERE key = ?", (key,))
        row = await cur.fetchone()
    if not row or not row["value"]:
        return {}
    try:
        return json.loads(str(row["value"]))
    except Exception:
        return {}


async def _save_ctx(chat_id: str, ctx: dict[str, Any]) -> None:
    key = _ctx_key(chat_id)
    payload = json.dumps(ctx, ensure_ascii=False)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO memories (key, value, tag)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                tag = excluded.tag,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, payload, "domain_scope"),
        )
        await db.commit()


async def persist_hospital_work_snapshot_for_chat(chat_id: str, text: str) -> str:
    """
    After a work_update message: store snapshot + last_active_domain.
    Returns a short Thai footer for the ack (source + not permanent DB).
    """
    snap = parse_hospital_work_snapshot(text)
    ctx = await _load_ctx(chat_id)
    ctx["hospital_snapshot"] = snap
    ctx["last_domain"] = DOMAIN_HOSPITAL_WORK
    ctx["last_ts"] = time.time()
    await _save_ctx(chat_id, ctx)
    return (
        "📎 **Snapshot ช่วงสั้น:** เก็บหัวข้อจากข้อความนี้ไว้ถามต่อในแชทนี้ (ชั่วคราว) — "
        "**ยังไม่ apply DB ถาวร** · **แหล่งอ้างอิงต่อไป:** snapshot งานโรงบาลล่าสุดในแชทนี้"
    )


async def _open_tasks_count() -> int:
    async with get_db() as db:
        cur = await db.execute("SELECT COUNT(*) AS c FROM tasks WHERE status = 'open'")
        row = await cur.fetchone()
    try:
        return int(row["c"]) if row else 0
    except Exception:
        return 0


async def format_work_query_reply_thai(text: str, chat_id: str) -> str:
    """
    Deterministic reply for work_query; always names the source (snapshot vs task DB vs none).
    """
    raw = (text or "").strip()
    explicit = _infer_work_query_domain(raw)
    ctx = await _load_ctx(chat_id)
    last_dom = str(ctx.get("last_domain") or "")
    try:
        last_ts = float(ctx.get("last_ts") or 0.0)
    except Exception:
        last_ts = 0.0
    now = time.time()
    use_ctx = (now - last_ts) <= _LAST_DOMAIN_TTL_SEC and last_dom in (
        DOMAIN_HOSPITAL_WORK,
        DOMAIN_PERSONAL_BUSINESS,
        DOMAIN_ENER_AI_SYSTEM,
    )
    domain = explicit or (last_dom if use_ctx else None)

    if domain is None:
        return (
            "หมายถึง **งานโรงบาล** งาน**ส่วนตัว** หรืองาน**ระบบ Ener-AI** ครับ?\n"
            "(ถ้าเพิ่งส่งสรุปโรงบาลมา ลองถามว่า “งานโรงบาลล่าสุดมีอะไร” ได้ครับ)"
        )

    lines: list[str] = []
    if domain == DOMAIN_HOSPITAL_WORK:
        snap = ctx.get("hospital_snapshot") or {}
        prev = snap.get("preview_lines") or []
        lines.append("**แหล่งอ้างอิง:** snapshot งานโรงบาลล่าสุด (ในแชท — ไม่ใช่ task DB ทั้งหมด)")
        if prev:
            lines.append("**จาก snapshot:**")
            for p in prev[:12]:
                lines.append(f"- {p}")
        else:
            lines.append("_ยังไม่มี snapshot โรงบาลในแชทนี้ — ส่งสรุปงานโรงบาลก่อนแล้วถามซ้ำได้ครับ_")
        n = await _open_tasks_count()
        lines.append(f"**Task DB (อ้างอิงเสริม):** งานเปิดในระบบ **{n}** รายการ (สรุปจากตาราง tasks เท่านั้น)")
        return "\n".join(lines)

    if domain == DOMAIN_PERSONAL_BUSINESS:
        lines.append("**แหล่งอ้างอิง:** ยังไม่มี snapshot งานส่วนตัวในระบบ — ตอบจากคำถามทั่วไปเท่านั้น")
        lines.append(
            "ถ้าต้องการให้จดจำรายการงานส่วนตัว ลองส่งสรุปมาเป็นข้อ ๆ แล้วถามซ้ำภายหลังได้ครับ"
        )
        return "\n".join(lines)

    if domain == DOMAIN_ENER_AI_SYSTEM:
        n = await _open_tasks_count()
        lines.append(
            "**แหล่งอ้างอิง:** ยังไม่มี snapshot “งานระบบค้าง” แยกจาก diagnostic — "
            "ใช้คำถามเชิงเทคนิค (logs / errors / status) หรือคำสั่ง /diag ได้ครับ"
        )
        lines.append(f"**Task DB:** งานเปิด **{n}** รายการ (ไม่รวม incident runtime)")
        return "\n".join(lines)

    lines.append("**แหล่งอ้างอิง:** ไม่ทราบ domain — ไม่ผสม snapshot กับงานอื่นโดยไม่แจ้ง")
    return "\n".join(lines)
