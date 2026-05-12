import docker as docker_sdk
import psutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.ai import chat, get_model_availability
from app.core.agents import log_agent_run
from app.core.config import settings
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

_BANGKOK = ZoneInfo("Asia/Bangkok")
MONITOR_SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: ช่วยกบมอนิเตอร์ระบบ Ener-AI ในมุม DevOps

สถาปัตยกรรมระบบ:
- FastAPI + Python 3.11 ใน Docker container
- SQLite (WAL mode) — อาจ lock ถ้าเขียนพร้อมกัน
- Telegram Bot (python-telegram-bot v21) — webhook mode
- APScheduler — cron jobs (news/digest/backup/health)
- Groq/Haiku/Gemini — AI providers (external API)
- gTTS + ffmpeg — TTS audio
- psutil — server monitoring

Error ที่ปกติและไม่ต้องกังวล:
- httpx.ConnectError ตอน startup → Telegram auto-retry
- database is locked เป็นครั้งคราว → WAL mode จัดการเอง
- 404 /robots.txt /.env /.git → bot scanner ทั่วไป
- WSServerHandshakeError 403 (edge-tts) → เปลี่ยนเป็น gTTS แล้ว

Error ที่ต้องแก้:
- database is locked บ่อยๆ (>5 ครั้ง/ชั่วโมง) → เพิ่ม timeout
- OOM / memory > 90% → restart / optimize
- disk > 90% → ลบ log เก่า
- telegram error ต่อเนื่อง → เช็ค webhook

