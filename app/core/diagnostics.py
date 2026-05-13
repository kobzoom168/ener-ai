"""
ChatOps-style diagnostics from real evidence (DB, logs, Telegram API).
Never exposes OTP codes. Does not claim to have run checks without results.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psutil

from app.core.config import settings
from app.core.database import get_db

# Memory keys (must match app/main.py)
_K_ADMIN_OTP_CODE = "admin_otp_code"
_K_ADMIN_OTP_EXPIRE = "admin_otp_expire"
_K_ADMIN_OTP_LAST_SENT = "admin_otp_last_sent"
_K_TERM_OTP_EXPIRE = "terminal_otp_expire"
_K_TERM_OTP_LAST_SENT = "terminal_otp_last_sent"

_LOG_DIR = Path("/var/log/ener-ai")
_REPO_ROOT = Path("/app")
_TELEGRAM_API = "https://api.telegram.org"

_log = logging.getLogger("ener-ai.diagnostics")


def sanitize_diagnostic_text(text: str | None) -> str:
    """Redact secrets and identifiers before logs/diagnostic output."""
    if text is None:
        return ""
    s = str(text)
    # Telegram bot token (numeric:id or bot<id>:secret)
    s = re.sub(r"\bbot\d{5,}:[A-Za-z0-9_-]{20,}\b", "bot[REDACTED]", s, flags=re.I)
    s = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{25,}\b", "[BOT_TOKEN]", s)
    # Bearer / Basic
    s = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._\-\+/=]{8,}", r"\1[REDACTED]", s)
    s = re.sub(r"(?i)(Basic\s+)[A-Za-z0-9+/=]{8,}", r"\1[REDACTED]", s)
    # Authorization header or assignment
    s = re.sub(r"(?i)(Authorization\s*[:=]\s*)([^\s\n\r;]{6,})", r"\1[REDACTED]", s)
    # api_key / password / secret / token style key=value
    s = re.sub(
        r'(?i)(["\']?(?:api[_-]?key|password|client_secret|secret|token)["\']?\s*[:=]\s*)'
        r'(["\']?)([^\s"\'\],}\]]{4,})\2',
        r"\1\2[REDACTED]\2",
        s,
    )
    s = re.sub(
        r"(?i)\b(api[_-]?key|password|secret|token)\s*=\s*([^\s&\]\}\"]{4,})",
        r"\1=[REDACTED]",
        s,
    )
    # 6-digit OTP near OTP / รหัส
    s = re.sub(
        r"((?:\b(?:otp)\b|OTP|รหัส(?:\s*OTP)?)[^\d]{0,30}?)(\d{6})\b",
        r"\1******",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"(\d{6})([^\d]{0,20}?(?:\b(?:otp)\b|OTP|รหัส))",
        r"******\2",
        s,
        flags=re.I,
    )
    # Long numeric ids (e.g. Telegram chat_id)
    s = re.sub(r"\b-?\d{12,}\b", "[CHAT_ID]", s)
    return s


DIAGNOSTIC_PROVENANCE_RULE_TH = (
    "\n\n**หลักฐาน / provenance:** รายงานนี้อิงเฉพาะข้อมูลที่ collector ดึงได้จริง — "
    "**ห้าม** แสดงคำสั่ง shell เป็นถึง `output:` ถ้าไม่ได้ execute จริง "
    "ถ้าไม่มีสิทธิ์หรือไม่พบ docker socket จะระบุ `docker_stats: no_access` ชัดเจน "
    "(ยังไม่ได้รัน command นี้ เพราะ collector ไม่มีสิทธิ์/ไม่พบ docker socket)"
)


def _diag_provenance_footer() -> str:
    return DIAGNOSTIC_PROVENANCE_RULE_TH


def _mask_ip(ip: str) -> str:
    if not ip:
        return ""
    if ip.count(".") == 3:
        parts = ip.split(".")
        parts[-1] = "xxx"
        return ".".join(parts)
    if ":" in ip:
        return (ip[:12] + "…") if len(ip) > 12 else ip[:6] + "…"
    return "masked"


def _parse_ts(created_at: str | None) -> datetime | None:
    if not created_at:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(created_at[:26], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None


async def log_otp_event(
    event_type: str,
    request: Any = None,
    reason: str = "",
    metadata: dict | None = None,
) -> None:
    """Persist OTP-related forensic event (never stores OTP code)."""
    path = method = referer = user_agent = ""
    client_ip = ""
    session_present = auth_present = 0
    session_valid: int | None = None
    if request is not None:
        try:
            path = str(getattr(request.url, "path", "") or "")
            method = str(getattr(request.method, "upper", lambda: request.method)() or request.method)
        except Exception:
            path = method = ""
        try:
            referer = request.headers.get("referer", "") or request.headers.get("Referer", "") or ""
            user_agent = request.headers.get("user-agent", "") or request.headers.get("User-Agent", "") or ""
        except Exception:
            pass
        try:
            if request.client and getattr(request.client, "host", None):
                client_ip = _mask_ip(str(request.client.host))
        except Exception:
            client_ip = ""
        try:
            session_present = 1 if request.cookies.get("admin_session") else 0
        except Exception:
            session_present = 0
        try:
            auth = request.headers.get("Authorization", "")
            auth_present = 1 if auth.startswith("Basic ") else 0
        except Exception:
            auth_present = 0
        # session_valid requires async session check — unknown => NULL (not 0 = "invalid")

    meta_json = json.dumps(metadata or {}, ensure_ascii=False)[:4000]
    try:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO otp_audit_logs
                   (event_type, path, method, client_ip, user_agent, referer,
                    session_present, session_valid, auth_header_present, reason, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_type,
                    path,
                    method,
                    client_ip,
                    user_agent[:500],
                    referer[:500],
                    session_present,
                    session_valid,
                    auth_present,
                    (reason or "")[:500],
                    meta_json,
                ),
            )
            await db.commit()
    except Exception as exc:
        _log.warning(
            "OTP_AUDIT_LOG_FAILED event_type=%s err=%s",
            event_type,
            exc,
            exc_info=False,
        )


async def collect_otp_state() -> dict[str, Any]:
    """Current OTP-related memory state — no OTP code returned."""
    keys = [
        _K_ADMIN_OTP_CODE,
        _K_ADMIN_OTP_EXPIRE,
        _K_ADMIN_OTP_LAST_SENT,
        "terminal_otp_code",
        _K_TERM_OTP_EXPIRE,
        _K_TERM_OTP_LAST_SENT,
    ]
    async with get_db() as db:
        placeholders = ",".join("?" * len(keys))
        cur = await db.execute(
            f"SELECT key, value FROM memories WHERE key IN ({placeholders})",
            keys,
        )
        rows = await cur.fetchall()
    kv = {r["key"]: r["value"] for r in rows}
    now = time.time()
    has_code = bool((kv.get(_K_ADMIN_OTP_CODE) or "").strip())
    try:
        admin_exp = float(kv.get(_K_ADMIN_OTP_EXPIRE, "0") or 0)
    except (TypeError, ValueError):
        admin_exp = 0.0
    has_admin_otp = has_code and admin_exp > now
    admin_expires_in = max(0, int(admin_exp - now)) if has_admin_otp else 0
    try:
        last_admin = float(kv.get(_K_ADMIN_OTP_LAST_SENT, "0") or 0)
    except (TypeError, ValueError):
        last_admin = 0.0
    sec_since_admin = int(now - last_admin) if last_admin > 0 else None
    try:
        term_exp = float(kv.get(_K_TERM_OTP_EXPIRE, "0") or 0)
    except (TypeError, ValueError):
        term_exp = 0.0
    term_code = (kv.get("terminal_otp_code") or "").strip()
    has_terminal_otp = bool(term_code) and term_exp > now
    try:
        last_term = float(kv.get(_K_TERM_OTP_LAST_SENT, "0") or 0)
    except (TypeError, ValueError):
        last_term = 0.0
    sec_since_term = int(now - last_term) if last_term > 0 else None
    return {
        "has_admin_otp": has_admin_otp,
        "admin_otp_expires_in": admin_expires_in,
        "seconds_since_last_admin_otp_sent": sec_since_admin,
        "has_terminal_otp": has_terminal_otp,
        "seconds_since_last_terminal_otp_sent": sec_since_term,
    }


async def collect_otp_audit_events(hours: int = 6) -> list[dict[str, Any]]:
    h = max(1, min(int(hours), 168))
    rel = f"-{h} hours"
    try:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT id, created_at, event_type, path, method, client_ip, user_agent,
                       referer, session_present, session_valid, auth_header_present,
                       reason, metadata_json
                FROM otp_audit_logs
                WHERE datetime(created_at) >= datetime('now', ?)
                ORDER BY datetime(created_at) ASC
                LIMIT 500
                """,
                (rel,),
            )
            rows = await cur.fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            for k in ("user_agent", "referer", "reason", "metadata_json", "path", "method", "client_ip"):
                if d.get(k) is not None:
                    d[k] = sanitize_diagnostic_text(str(d[k]))
        return out
    except Exception:
        return []


