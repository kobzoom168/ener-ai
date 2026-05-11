from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from app.core.database import get_config, get_db

_BKK = ZoneInfo("Asia/Bangkok")

STANDUP_TEMPLATE = """{mention}
List Today {date_range}
##################################
{date_th}

หัวข้อที่ต้องการสื่อสารมีดังนี้

1.วันนี้ คุณพบงานที่มีปัญหา หรือไม่
ถ้ามี รบกวนแจ้งปัญหาให้ทราบด้วย
-->[Tanarit] : {problems}

2.วันนี้ คุณมีงานที่เป็นงานหลักที่ต้องทำคืออะไร
{projects_section}

3.วันนี้ คุณมีคำแนะนำหรือต้องการความช่วยเหลือจากทีม หรือไม่
-->[Tanarit] :
งานอื่น ๆ (Other Tasks)
{other_tasks}
##########################################################################
สิ่งที่ต้องทำวันนี้({today_date})

{today_section}"""


def _format_project(p: dict, idx: int) -> str:
    lines = [
        f"Project {idx} :{p['name']}",
        f">>สถานะปัจจุบัน: {p['current_status']}",
        f"Current Status: {p['status']}",
        f"% Complete: {p['percent_complete']}%",
    ]
    if p["next_steps"]:
        lines.append("--------------")
        for step in str(p["next_steps"]).split("\n"):
            if step.strip():
                lines.append(f">{step.strip()}")
    if p["due_date"]:
        lines.append("")
        lines.append(f"กำหนดการ Implementation: {p['due_date']}")
    lines.append("---------------------------------------------------------------")
    return "\n".join(lines)


async def generate_standup() -> str:
    mention = await get_config("standup_mention", "@Noom")
    now = datetime.now(_BKK)
    weekday = now.weekday()
    monday = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=weekday)
    friday = monday + timedelta(days=4)

    months_th = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                 "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

    m_start = f"{monday.day} {months_th[monday.month - 1]}"
    m_end = f"{friday.day} {months_th[friday.month - 1]} {friday.year}"
    date_range = f"{m_start} - {m_end}"
    date_th = now.strftime("%d-%b-%Y")
    today_date = now.strftime("%d-%b-%Y")

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM standup_projects WHERE is_active = 1 ORDER BY sort_order ASC, id ASC"
        )
        projects = await cursor.fetchall()

    projects_section_parts: list[str] = []
    today_section_items: list[str] = []
    for idx, row in enumerate(projects, 1):
        project = dict(row)
        projects_section_parts.append(_format_project(project, idx))
        if project.get("today_tasks"):
            today_section_items.append(f"{idx}. {project['name']}")
            for task in str(project["today_tasks"]).split("\n"):
                if task.strip():
                    today_section_items.append(f">{task.strip()}")

    projects_section = "\n".join(projects_section_parts).strip() or "-"
    today_section = "\n".join(today_section_items).strip() or "-"

    return STANDUP_TEMPLATE.format(
        mention=mention or "@Noom",
        date_range=date_range,
        date_th=date_th,
        problems="-",
        projects_section=projects_section,
        other_tasks="-",
        today_date=today_date,
        today_section=today_section,
    )


async def send_to_line(message: str) -> tuple[bool, str]:
    """Send to LINE group via Messaging API push endpoint."""
    token = await get_config("line_channel_access_token")
    line_to = await get_config("line_to")
    if not token or not line_to:
        return False, "ยังไม่ได้ตั้งค่า LINE token หรือ Group ID"

    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": line_to,
                    "messages": [{"type": "text", "text": str(message or "")}],
                },
                timeout=15.0,
            )
        if resp.status_code == 200:
            return True, "ส่งสำเร็จ"
        return False, f"LINE API error {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, str(exc)


async def list_projects() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                status,
                percent_complete,
                current_status,
                next_steps,
                due_date,
                today_tasks,
                sort_order,
                is_active,
                datetime(updated_at, '+7 hours') AS local_updated_at
            FROM standup_projects
            ORDER BY sort_order ASC, id ASC
            """
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "name": str(row["name"] or ""),
            "status": str(row["status"] or "In Progress"),
            "percent_complete": int(row["percent_complete"] or 0),
            "current_status": str(row["current_status"] or ""),
            "next_steps": str(row["next_steps"] or ""),
            "due_date": str(row["due_date"] or ""),
            "today_tasks": str(row["today_tasks"] or ""),
            "sort_order": int(row["sort_order"] or 0),
            "is_active": int(row["is_active"] or 0),
            "updated_at": str(row["local_updated_at"] or ""),
        }
        for row in rows
    ]


async def update_project_field(project_id: int, field: str, value: str | int) -> bool:
    allowed = {
        "status",
        "percent_complete",
        "current_status",
        "next_steps",
        "due_date",
        "today_tasks",
    }
    if field not in allowed:
        return False

    normalized_value: str | int = value
    if field == "percent_complete":
        try:
            normalized_value = max(0, min(100, int(str(value).strip() or "0")))
        except Exception:
            return False
    else:
        normalized_value = str(value or "").strip()

    async with get_db() as db:
        cursor = await db.execute(
            f"UPDATE standup_projects SET {field} = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (normalized_value, project_id),
        )
        await db.commit()
    return cursor.rowcount > 0


async def parse_and_update_from_chat(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return ""

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, name FROM standup_projects WHERE is_active = 1 ORDER BY sort_order ASC, id ASC"
        )
        projects = await cursor.fetchall()

    matched_project = None
    project_id_match = re.search(r"\bproject\s*(\d+)\b", text, re.IGNORECASE)
    if project_id_match:
        target_id = int(project_id_match.group(1))
        matched_project = next((row for row in projects if int(row["id"]) == target_id), None)

    lowered = text.lower()
    if not matched_project:
        for project in projects:
            name = str(project["name"] or "")
            keywords = [token.strip().lower() for token in re.split(r"[\s/()]+", name) if token.strip()]
            if name.lower() in lowered or any(token and token in lowered for token in keywords):
                matched_project = project
                break

    pct_match = re.search(r"(\d{1,3})\s*%", text)
    if matched_project and pct_match:
        pct = max(0, min(100, int(pct_match.group(1))))
        await update_project_field(int(matched_project["id"]), "percent_complete", pct)
        return f"✅ อัปเดต {matched_project['name']} เป็น {pct}% แล้วครับกบ"

    return ""