ตอบเป็นภาษาไทย กระชับ บอกสาเหตุและวิธีแก้ชัดเจน""")

KNOWN_ERRORS = {
    "database is locked": {
        "cause": "มีหลาย process เขียน SQLite พร้อมกัน",
        "fix": "เพิ่ม busy_timeout หรือ queue การ write",
        "severity": "warning",
    },
    "httpx.ConnectError": {
        "cause": "Telegram API connect ไม่ได้ตอน startup",
        "fix": "ปกติ auto-retry เอง ไม่ต้องทำอะไร",
        "severity": "info",
    },
    "JSONDecodeError": {
        "cause": "AI ตอบกลับมาไม่ใช่ JSON ที่ถูกต้อง",
        "fix": "Groq/Haiku ตอบผิด format — ระบบ fallback แล้ว",
        "severity": "info",
    },
    "WSServerHandshakeError: 403": {
        "cause": "Edge TTS โดน Microsoft block IP Hetzner",
        "fix": "ใช้ gTTS แทนแล้ว — ไม่ต้องทำอะไร",
        "severity": "info",
    },
    "sqlite3.OperationalError": {
        "cause": "SQLite operation ล้มเหลว",
        "fix": "ตรวจสอบ disk space และ DB integrity",
        "severity": "warning",
    },
    "telegram.error.NetworkError": {
        "cause": "Network ขาดหาย Telegram ไม่ตอบ",
        "fix": "รอ auto-retry หรือ restart container",
        "severity": "warning",
    },
    "ConnectionRefusedError": {
        "cause": "Service ที่ connect ไม่รัน",
        "fix": "เช็ค Ollama หรือ service อื่นที่ใช้งาน",
        "severity": "high",
    },
}
_ERROR_WORDS = [
    "error",
    "warning",
    "exception",
    "traceback",
    "failed",
    "critical",
    "operationalerror",
    "networkerror",
    "connectionrefusederror",
    "jsondecodeerror",
    "database is locked",
]
_NOISE_PATTERNS = [
    "get /admin",
    "post /webhook",
    "get /robots.txt",
    "get /.env",
    "get /.git",
]


def get_docker_logs(lines: int = 20, filter_errors: bool = False) -> str:
    """ดึง docker logs ล่าสุด"""
    try:
        client = docker_sdk.from_env()

        containers = client.containers.list()
        target = None
        for container in containers:
            if "ener-ai" in container.name:
                target = container
                break

        if not target:
            return "ไม่พบ container ener-ai"

        output = target.logs(
            tail=lines,
            timestamps=True,
        ).decode("utf-8", errors="replace")

        if filter_errors:
            error_lines = _filter_relevant_log_lines(output, error_only=True)
            return "\n".join(error_lines) if error_lines else "ไม่พบ error"

        return output if output.strip() else "ไม่มี logs"
    except Exception as exc:
        return f"ดึง logs ไม่ได้: {exc}"


def get_server_stats() -> dict:
    """ดึงสถานะ server"""
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    processes = []
    try:
        proc_items = sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
            key=lambda proc: proc.info["cpu_percent"] or 0,
            reverse=True,
        )[:5]
        for proc in proc_items:
            processes.append(
                f"{proc.info['name']} CPU:{(proc.info['cpu_percent'] or 0):.1f}% "
                f"RAM:{(proc.info['memory_percent'] or 0):.1f}%"
            )
    except Exception:
        processes.append("อ่าน process ไม่ได้")

    return {
        "cpu_percent": cpu,
        "ram_used_gb": ram.used / 1024**3,
        "ram_total_gb": ram.total / 1024**3,
        "ram_percent": ram.percent,
        "disk_used_gb": disk.used / 1024**3,
        "disk_total_gb": disk.total / 1024**3,
        "disk_percent": disk.percent,
        "top_processes": processes,
    }


def format_nl_resource_report(stats: dict) -> str:
    """Snapshot for natural-language CPU/RAM/disk questions (real psutil, no shell hints)."""
    top = stats.get("top_processes") or []
    top_lines = "\n".join(f"- {p}" for p in top[:3]) if top else "- (อ่าน process ไม่ได้)"
    ok = stats["cpu_percent"] < 85 and stats["ram_percent"] < 90 and stats["disk_percent"] < 90
    status_line = "สถานะ: ปกติ" if ok else "สถานะ: ควรจับตา (ทรัพยากรใช้งานสูง)"
    return (
        "🖥️ **Ener-AI Resource**\n\n"
        f"CPU: **{stats['cpu_percent']:.1f}%**\n"
        f"RAM: **{stats['ram_percent']:.1f}%** "
        f"({stats['ram_used_gb']:.1f}/{stats['ram_total_gb']:.1f} GB)\n"
        f"Disk: **{stats['disk_percent']:.1f}%** "
        f"({stats['disk_used_gb']:.1f}/{stats['disk_total_gb']:.1f} GB)\n\n"
        f"{status_line}\n"
        "โปรเซสที่ใช้ทรัพยากรสูง:\n"
        f"{top_lines}\n"
    )


def format_server_stats(stats: dict) -> str:
    """format สำหรับ Telegram"""
    ram_bar = "█" * int(stats["ram_percent"] / 10) + "░" * (10 - int(stats["ram_percent"] / 10))
    cpu_bar = "█" * int(stats["cpu_percent"] / 10) + "░" * (10 - int(stats["cpu_percent"] / 10))
    disk_bar = "█" * int(stats["disk_percent"] / 10) + "░" * (10 - int(stats["disk_percent"] / 10))

    lines = [
        "🖥️ Server Status",
        "",
        f"CPU  [{cpu_bar}] {stats['cpu_percent']:.1f}%",
        f"RAM  [{ram_bar}] {stats['ram_percent']:.1f}% ({stats['ram_used_gb']:.1f}/{stats['ram_total_gb']:.1f} GB)",
        f"Disk [{disk_bar}] {stats['disk_percent']:.1f}% ({stats['disk_used_gb']:.1f}/{stats['disk_total_gb']:.1f} GB)",
        "",
        "🔝 Top Processes:",
    ]
    for proc in stats["top_processes"]:
        lines.append(f"  · {proc}")

    return "\n".join(lines)


def _filter_relevant_log_lines(log_text: str, error_only: bool = False) -> list[str]:
    lines = []
    for raw_line in log_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if any(pattern in lowered for pattern in _NOISE_PATTERNS):
            continue
        if error_only:
            if any(word in lowered for word in _ERROR_WORDS if word != "warning"):
                lines.append(line)
            continue
        if any(word in lowered for word in _ERROR_WORDS):
            lines.append(line)
    return lines


def analyze_error_patterns(log_text: str) -> list[dict]:
    findings = []
    error_counts: dict[str, dict] = {}
    for line in log_text.split("\n"):
        lowered = line.lower()
        for pattern, info in KNOWN_ERRORS.items():
            if pattern.lower() in lowered:
                if pattern not in error_counts:
                    error_counts[pattern] = {
                        "count": 0,
                        "pattern": pattern,
                        **info,
                    }
                error_counts[pattern]["count"] += 1

    for pattern_data in error_counts.values():
        if pattern_data["pattern"] == "database is locked" and pattern_data["count"] > 5:
            pattern_data["severity"] = "high"
            pattern_data["fix"] = "พบบ่อยผิดปกติ ควรเพิ่ม timeout, queue write หรือแยก job ที่เขียน DB"
        findings.append(pattern_data)
    return findings


def _basic_error_analysis(errors: str) -> str:
    if "ไม่พบ error" in errors:
        return "ไม่พบ error สำคัญใน logs ล่าสุด ระบบน่าจะปกติดี"
    lowered = errors.lower()
    issues = []
    if "database is locked" in lowered:
        issues.append("SQLite มีการเขียนพร้อมกันมากเกินไป ควรลด concurrent writes หรือเพิ่ม queue")
    if "traceback" in lowered or "exception" in lowered:
        issues.append("มี exception ในแอป ควรเปิดดู stack trace และไล่ root cause")
    if "docker" in lowered and "not found" in lowered:
        issues.append("container หรือ docker command อาจมีปัญหา")
    if not issues:
        issues.append("พบ error ใน logs ควรตรวจบรรทัดล่าสุดและดู service ที่เกี่ยวข้อง")
    return " | ".join(issues)


async def _analyze_with_groq(prompt: str) -> str:
    availability = get_model_availability()
    if not availability.get("groq"):
        return ""
    try:
        return await chat(
            prompt,
            system=MONITOR_SYSTEM,
            agent="monitor",
            preferred_model="groq",
            strict_model=True,
        )
    except Exception:
        return ""


async def _should_send_alert(alert_key: str, cooldown_minutes: int = 60) -> bool:
    now = datetime.now(_BANGKOK)
    key = f"last_alert_{alert_key}"
    async with get_db() as db:
        cursor = await db.execute("SELECT value FROM memories WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row:
            try:
                last_alert = datetime.fromisoformat(str(row["value"]))
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=_BANGKOK)
                if now - last_alert < timedelta(minutes=cooldown_minutes):
                    return False
            except Exception:
                pass
        await db.execute(
            """
            INSERT INTO memories (key, value, tag)
            VALUES (?, ?, 'monitor_alert')
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                tag = 'monitor_alert',
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, now.isoformat()),
        )
        await db.commit()
    return True