async def log_diagnostic_audit(action: str, details: str = "") -> None:
    det = sanitize_diagnostic_text(str(details))[:2000]
    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                (action, det),
            )
            await db.commit()
    except Exception as exc:
        _log.warning("DIAG_AUDIT_LOG_FAILED action=%s err=%s", action, exc)


async def prune_otp_audit_logs(days: int = 90) -> int:
    d = max(7, min(int(days), 3650))
    try:
        async with get_db() as db:
            cur = await db.execute(
                "DELETE FROM otp_audit_logs WHERE datetime(created_at) < datetime('now', ?)",
                (f"-{d} days",),
            )
            await db.commit()
            n = cur.rowcount
            return int(n) if n is not None and n >= 0 else 0
    except Exception as exc:
        _log.warning("PRUNE_OTP_AUDIT_LOGS_FAILED err=%s", exc)
        return 0


async def collect_recent_app_logs(keywords: list[str], limit: int = 100) -> dict[str, Any]:
    if not _LOG_DIR.exists() or not _LOG_DIR.is_dir():
        return {"status": "no_log_access", "lines": []}
    log_files = sorted(
        [p for p in _LOG_DIR.iterdir() if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:3]
    if not log_files:
        return {"status": "no_log_access", "lines": []}
    kw_lower = [k.lower() for k in keywords]
    matched: list[str] = []
    try:
        for lf in log_files:
            try:
                text = lf.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(text[-5000:]):
                low = line.lower()
                if any(k in low for k in kw_lower):
                    matched.append(sanitize_diagnostic_text(line[:500]))
                if len(matched) >= limit:
                    break
            if len(matched) >= limit:
                break
        return {"status": "ok", "source_files": [str(p) for p in log_files], "lines": matched[:limit]}
    except Exception:
        return {"status": "no_log_access", "lines": []}


async def collect_recent_git_context() -> dict[str, Any]:
    if not (_REPO_ROOT / ".git").exists():
        return {"status": "no_git_access", "detail": "/app/.git not found"}
    try:
        log_r = await asyncio.to_thread(
            subprocess.run,
            ["git", "log", "--oneline", "-10"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        show_r = await asyncio.to_thread(
            subprocess.run,
            ["git", "show", "--stat", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {
            "status": "ok",
            "log_oneline": sanitize_diagnostic_text((log_r.stdout or log_r.stderr or "")[:2000]),
            "head_stat": sanitize_diagnostic_text((show_r.stdout or show_r.stderr or "")[:2000]),
        }
    except Exception as exc:
        return {"status": "no_git_access", "detail": sanitize_diagnostic_text(str(exc)[:200])}


def _session_valid_label(v: Any) -> str:
    if v == 1:
        return "valid"
    return "unknown"


def _best_precursor_event(events: list[dict[str, Any]], sent_idx: int) -> dict[str, Any] | None:
    """Within 60s before SENT, pick prior event with closest IP/UA/path match."""
    sent = events[sent_idx]
    t_sent = _parse_ts(str(sent.get("created_at") or ""))
    if not t_sent:
        if sent_idx > 0:
            return events[sent_idx - 1]
        return None
    best: tuple[float, float, dict[str, Any]] | None = None
    for j in range(sent_idx):
        e = events[j]
        t = _parse_ts(str(e.get("created_at") or ""))
        if not t:
            continue
        delta = (t_sent - t).total_seconds()
        if delta < 0 or delta > 60:
            continue
        score = 0.0
        pe, ps = (e.get("path") or ""), (sent.get("path") or "")
        if pe and ps and pe == ps:
            score += 3.0
        elif pe and ps and (pe in ps or ps in pe):
            score += 1.0
        ua_e = (e.get("user_agent") or "")[:80]
        ua_s = (sent.get("user_agent") or "")[:80]
        if ua_e and ua_s:
            if ua_e == ua_s:
                score += 2.0
            elif ua_e[:32] == ua_s[:32]:
                score += 1.0
        if (e.get("client_ip") or "") and (e.get("client_ip") or "") == (sent.get("client_ip") or ""):
            score += 2.0
        cand = (score, -delta, e)
        if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] > best[1]):
            best = cand
    if best is not None and best[0] > 0:
        return best[2]
    if sent_idx > 0:
        prev = events[sent_idx - 1]
        t_prev = _parse_ts(str(prev.get("created_at") or ""))
        if t_prev and 0 < (t_sent - t_prev).total_seconds() <= 60:
            return prev
    return None


def analyze_otp_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    sent_types = {"ADMIN_OTP_SENT", "ADMIN_OTP_SENT_MANUAL"}
    sent_rows = [e for e in events if e.get("event_type") in sent_types]
    times: list[datetime] = []
    for e in sent_rows:
        t = _parse_ts(str(e.get("created_at") or ""))
        if t:
            times.append(t)
    intervals: list[float] = []
    for i in range(1, len(times)):
        intervals.append((times[i] - times[i - 1]).total_seconds())
    in_band = [x for x in intervals if 280 <= x <= 330]
    repeated_5min = len(in_band) >= 2
    ua_counts: dict[str, int] = {}
    path_counts: dict[str, int] = {}
    for e in events:
        ua = (e.get("user_agent") or "")[:120]
        if ua:
            ua_counts[ua] = ua_counts.get(ua, 0) + 1
        p = e.get("path") or ""
        if p:
            path_counts[p] = path_counts.get(p, 0) + 1
    top_ua = sorted(ua_counts.items(), key=lambda x: -x[1])[:3]
    top_paths = sorted(path_counts.items(), key=lambda x: -x[1])[:5]
    pre_sent: list[str | None] = []
    for i, e in enumerate(events):
        if e.get("event_type") in sent_types:
            pre = _best_precursor_event(events, i)
            pre_sent.append(pre.get("event_type") if pre else None)
    pre_counter = Counter([x for x in pre_sent if x])
    return {
        "admin_otp_sent_count": len(sent_rows),
        "intervals_sec": intervals[:20],
        "repeated_5min_otp_loop": repeated_5min,
        "top_user_agents": top_ua,
        "top_paths": top_paths,
        "events_before_sent": dict(pre_counter.most_common(8)),
        "evidence_sufficient": len(events) >= 3,
    }


async def diagnose_otp_loop() -> dict[str, Any]:
    out: dict[str, Any] = {"what": "otp_loop", "evidence": {}, "errors": []}
    try:
        out["evidence"]["otp_state"] = await collect_otp_state()
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"otp_state:{exc}")[:300])
    try:
        out["evidence"]["otp_events"] = await collect_otp_audit_events(6)
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"otp_events:{exc}")[:300])
    try:
        out["evidence"]["app_logs"] = await collect_recent_app_logs(
            ["OTP", "ADMIN_OTP", "/admin/otp", "Session expired", "Redirect", "error"],
            80,
        )
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"app_logs:{exc}")[:300])
    try:
        out["evidence"]["git"] = await collect_recent_git_context()
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"git:{exc}")[:300])
    out["evidence"]["analysis"] = analyze_otp_events(out["evidence"].get("otp_events") or [])
    return out


