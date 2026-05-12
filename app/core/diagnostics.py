"""
ChatOps-style diagnostics from real evidence (DB, logs, Telegram API).
Never exposes OTP codes. Does not claim to have run checks without results.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

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
    session_present = session_valid = auth_present = 0
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
        # session_valid requires async session check — omitted intentionally (no false positives)
        session_valid = 0

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
    except Exception:
        pass


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
        return [dict(r) for r in rows]
    except Exception:
        return []


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
                    matched.append(line[:500])
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
            "log_oneline": (log_r.stdout or log_r.stderr or "")[:2000],
            "head_stat": (show_r.stdout or show_r.stderr or "")[:2000],
        }
    except Exception as exc:
        return {"status": "no_git_access", "detail": str(exc)[:200]}


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
    pre_sent = []
    for i, e in enumerate(events):
        if e.get("event_type") in sent_types and i > 0:
            pre_sent.append(events[i - 1].get("event_type"))
    from collections import Counter
    pre_counter = Counter(pre_sent)
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
        out["errors"].append(f"otp_state:{exc}")
    try:
        out["evidence"]["otp_events"] = await collect_otp_audit_events(6)
    except Exception as exc:
        out["errors"].append(f"otp_events:{exc}")
    try:
        out["evidence"]["app_logs"] = await collect_recent_app_logs(
            ["OTP", "ADMIN_OTP", "/admin/otp", "Session expired", "Redirect", "error"],
            80,
        )
    except Exception as exc:
        out["errors"].append(f"app_logs:{exc}")
    try:
        out["evidence"]["git"] = await collect_recent_git_context()
    except Exception as exc:
        out["errors"].append(f"git:{exc}")
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
            out["evidence"]["memory_agent_events"] = [dict(r) for r in await cur.fetchall()]
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
            out["evidence"]["audit_tail"] = [dict(r) for r in await cur.fetchall()]
    except Exception as exc:
        out["errors"].append(str(exc))
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
                out["evidence"]["webhook_body"] = r.text[:500]
    except Exception as exc:
        out["errors"].append(f"webhook:{exc}")
    try:
        from app.core.ai import get_model_availability_async
        out["evidence"]["models"] = await get_model_availability_async()
    except Exception as exc:
        out["errors"].append(f"models:{exc}")
    try:
        async with get_db() as db:
            cur = await db.execute("SELECT COUNT(*) as c FROM messages")
            row = await cur.fetchone()
            out["evidence"]["messages_count"] = row["c"] if row else None
    except Exception as exc:
        out["errors"].append(f"db:{exc}")
    return out


def classify_diagnostic_intent(text: str) -> str | None:
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
        "bot ไม่ตอบ", "บอทไม่ตอบ", "ไม่ตอบ", "webhook", "telegram ไม่ทำงาน",
    ]
    if any(k in t for k in bot_kw):
        return "bot"
    agent_kw = [
        "memorykeeper", "memory curator", "memorycurator", "agent ล้ม",
        "เช็ค error", "ดู log", "เช็คระบบ", "ระบบล่ม",
    ]
    if any(k in t for k in agent_kw):
        return "agent"
    if "เช็คระบบ" in th or "check system" in t:
        return "system"
    return None


def _confidence_label(evidence_sufficient: bool, loop_flag: bool) -> str:
    if loop_flag and evidence_sufficient:
        return "สูง"
    if evidence_sufficient:
        return "กลาง"
    return "ต่ำ"


def format_otp_diagnosis_thai(data: dict[str, Any]) -> str:
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
            lines.append(
                f"- {e.get('created_at')} **{e.get('event_type')}** path=`{e.get('path')}` "
                f"method={e.get('method')} ua={(e.get('user_agent') or '')[:60]}… ip={e.get('client_ip')}"
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
            lines.append("**event ก่อนหน้า SENT (สรุป):** " + json.dumps(ps, ensure_ascii=False))
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
        lines.append("**ข้อจำกัดการเข้าถึง:** " + "; ".join(errs[:4]))
        lines.append(
            "ผมยังเข้าถึงบางแหล่งไม่ได้ครบ — **จะไม่อ้างว่า “รันแล้ว” โดยไม่มีผลลัพธ์จริง**"
        )
    return "\n".join(lines)[:3900]


def format_agent_diagnosis_thai(data: dict[str, Any]) -> str:
    ev = data.get("evidence") or {}
    lines = ["🔎 **ตรวจสอบ Agent / Memory**", ""]
    rows = ev.get("memory_agent_events") or []
    if rows:
        lines.append("**agent_events (memory ล่าสุด):**")
        for r in rows[:8]:
            lines.append(
                f"- {r.get('created_at')} `{r.get('agent_name')}` result={r.get('result')} — {(r.get('summary') or '')[:120]}"
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
        lines.append("**ข้อจำกัด:** " + "; ".join(data["errors"][:3]))
    else:
        lines.append("**ความมั่นใจ:** กลาง — อิงจากตาราง DB เท่านั้น ไม่เดา")
    return "\n".join(lines)[:3900]


def format_bot_diagnosis_thai(data: dict[str, Any]) -> str:
    ev = data.get("evidence") or {}
    lines = ["🔎 **ตรวจสอบ Bot / Webhook**", ""]
    wh = ev.get("webhook_result") or {}
    lines.append(f"**getWebhookInfo:** `{json.dumps(wh, ensure_ascii=False)[:800]}`")
    lines.append(f"**HTTP:** {ev.get('webhook_http_status')}")
    if ev.get("models"):
        lines.append(f"**model availability:** {ev.get('models')}")
    lines.append(f"**messages row count:** {ev.get('messages_count')}")
    lines.append("")
    if data.get("errors"):
        lines.append("**ข้อจำกัด:** " + "; ".join(data["errors"]))
        lines.append("ถ้าไม่มีสิทธิ์หรือ token ไม่ครบ จะไม่อ้างว่าเช็คครบแล้ว")
    return "\n".join(lines)[:3900]


def format_system_diagnosis_thai(otp_d: dict, agent_d: dict, bot_d: dict) -> str:
    parts = [
        "🔎 **สรุปสุขภาพระบบ (จากหลักฐาน)**",
        "",
        "### OTP",
        format_otp_diagnosis_thai(otp_d)[:1800],
        "",
        "### Agents",
        format_agent_diagnosis_thai(agent_d)[:1200],
        "",
        "### Bot",
        format_bot_diagnosis_thai(bot_d)[:1200],
    ]
    return "\n".join(parts)[:3900]


async def diagnose_user_message(message_text: str, chat_id: str) -> str:
    _ = chat_id
    intent = classify_diagnostic_intent(message_text)
    if not intent:
        return (
            "ผมยังจัดประเภทข้อความนี้เป็น diagnostic ไม่ได้ — "
            "ลองพิมพ์คำถามให้ชัด เช่น “ทำไม OTP ส่งตลอด” หรือใช้ `/otp_debug`"
        )
    try:
        if intent == "otp":
            d = await diagnose_otp_loop()
            return format_otp_diagnosis_thai(d)
        if intent == "bot":
            d = await diagnose_bot_unresponsive()
            return format_bot_diagnosis_thai(d)
        if intent == "agent":
            d = await diagnose_agent_health()
            return format_agent_diagnosis_thai(d)
        # system
        o, a, b = await asyncio.gather(
            diagnose_otp_loop(),
            diagnose_agent_health(),
            diagnose_bot_unresponsive(),
        )
        return format_system_diagnosis_thai(o, a, b)
    except Exception as exc:
        return (
            "ผมยังเข้าถึง log/server ไม่ได้หรือเกิดข้อผิดพลาดขณะรวบรวมหลักฐาน จึงวิเคราะห์จากข้อมูลที่มีเท่านั้น\n"
            f"รายละเอียดทางเทคนิค: `{type(exc).__name__}`"
        )


def split_telegram_chunks(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]