@log_agent_run("MonitorAgent")
async def cmd_logs(lines: int = 20) -> str:
    """/logs command"""
    logs = get_docker_logs(lines=max(lines, 50))
    now = datetime.now(_BANGKOK).strftime("%H:%M")
    relevant_lines = _filter_relevant_log_lines(logs, error_only=False)
    if not relevant_lines:
        return f"✅ ไม่พบ warning/error ที่สำคัญใน logs ล่าสุด ({now})"

    error_count = sum(1 for line in relevant_lines if any(word in line.lower() for word in ["error", "exception", "traceback", "critical", "failed"]))
    warning_count = sum(1 for line in relevant_lines if "warning" in line.lower())
    snippet = "\n".join(relevant_lines[-lines:])
    summary_prompt = (
        f"สรุป logs นี้แบบสั้นมาก\n"
        f"พบ {error_count} errors และ {warning_count} warnings\n"
        f"logs:\n{snippet[-1500:]}"
    )
    ai_summary = await _analyze_with_groq(summary_prompt)
    if not ai_summary:
        ai_summary = f"สรุป: พบ {error_count} errors, {warning_count} warnings, สาเหตุหลักคือ error ใน logs ล่าสุด"
    return f"📋 Logs ล่าสุด ({now})\n\n```\n{snippet}\n```\n\nสรุป: {ai_summary}"


@log_agent_run("MonitorAgent")
async def cmd_errors() -> str:
    """ดู errors อย่างเดียว"""
    logs = get_docker_logs(lines=200, filter_errors=True)
    patterns = analyze_error_patterns(logs)

    if not patterns:
        return "✅ ไม่พบ error pattern ที่น่าเป็นห่วงครับ"

    lines = ["⚠️ พบปัญหา:", ""]
    for pattern_data in patterns:
        severity_emoji = {"high": "🔴", "warning": "🟡", "info": "🟢"}.get(
            pattern_data["severity"],
            "🟡",
        )
        lines.append(
            f"{severity_emoji} {pattern_data['pattern']} (x{pattern_data['count']})\n"
            f"   สาเหตุ: {pattern_data['cause']}\n"
            f"   แก้ไข: {pattern_data['fix']}\n"
        )

    high_errors = [pattern for pattern in patterns if pattern["severity"] == "high"]
    if high_errors:
        error_summary = "\n".join(pattern["pattern"] for pattern in high_errors)
        ai_advice = await chat(
            f"errors: {error_summary}\nlogs:\n{logs[-1000:]}",
            system=MONITOR_SYSTEM,
            agent="monitor",
            preferred_model="groq",
            strict_model=True,
        )
        lines.append(f"🤖 AI วิเคราะห์:\n{ai_advice}")

    return "\n".join(lines)


