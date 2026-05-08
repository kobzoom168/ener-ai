from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.policy import build_system_prompt

_BANGKOK = ZoneInfo("Asia/Bangkok")

SESSION_SYSTEM = build_system_prompt("""
วิเคราะห์บทสนทนาและ logs วันนี้ แล้วสร้าง session log
ตอบเป็น JSON:
{
  "key_insights": ["insight 1", "insight 2"],
  "decisions_made": ["decision 1"],
  "things_failed": ["สิ่งที่ไม่ work 1"],
  "open_questions": ["คำถามค้าง 1"],
  "next_focus": ["focus 1", "focus 2", "focus 3"]
}

กฎ:
- ตอบภาษาไทย
- ถ้าไม่มีข้อมูลในหัวข้อใด ให้ส่งเป็น []
- next_focus ควรเป็นรายการที่ทำต่อได้จริงและสั้นกระชับ
- ห้ามตอบนอก JSON
""")


def _clean_items(values, limit: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    seen = set()
    for item in values:
        text = " ".join(str(item or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def _format_bullets(title: str, icon: str, items: list[str]) -> list[str]:
    lines = [f"{icon} {title}:"]
    if items:
        for item in items:
            lines.append(f"· {item}")
    else:
        lines.append("· ไม่มี")
    return lines


@log_agent_run("SessionAgent", triggered_by="scheduler")
async def generate_session_log() -> str:
    today = datetime.now(_BANGKOK).date().isoformat()

    async with get_db() as db:
        message_rows = await (
            await db.execute(
                """
                SELECT role, content, created_at
                FROM messages
                WHERE date(datetime(created_at, '+7 hours')) = ?
                ORDER BY id
                """,
                (today,),
            )
        ).fetchall()
        audit_rows = await (
            await db.execute(
                """
                SELECT action, details, created_at
                FROM audit_logs
                WHERE date(datetime(created_at, '+7 hours')) = ?
                ORDER BY id
                LIMIT 200
                """,
                (today,),
            )
        ).fetchall()

    transcript_lines = [
        f"- {row['role']}: {' '.join(str(row['content']).split())[:240]}"
        for row in message_rows
    ]
    audit_lines = [
        f"- {row['action']}: {' '.join(str(row['details'] or '').split())[:220]}"
        for row in audit_rows
    ]

    prompt = (
        f"สรุป session ของวันที่ {today}\n\n"
        "=== Messages ===\n"
        + ("\n".join(transcript_lines) if transcript_lines else "- ไม่มี")
        + "\n\n=== Audit Logs ===\n"
        + ("\n".join(audit_lines) if audit_lines else "- ไม่มี")
    )

    try:
        result = await chat_json(prompt, system=SESSION_SYSTEM, agent="session")
    except Exception:
        result = {
            "key_insights": ["ยังสรุป insight อัตโนมัติไม่ได้"],
            "decisions_made": [],
            "things_failed": [],
            "open_questions": [],
            "next_focus": ["ตรวจสอบ session log วันนี้อีกครั้ง"],
        }

    key_insights = _clean_items(result.get("key_insights", []))
    decisions_made = _clean_items(result.get("decisions_made", []))
    things_failed = _clean_items(result.get("things_failed", []))
    open_questions = _clean_items(result.get("open_questions", []))
    next_focus = _clean_items(result.get("next_focus", []), limit=3)

    lines = [f"📅 Session {today}", ""]
    lines.extend(_format_bullets("Key Insights", "💡", key_insights))
    lines.append("")
    lines.extend(_format_bullets("Decisions", "✅", decisions_made))
    lines.append("")
    lines.extend(_format_bullets("Didn't Work", "❌", things_failed))
    lines.append("")
    lines.extend(_format_bullets("Open Questions", "❓", open_questions))
    lines.append("")
    lines.append("🎯 Next Focus:")
    if next_focus:
        for index, item in enumerate(next_focus, start=1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("1. ทบทวนสิ่งที่ทำวันนี้อีกครั้ง")

    summary_text = "\n".join(lines)

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO session_logs (
                log_date,
                key_insights,
                decisions_made,
                things_failed,
                open_questions,
                next_focus,
                raw_summary
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(log_date) DO UPDATE SET
                key_insights = excluded.key_insights,
                decisions_made = excluded.decisions_made,
                things_failed = excluded.things_failed,
                open_questions = excluded.open_questions,
                next_focus = excluded.next_focus,
                raw_summary = excluded.raw_summary
            """,
            (
                today,
                "\n".join(key_insights),
                "\n".join(decisions_made),
                "\n".join(things_failed),
                "\n".join(open_questions),
                "\n".join(next_focus),
                summary_text,
            ),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("session_log_generated", f"date={today}"),
        )
        await db.commit()

    return summary_text
