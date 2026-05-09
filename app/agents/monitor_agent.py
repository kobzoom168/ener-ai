import docker as docker_sdk
import psutil
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.ai import chat, get_model_availability
from app.core.agents import log_agent_run
from app.core.config import settings
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

_BANGKOK = ZoneInfo("Asia/Bangkok")
MONITOR_SYSTEM = build_system_prompt("""คุณเป็น Server Monitor AI ของ Ener-AI
วิเคราะห์ logs และสถานะ server
ตอบเป็นภาษาไทย กระชับ ตรงประเด็น
ถ้าไม่มีปัญหา → บอกสั้นๆ ว่าปกติดี
ถ้ามีปัญหา → บอกว่าคืออะไร และแนะนำวิธีแก้""")


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
            error_lines = [
                line
                for line in output.split("\n")
                if any(
                    word in line.lower()
                    for word in [
                        "error",
                        "exception",
                        "traceback",
                        "failed",
                        "critical",
                        "operationalerror",
                    ]
                )
            ]
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


@log_agent_run("MonitorAgent")
async def cmd_logs(lines: int = 20) -> str:
    """/logs command"""
    logs = get_docker_logs(lines=lines)
    now = datetime.now(_BANGKOK).strftime("%H:%M")
    snippet = logs[-2000:] if len(logs) > 2000 else logs
    return f"📋 Logs ล่าสุด {lines} บรรทัด ({now})\n\n```\n{snippet}\n```"


@log_agent_run("MonitorAgent")
async def cmd_errors() -> str:
    """ดู errors อย่างเดียว"""
    errors = get_docker_logs(lines=100, filter_errors=True)
    if "ไม่พบ error" in errors:
        return "✅ ไม่พบ error ใน logs ล่าสุดครับ"

    analysis = await _analyze_with_groq(f"วิเคราะห์ errors เหล่านี้:\n{errors[:2000]}")
    if not analysis:
        analysis = _basic_error_analysis(errors)
    return f"⚠️ Errors ที่พบ:\n\n{analysis}"


@log_agent_run("MonitorAgent")
async def cmd_server() -> str:
    """/server command"""
    stats = get_server_stats()
    return format_server_stats(stats)


@log_agent_run("MonitorAgent")
async def cmd_status() -> str:
    """สรุปสถานะทั้งหมด"""
    stats = get_server_stats()
    errors = get_docker_logs(lines=50, filter_errors=True)

    prompt = f"""
สรุปสถานะ Ener-AI server:

Server Stats:
- CPU: {stats['cpu_percent']}%
- RAM: {stats['ram_percent']}% ({stats['ram_used_gb']:.1f}GB)
- Disk: {stats['disk_percent']}%

Recent Errors:
{errors[:1000]}

ประเมินว่าระบบปกติดีไหม มีอะไรน่ากังวลไหม
"""
    summary = await _analyze_with_groq(prompt)
    if not summary:
        if "ไม่พบ error" in errors and stats["cpu_percent"] < 80 and stats["ram_percent"] < 85 and stats["disk_percent"] < 80:
            summary = "ระบบปกติดี ยังไม่เห็นสัญญาณน่ากังวล"
        else:
            summary = _basic_error_analysis(errors)

    stats_text = format_server_stats(stats)
    return f"{stats_text}\n\n🤖 AI Analysis:\n{summary}"


@log_agent_run("MonitorAgent", triggered_by="scheduler")
async def check_and_alert(bot) -> str | None:
    """
    เรียกจาก scheduler ทุก 30 นาที
    ถ้าพบปัญหา → ส่ง Telegram alert
    """
    stats = get_server_stats()
    errors = get_docker_logs(lines=100, filter_errors=True)

    issues = []
    if stats["cpu_percent"] > 80:
        issues.append(f"CPU สูง {stats['cpu_percent']:.0f}%")
    if stats["ram_percent"] > 85:
        issues.append(f"RAM สูง {stats['ram_percent']:.0f}%")
    if stats["disk_percent"] > 80:
        issues.append(f"Disk ใกล้เต็ม {stats['disk_percent']:.0f}%")
    if "ไม่พบ error" not in errors and len(errors) > 100:
        issues.append("พบ errors ใน logs")

    if not issues:
        return None

    prompt = f"""
ปัญหาที่พบ: {', '.join(issues)}

Errors: {errors[:1000]}

สรุปและแนะนำวิธีแก้สั้นๆ
"""
    analysis = await _analyze_with_groq(prompt)
    if not analysis:
        analysis = _basic_error_analysis(errors)

    alert_msg = f"⚠️ Server Alert\n\n{analysis}"
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=alert_msg,
        parse_mode=None,
    )

    try:
        await log_event(
            agent_name="MonitorAgent",
            event_type="warning",
            summary=f"Alert: {', '.join(issues)}",
            tags=["monitor", "alert"],
            result="success",
        )
    except Exception:
        pass

    return alert_msg