@log_agent_run("MonitorAgent")
async def cmd_server() -> str:
    """/server command"""
    stats = get_server_stats()
    return format_server_stats(stats)


@log_agent_run("MonitorAgent")
async def cmd_status() -> str:
    """สรุปสถานะทั้งหมด — ไม่เรียก LLM ถ้า metrics + logs ปกติ"""
    stats = get_server_stats()
    errors = get_docker_logs(lines=50, filter_errors=True)
    err_stripped = (errors or "").strip()

    no_log_errors = (
        "ไม่พบ error" in err_stripped
        or "ไม่มี logs" in err_stripped
        or err_stripped == ""
    )
    cpu_ok = stats["cpu_percent"] < 80
    ram_ok = stats["ram_percent"] < 85
    disk_ok = stats["disk_percent"] < 80
    stats_text = format_server_stats(stats)

    if no_log_errors and cpu_ok and ram_ok and disk_ok:
        return f"{stats_text}\n\n📌 สรุป: ระบบปกติดี ไม่พบ error สำคัญ"

    summary = ""
    if not (no_log_errors and cpu_ok and ram_ok and disk_ok):
        prompt = (
            "คุณได้รับเฉพาะหลักฐานด้านล่างนี้เท่านั้น — **ห้าม** อ้าง API version, service ภายนอก, "
            "หรือข้อมูลที่ไม่ได้ปรากฏใน evidence\n\n"
            "=== Evidence ===\n"
            f"CPU: {stats['cpu_percent']:.1f}%\n"
            f"RAM: {stats['ram_percent']:.1f}% ({stats['ram_used_gb']:.1f}/{stats['ram_total_gb']:.1f} GB)\n"
            f"Disk: {stats['disk_percent']:.1f}%\n\n"
            "=== Recent error log excerpt ===\n"
            f"{errors[:1200]}\n\n"
            "สรุปสั้น ๆ เป็นภาษาไทย: สภาพทรัพยากร + มีความเสี่ยงจาก logs หรือไม่ (อ้างอิงเฉพาะข้อความใน excerpt)"
        )
        summary = await _analyze_with_groq(prompt)
    if not summary:
        summary = _basic_error_analysis(errors)

    return f"{stats_text}\n\n🤖 AI Analysis:\n{summary}"


@log_agent_run("MonitorAgent", triggered_by="scheduler")
async def check_and_alert(bot) -> str | None:
    """
    เรียกจาก scheduler ทุก 10 นาที
    ถ้าพบปัญหา → ส่ง Telegram alert
    """
    stats = get_server_stats()
    errors = get_docker_logs(lines=200, filter_errors=True)
    patterns = analyze_error_patterns(errors)

    alert_lines = []
    high_patterns = [pattern for pattern in patterns if pattern["severity"] == "high"]
    for pattern in high_patterns:
        if await _should_send_alert(pattern["pattern"]):
            alert_lines.append(
                f"🔴 {pattern['pattern']} (x{pattern['count']})\n"
                f"สาเหตุ: {pattern['cause']}\n"
                f"แก้ไข: {pattern['fix']}"
            )

    if stats["cpu_percent"] > 85 and await _should_send_alert("cpu"):
        alert_lines.append(f"🟡 CPU สูง {stats['cpu_percent']:.0f}%")
    if stats["ram_percent"] > 90 and await _should_send_alert("ram"):
        alert_lines.append(f"🔴 RAM สูง {stats['ram_percent']:.0f}%")
    if stats["disk_percent"] > 85 and await _should_send_alert("disk"):
        alert_lines.append(f"🟡 Disk สูง {stats['disk_percent']:.0f}%")

    if not alert_lines:
        return None

    issue_summary = "\n".join(alert_lines)
    analysis = ""
    if high_patterns:
        prompt = (
            f"ปัญหาที่พบ:\n{issue_summary}\n\n"
            f"Errors:\n{errors[-1200:]}\n\n"
            "สรุปและแนะนำวิธีแก้สั้นๆ"
        )
        analysis = await _analyze_with_groq(prompt)
    if not analysis:
        analysis = _basic_error_analysis(errors)

    alert_msg = f"⚠️ Server Alert\n\n{issue_summary}\n\n🤖 สรุป: {analysis}"
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=alert_msg,
        parse_mode=None,
    )

    try:
        await log_event(
            agent_name="MonitorAgent",
            event_type="warning",
            summary=f"Alert: {issue_summary[:200]}",
            tags=["monitor", "alert"],
            result="success",
        )
    except Exception:
        pass

    return alert_msg