async def diagnose_agent_health() -> dict[str, Any]:
    out: dict[str, Any] = {"what": "agent_health", "evidence": {}, "errors": []}
    try:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT agent_name, result, summary, substr(context,1,200) as ctx, created_at
                FROM agent_events
                WHERE lower(agent_name) LIKE '%memory%' OR lower(summary) LIKE '%memory%'
                ORDER BY datetime(created_at) DESC LIMIT 25
                """
            )
            out["evidence"]["memory_agent_events"] = []
            for r in await cur.fetchall():
                d = dict(r)
                for k in ("summary", "ctx", "agent_name", "result"):
                    if d.get(k) is not None:
                        d[k] = sanitize_diagnostic_text(str(d[k]))
                out["evidence"]["memory_agent_events"].append(d)
            cur = await db.execute(
                """
                SELECT agent, model, success, created_at
                FROM ai_runs WHERE success = 0
                ORDER BY datetime(created_at) DESC LIMIT 15
                """
            )
            out["evidence"]["failed_ai_runs"] = [dict(r) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT action, details, created_at FROM audit_logs ORDER BY datetime(created_at) DESC LIMIT 20"
            )
            audit_rows = []
            for r in await cur.fetchall():
                d = dict(r)
                if d.get("details"):
                    d["details"] = sanitize_diagnostic_text(str(d["details"]))
                if d.get("action"):
                    d["action"] = sanitize_diagnostic_text(str(d["action"]))
                audit_rows.append(d)
            out["evidence"]["audit_tail"] = audit_rows
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(str(exc))[:400])
    return out


async def diagnose_bot_unresponsive() -> dict[str, Any]:
    out: dict[str, Any] = {"what": "bot_unresponsive", "evidence": {}, "errors": []}
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        out["errors"].append("no_bot_token_config")
        return out
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{_TELEGRAM_API}/bot{token}/getWebhookInfo")
            out["evidence"]["webhook_http_status"] = r.status_code
            if r.status_code == 200:
                data = r.json()
                out["evidence"]["webhook_result"] = data.get("result") or {}
            else:
                out["evidence"]["webhook_body"] = sanitize_diagnostic_text(r.text[:500])
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"webhook:{exc}")[:300])
    try:
        from app.core.ai import get_model_availability_async
        out["evidence"]["models"] = await get_model_availability_async()
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"models:{exc}")[:300])
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) as c FROM messages")
            row = await cur.fetchone()
            out["evidence"]["messages_count"] = row["c"] if row else None
    except Exception as exc:
        out["errors"].append(sanitize_diagnostic_text(f"db:{exc}")[:300])
    return out


_STAKEHOLDER_NO_RESPONSE_PHRASES = (
    "ลูกค้าไม่ตอบ",
    "vendor ไม่ตอบ",
    "ผู้ขายไม่ตอบ",
    "ทีมไม่ตอบ",
    "user ไม่ตอบ",
    "คนไข้ไม่ตอบ",
    "เขาไม่ตอบ",
)

_DIAG_TECH_STRICT_RE = re.compile(
    r"\b(bot|system|server|webhook|telegram|api|error|log|otp)\b",
    re.I,
)


def has_diagnostic_tech_context_strict(text: str) -> bool:
    """True if user explicitly mentions stack/diagnostic topics (guard for stakeholder routing)."""
    raw = text or ""
    t = raw.lower()
    if _DIAG_TECH_STRICT_RE.search(t):
        return True
    if "บอท" in raw or "ระบบ" in raw:
        return True
    return False


_ENGINEERING_TOPIC_RE = re.compile(
    r"\b(ssh|server|webhooks?|telegram|bots?|system|apis?|errors?|logs?|otp|debug|diagnostics?|"
    r"repos?|repositories|github|gitlab|docker|deploy|sourcecode|source\s*code)\b",
    re.I,
)


def user_message_touches_engineering_topics(text: str) -> bool:
    """Broad detector for when the user is asking about stack / infra / code (Main Chat scope guard)."""
    raw = text or ""
    t = raw.lower()
    if _ENGINEERING_TOPIC_RE.search(t):
        return True
    if "บอท" in raw or "ระบบ" in raw or "โค้ด" in raw:
        return True
    return False


def matches_stakeholder_no_response_phrase(text: str) -> bool:
    th = text or ""
    t = th.lower()
    for p in _STAKEHOLDER_NO_RESPONSE_PHRASES:
        if p.lower() in t or p in th:
            return True
    return False


def list_stakeholder_no_response_phrases() -> tuple[str, ...]:
    """Phrases for multi-intent communication detection (substring match)."""
    return _STAKEHOLDER_NO_RESPONSE_PHRASES


def is_communication_followup_intent(text: str) -> bool:
    """Human/vendor/team silence — not Telegram bot / Ener-AI stack (unless user mixes tech terms)."""
    return matches_stakeholder_no_response_phrase(text) and not has_diagnostic_tech_context_strict(text)


def communication_followup_reply_thai(text: str) -> str:
    """Deterministic coaching reply; must stay free of DevOps/diagnostic vocabulary."""
    _ = text
    return (
        "รับทราบครับ น่าจะเป็นเรื่อง **การติดตามคน** มากกว่าเรื่องฝั่งโปรแกรม\n\n"
        "**แนวทางสั้น ๆ**\n"
        "• ส่งข้อความตามแบบสุภาพ ไม่กดดัน ระบุชื่อเรื่องกับสิ่งที่ต้องการชัด ๆ\n"
        "• เว้นระยะประมาณ 1 วันค่อยตามใหม่ (งานเร่งให้ใส่กำหนดเวลาชัด ๆ)\n"
        "• ครั้งถัดไปถามว่า “สะดวกอัปเดตช่วงไหน” เพื่อให้ตอบง่ายขึ้น\n\n"
        "**ตัวอย่างข้อความตาม (ปรับชื่อเรื่องได้)**\n"
        "สวัสดีครับ ขออนุญาตตามเรื่อง ___ อีกครั้งนะครับ\n"
        "ถ้าสะดวกช่วยตอบกลับเมื่อไหร่ก็ได้ครับ ขอบคุณมากครับ\n\n"
        "ถ้าบอกได้ว่าเป็นงานประเภทไหน (งบ / สัญญา / ส่งมอบ) จะช่วยปรับถ้อยคำให้เข้ากับบริบทได้ครับ"
    )


_RESOURCE_SUBSTRINGS_TH = (
    "ซีพียู",
    "ซีพิ",
    "แรม",
    "docker stats",
    "container หนัก",
    "เครื่องหนัก",
    "ใช้ resource",
    "ทรัพยากร",
)
_RESOURCE_WORD_RE = re.compile(
    r"\b(cpu|ram|mem|memory|resource)\b",
    re.I,
)


def matches_resource_diagnostic_intent(text: str) -> bool:
    """CPU/RAM/memory/resource questions (natural language)."""
    raw = text or ""
    t = raw.lower()
    if any(s in raw for s in _RESOURCE_SUBSTRINGS_TH):
        return True
    if _RESOURCE_WORD_RE.search(t):
        return True
    if "ram" in t and any(x in raw for x in ("ละ", "ไหน", "เท่า", "เท่าไร", "เท่าไหร่", "เท่าไร่")):
        return True
    return False


def resource_diagnostic_intent_position(text: str) -> int | None:
    """Earliest char index for resource diagnostic keywords, for NL routing order."""
    if not matches_resource_diagnostic_intent(text):
        return None
    raw = text or ""
    t = raw.lower()
    positions: list[int] = []
    for s in _RESOURCE_SUBSTRINGS_TH:
        i = raw.find(s)
        if i >= 0:
            positions.append(i)
    for m in _RESOURCE_WORD_RE.finditer(t):
        positions.append(m.start())
    if "ram" in t:
        ri = t.find("ram")
        if ri >= 0 and any(x in raw for x in ("ละ", "ไหน", "เท่า", "เท่าไร", "เท่าไหร่", "เท่าไร่")):
            positions.append(ri)
    return min(positions) if positions else 0


async def collect_resource_usage() -> dict[str, Any]:
    """Gather metrics from DB snapshot, live psutil, and optional docker stats (real exec only)."""
    out: dict[str, Any] = {
        "server_metrics": None,
        "psutil": None,
        "docker_stats": None,
        "docker_status": "skipped",
        "docker_reason": "",
        "errors": [],
    }

    try:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT cpu_percent, ram_percent, ram_used_mb, ram_total_mb, disk_percent,
                       net_in_bytes, net_out_bytes, recorded_at
                FROM server_metrics
                ORDER BY datetime(recorded_at) DESC, id DESC
                LIMIT 1
                """
            )
            row = await cur.fetchone()
            if row:
                out["server_metrics"] = dict(row)
    except Exception as exc:
        out["errors"].append(f"server_metrics:{exc}")

    try:
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        proc_cpu: float | None = None
        try:
            proc_cpu = float(psutil.Process().cpu_percent(interval=0.2))
        except Exception:
            proc_cpu = None
        out["psutil"] = {
            "cpu_percent": float(psutil.cpu_percent(interval=0.25)),
            "process_cpu_percent": proc_cpu,
            "ram_percent": float(vm.percent),
            "ram_used_mb": int(vm.used // (1024 * 1024)),
            "ram_total_mb": int(vm.total // (1024 * 1024)),
            "disk_percent": float(du.percent),
        }
    except Exception as exc:
        out["errors"].append(f"psutil:{exc}")

    container = (getattr(settings, "docker_stats_container", None) or "").strip()
    if not container:
        out["docker_status"] = "skipped"
        out["docker_reason"] = "docker_stats_container empty (disabled in config)"
        return out

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        out_txt = (stdout or b"").decode("utf-8", errors="replace").strip()
        err_txt = (stderr or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and out_txt:
            out["docker_stats"] = out_txt[:4000]
            out["docker_status"] = "ok"
        else:
            out["docker_status"] = "no_access"
            out["docker_reason"] = err_txt[:500] or f"exit_code={proc.returncode}"
    except FileNotFoundError:
        out["docker_status"] = "no_access"
        out["docker_reason"] = "docker CLI not found in PATH"
    except asyncio.TimeoutError:
        out["docker_status"] = "no_access"
        out["docker_reason"] = "docker stats timed out (5s)"
    except Exception as exc:
        out["docker_status"] = "no_access"
        out["docker_reason"] = str(exc)[:500]

    return out


async def diagnose_resource_usage(*, debug: bool = False) -> dict[str, Any]:
    """Structured resource diagnostic with provenance (no fabricated command output)."""
    collected = await collect_resource_usage()
    collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    errors = list(collected.get("errors") or [])

    sm = collected.get("server_metrics") or {}
    ps = collected.get("psutil") or {}

    def _pick(sm_key: str, ps_key: str | None = None) -> Any:
        k = ps_key or sm_key
        v = sm.get(sm_key)
        if v is not None:
            return v
        return ps.get(k)

    cpu = _pick("cpu_percent")
    ram_p = _pick("ram_percent")
    ram_u = _pick("ram_used_mb")
    ram_t = _pick("ram_total_mb")
    disk = _pick("disk_percent")
    proc_cpu = ps.get("process_cpu_percent")

    source_parts: list[str] = []
    if sm and any(
        sm.get(k) is not None for k in ("cpu_percent", "ram_percent", "ram_used_mb", "ram_total_mb", "disk_percent")
    ):
        source_parts.append("server_metrics")
    if ps:
        source_parts.append("psutil")
    docker_raw = collected.get("docker_stats") if collected.get("docker_status") == "ok" else None
    if collected.get("docker_status") == "ok" and docker_raw:
        source_parts.append("docker_stats")

    if cpu is None and ram_p is None and disk is None:
        source_used = "no_access"
    else:
        source_used = "+".join(dict.fromkeys(source_parts)) if source_parts else "no_access"

    out: dict[str, Any] = {
        "what": "resource_usage",
        "collected_at": collected_at,
        "source_used": source_used,
        "cpu_percent": cpu,
        "process_cpu_percent": proc_cpu,
        "ram_percent": ram_p,
        "ram_used_mb": ram_u,
        "ram_total_mb": ram_t,
        "disk_percent": disk,
        "docker_raw": docker_raw,
        "docker_status": collected.get("docker_status"),
        "docker_reason": collected.get("docker_reason") or "",
        "server_metrics_row": collected.get("server_metrics"),
        "psutil_row": collected.get("psutil"),
        "errors": errors,
    }
    if debug:
        out["debug_collect"] = collected
    return out


def _ener_log_dir_access() -> str:
    try:
        if _LOG_DIR.is_dir():
            next(_LOG_DIR.iterdir())
            return "ok"
    except OSError:
        return "no_log_access"
    except Exception:
        return "no_log_access"
    return "no_log_access"


def format_resource_diagnosis_thai(data: dict[str, Any]) -> str:
    lines = [
        "🖥️ **ตรวจสอบ Resource Ener-AI**",
        "",
        "**สรุป:**",
    ]
    cpu = data.get("cpu_percent")
    ram_p = data.get("ram_percent")
    ram_u = data.get("ram_used_mb")
    ram_t = data.get("ram_total_mb")
    disk = data.get("disk_percent")

    if cpu is not None:
        lines.append(f"- CPU ระบบ (host/container view): **{cpu:.1f}%**")
    else:
        lines.append("- CPU ระบบ: **ไม่มีข้อมูล**")
    pcpu = data.get("process_cpu_percent")
    if pcpu is not None:
        lines.append(f"- CPU โปรเซสบอท (psutil Process): **{pcpu:.1f}%**")

    if ram_p is not None and ram_u is not None and ram_t is not None:
        lines.append(f"- RAM: **{ram_u}MB / {ram_t}MB** (**{ram_p:.1f}%**)")
    elif ram_p is not None:
        lines.append(f"- RAM: **{ram_p:.1f}%**")
    else:
        lines.append("- RAM: **ไม่มีข้อมูล**")

    if disk is not None:
        lines.append(f"- Disk: **{disk:.1f}%**")
    else:
        lines.append("- Disk: **ไม่มีข้อมูล**")

    lines.extend(
        [
            "",
            "**หลักฐาน (provenance):**",
            f"- `source_used`: **{data.get('source_used', 'unknown')}**",
            f"- `collected_at`: **{data.get('collected_at', '')}**",
        ]
    )

    sm = data.get("server_metrics_row")
    if sm:
        lines.append(
            f"- `server_metrics` ล่าสุด (DB): recorded_at={sm.get('recorded_at')} "
            f"cpu={sm.get('cpu_percent')}% ram={sm.get('ram_percent')}% disk={sm.get('disk_percent')}%"
        )

    dstatus = data.get("docker_status")
    lines.append(f"- `docker_stats`: **{dstatus}**")
    if dstatus == "no_access" and data.get("docker_reason"):
        lines.append(f"  - reason: {sanitize_diagnostic_text(str(data.get('docker_reason')))[:400]}")

    dr = data.get("docker_raw")
    if dr:
        lines.append("- `docker` raw (รันจริงแล้ว):")
        lines.append(f"```\n{sanitize_diagnostic_text(str(dr)[:1800])}\n```")
    else:
        lines.append("- `docker` raw: **ไม่มี** (ไม่แสดง `output:` ปลอม)")

    errs = data.get("errors") or []
    if errs:
        lines.append("")
        lines.append("**ข้อจำกัด collector:** " + "; ".join(sanitize_diagnostic_text(str(e))[:200] for e in errs[:4]))

    log_st = _ener_log_dir_access()
    lines.append("")
    lines.append(f"- **log file:** `{log_st}`" + (" (ไม่พบ `/var/log/ener-ai` หรืออ่านไม่ได้)" if log_st != "ok" else ""))

    body = "\n".join(lines)[:3600] + _diag_provenance_footer()
    return sanitize_diagnostic_text(body[:3900])


def format_resource_debug_appendix(data: dict[str, Any]) -> str:
    """Extra sanitized raw collector payload for /resource_debug only."""
    raw = data.get("debug_collect")
    if not raw:
        return ""
    try:
        js = json.dumps(raw, ensure_ascii=False)
    except TypeError:
        js = str(raw)
    js = sanitize_diagnostic_text(js)[:2800]
    return f"\n\n**debug_collect (sanitize):**\n```\n{js}\n```"


def classify_diagnostic_intent(text: str) -> str | None:
    if is_communication_followup_intent(text):
        return None
    t = (text or "").lower()
    th = text or ""
    otp_kw = [
        "otp", "รหัส otp", "ส่ง otp", "otp วน", "otp loop", "otp ส่ง",
        "เช็ค otp", "เช็คotp", "ทำไม otp", "otp ซ้ำ", "otp มารัว", "otp ตลอด",
        "รหัส otp", "รหัสotp",
    ]
    if any(k in t for k in otp_kw) or ("otp" in t and any(x in t for x in ["ส่ง", "วน", "ทำไม", "เช็ค", "ซ้ำ", "loop", "รัว", "ตลอด", "รหัส"])):
        return "otp"
    bot_kw = [
        "bot ไม่ตอบ",
        "บอทไม่ตอบ",
        "telegram ไม่ตอบ",
        "webhook",
        "ระบบไม่ตอบ",
        "ener ไม่ตอบ",
    ]
    if any(k in t for k in bot_kw):
        return "bot"
    agent_kw = [
        "memorykeeper",
        "memory curator",
        "memorycurator",
        "agent ล้ม",
        "ระบบล่ม",
    ]
    if any(k in t for k in agent_kw):
        return "agent"
    if matches_resource_diagnostic_intent(th):
        return "resource"
    return None


def _confidence_label(evidence_sufficient: bool, loop_flag: bool) -> str:
    if loop_flag and evidence_sufficient:
        return "สูง"
    if evidence_sufficient:
        return "กลาง"
    return "ต่ำ"


def format_otp_diagnosis_thai(data: dict[str, Any], *, include_provenance_footer: bool = True) -> str:
    ev = data.get("evidence") or {}
    st = ev.get("otp_state") or {}
    an = ev.get("analysis") or {}
    events = ev.get("otp_events") or []
    logs = ev.get("app_logs") or {}
    git = ev.get("git") or {}
    errs = data.get("errors") or []

    lines = ["🔎 ตรวจสอบ OTP Loop", ""]
    sufficient = an.get("evidence_sufficient") or len(events) >= 3
    loop_flag = bool(an.get("repeated_5min_otp_loop"))
    conf = _confidence_label(bool(sufficient), loop_flag)

    lines.append("สรุป:")
    if not events:
        lines.append(
            "ตอนนี้ยังไม่มี forensic log ใน `otp_audit_logs` (หรือยังไม่มี event ในช่วง 6 ชม.) "
            "จึงบอก pattern จาก state/log file เท่าที่อ่านได้เท่านั้น — **ห้ามอ้างว่า “รันแล้ว” โดยไม่มีหลักฐาน**"
        )
    else:
        n_sent = an.get("admin_otp_sent_count", 0)
        lines.append(f"- พบ event ที่เกี่ยวกับการส่ง OTP (ประเภท SENT) จำนวน **{n_sent}** ครั้งในช่วงที่ดึงมา")
        if loop_flag:
            lines.append("- interval หลายคู่อยู่ช่วง **280–330 วินาที** → สอดคล้องกับ OTP_EXPIRE≈300s (**repeated_5min_otp_loop**)")
    lines.append("")

    lines.append("หลักฐาน (state ปัจจุบัน — ไม่มีรหัส OTP):")
    lines.append(f"- has_admin_otp: **{st.get('has_admin_otp')}** · หมดอายุใน ~{st.get('admin_otp_expires_in')}s")
    la = st.get("seconds_since_last_admin_otp_sent")
    lines.append(f"- ครั้งล่าสุดที่บันทึกว่าส่ง admin OTP: **{la}s** ที่แล้ว (ถ้ามี)")
    lines.append(f"- has_terminal_otp: **{st.get('has_terminal_otp')}**")
    lines.append("")

    if events:
        lines.append("**หลักฐาน (audit ล่าสุด — ไม่เก็บ OTP code):**")
        for e in events[-8:]:
            sv = _session_valid_label(e.get("session_valid"))
            lines.append(
                f"- {e.get('created_at')} **{e.get('event_type')}** path=`{e.get('path')}` "
                f"method={e.get('method')} session={sv} ua={(e.get('user_agent') or '')[:60]}… "
                f"ip={e.get('client_ip')}"
            )
        lines.append("")
        tp = an.get("top_paths") or []
        if tp:
            lines.append("**path ที่พบบ่อย:** " + ", ".join(f"`{p}`×{c}" for p, c in tp[:4]))
        tua = an.get("top_user_agents") or []
        if tua:
            lines.append("user-agent ที่พบบ่อย: " + "; ".join(f"{ua[:48]} ×{c}" for ua, c in tua[:3]))
        ps = an.get("events_before_sent") or {}
        if ps:
            lines.append(
                "**event ก่อนหน้า SENT (สรุป):** "
                + sanitize_diagnostic_text(json.dumps(ps, ensure_ascii=False))
            )
        lines.append("")

    lines.append("**สาเหตุที่เป็นไปได้:**")
    if loop_flag:
        lines.append("- client/เบราว์เซอร์/monitor ยิง endpoint ที่ทำให้เกิด OTP ใหม่เป็นระยะ ~5 นาที")
    elif "GET /admin/otp" in str(an.get("top_paths")):
        lines.append("- เคยมี traffic ไปที่ `/admin/otp` บ่อย — ตรวจใน audit ว่าเคย trigger `ADMIN_OTP_SENT` หรือไม่ (หลังแก้ GET ไม่ควรส่ง OTP)")
    else:
        lines.append("- ต้องดู audit เพิ่มเติมว่าเป็น `POST /admin/otp/send` ซ้ำหรือ redirect/session")
    lines.append("")

    lines.append(f"**ความมั่นใจ:** {conf}")
    lines.append("")
    lines.append("**สิ่งที่ควรตรวจ/แก้:**")
    lines.append("- `GET /admin/otp` ต้องไม่ส่ง OTP เอง (ส่งเฉพาะ `POST /admin/otp/send`)")
    lines.append("- ใช้ `otp_audit_logs` ติดตาม `ADMIN_OTP_PAGE_VIEW` vs `ADMIN_OTP_SENT`")
    lines.append("")

    if logs.get("status") == "ok" and logs.get("lines"):
        lines.append("**log file (คีย์เวิร์ด):** ตัวอย่างบรรทัดที่ match")
        for ln in (logs.get("lines") or [])[:5]:
            lines.append(f"  · {(ln or '')[:200]}")
    elif logs.get("status") == "no_log_access":
        lines.append("**log file:** `no_log_access` (ไม่พบ `/var/log/ener-ai` หรืออ่านไม่ได้)")

    if git.get("status") == "ok":
        lines.append("")
        lines.append("**git (ล่าสุด):** มี repo local — ดู `git log -10` / `git show HEAD` ในข้อมูลดิบ")
    elif git.get("status") == "no_git_access":
        lines.append("")
        lines.append(f"**git:** `no_git_access` — {git.get('detail', '')}")

    if errs:
        lines.append("")
        lines.append(
            "**ข้อจำกัดการเข้าถึง:** "
            + "; ".join(sanitize_diagnostic_text(str(x)) for x in errs[:4])
        )
        lines.append(
            "ผมยังเข้าถึงบางแหล่งไม่ได้ครบ — **จะไม่อ้างว่า “รันแล้ว” โดยไม่มีผลลัพธ์จริง**"
        )
    body = "\n".join(lines)[:3600]
    if include_provenance_footer:
        body = (body + _diag_provenance_footer())[:3900]
    return sanitize_diagnostic_text(body)


def format_agent_diagnosis_thai(data: dict[str, Any], *, include_provenance_footer: bool = True) -> str:
    ev = data.get("evidence") or {}
    lines = ["🔎 **ตรวจสอบ Agent / Memory**", ""]
    rows = ev.get("memory_agent_events") or []
    if rows:
        lines.append("**agent_events (memory ล่าสุด):**")
        for r in rows[:8]:
            sm = sanitize_diagnostic_text((r.get("summary") or "")[:120])
            lines.append(
                f"- {r.get('created_at')} `{r.get('agent_name')}` result={r.get('result')} — {sm}"
            )
    else:
        lines.append("ไม่พบ agent_events ที่ match memory ในช่วงล่าสุด")
    fails = ev.get("failed_ai_runs") or []
    if fails:
        lines.append("")
        lines.append("**ai_runs ที่ success=0:**")
        for r in fails[:6]:
            lines.append(f"- {r.get('created_at')} agent={r.get('agent')} model={r.get('model')}")
    lines.append("")
    if data.get("errors"):
        lines.append(
            "**ข้อจำกัด:** "
            + "; ".join(sanitize_diagnostic_text(str(x)) for x in data["errors"][:3])
        )
    else:
        lines.append("**ความมั่นใจ:** กลาง — อิงจากตาราง DB เท่านั้น ไม่เดา")
    body = "\n".join(lines)[:3600]
    if include_provenance_footer:
        body = (body + _diag_provenance_footer())[:3900]
    return sanitize_diagnostic_text(body)


def format_bot_diagnosis_thai(data: dict[str, Any], *, include_provenance_footer: bool = True) -> str:
    ev = data.get("evidence") or {}
    lines = ["🔎 **ตรวจสอบ Bot / Webhook**", ""]
    wh = ev.get("webhook_result") or {}
    wh_raw = sanitize_diagnostic_text(json.dumps(wh, ensure_ascii=False)[:800])
    lines.append(f"**getWebhookInfo:** `{wh_raw}`")
    lines.append(f"**HTTP:** {ev.get('webhook_http_status')}")
    if ev.get("models"):
        lines.append(
            "**model availability:** "
            + sanitize_diagnostic_text(str(ev.get("models")))[:1200]
        )
    lines.append(f"**messages row count:** {ev.get('messages_count')}")
    lines.append("")
    if data.get("errors"):
        lines.append(
            "**ข้อจำกัด:** "
            + "; ".join(sanitize_diagnostic_text(str(x)) for x in data["errors"])
        )
        lines.append("ถ้าไม่มีสิทธิ์หรือ token ไม่ครบ จะไม่อ้างว่าเช็คครบแล้ว")
    body = "\n".join(lines)[:3600]
    if include_provenance_footer:
        body = (body + _diag_provenance_footer())[:3900]
    return sanitize_diagnostic_text(body)


def format_system_diagnosis_thai(otp_d: dict, agent_d: dict, bot_d: dict) -> str:
    parts = [
        "🔎 **สรุปสุขภาพระบบ (จากหลักฐาน)**",
        "",
        "### OTP",
        format_otp_diagnosis_thai(otp_d, include_provenance_footer=False)[:1800],
        "",
        "### Agents",
        format_agent_diagnosis_thai(agent_d, include_provenance_footer=False)[:1200],
        "",
        "### Bot",
        format_bot_diagnosis_thai(bot_d, include_provenance_footer=False)[:1200],
    ]
    return sanitize_diagnostic_text(
        ("\n".join(parts)[:3600] + _diag_provenance_footer())[:3900]
    )


async def diagnose_user_message(message_text: str, chat_id: str) -> str:
    intent = classify_diagnostic_intent(message_text)
    cid = sanitize_diagnostic_text(str(chat_id))
    preview = sanitize_diagnostic_text((message_text or "")[:120])
    if not intent:
        return (
            "ผมยังจัดประเภทข้อความนี้เป็น diagnostic ไม่ได้ — "
            "ลองพิมพ์คำถามให้ชัด เช่น “ทำไม OTP ส่งตลอด” หรือใช้ `/otp_debug`"
        )
    await log_diagnostic_audit(
        "DIAG_REQUEST",
        f"intent={intent} natural=1 chat_id={cid} preview={preview}",
    )
    try:
        if intent == "otp":
            d = await diagnose_otp_loop()
            out = format_otp_diagnosis_thai(d)
        elif intent == "bot":
            d = await diagnose_bot_unresponsive()
            out = format_bot_diagnosis_thai(d)
        elif intent == "agent":
            d = await diagnose_agent_health()
            out = format_agent_diagnosis_thai(d)
        elif intent == "resource":
            d = await diagnose_resource_usage()
            out = format_resource_diagnosis_thai(d)
        else:
            return (
                "ผมยังจัดประเภทข้อความนี้เป็น diagnostic ไม่ได้ — "
                "ลองพิมพ์คำถามให้ชัด เช่น “ทำไม OTP ส่งตลอด” หรือใช้ `/otp_debug`"
            )
        await log_diagnostic_audit("DIAG_SUCCESS", f"intent={intent} natural=1 chat_id={cid}")
        return out
    except Exception as exc:
        await log_diagnostic_audit(
            "DIAG_FAILED",
            f"intent={intent} natural=1 chat_id={cid} err={type(exc).__name__}:{exc!s}"[:1900],
        )
        _log.warning("DIAGNOSE_USER_MESSAGE_FAILED intent=%s err=%s", intent, exc)
        return (
            "ผมยังเข้าถึง log/server ไม่ได้หรือเกิดข้อผิดพลาดขณะรวบรวมหลักฐาน จึงวิเคราะห์จากข้อมูลที่มีเท่านั้น\n"
            f"รายละเอียดทางเทคนิค: `{type(exc).__name__}`"
        )


def split_telegram_chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]
