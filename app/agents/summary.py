from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from app.core.ai import chat
from app.core.database import get_db

_BANGKOK = ZoneInfo("Asia/Bangkok")
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def generate_daily_summary() -> str:
    today = datetime.now(_BANGKOK).date()
    today_text = today.strftime("%d/%m/%Y")

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT category, content FROM daily_logs WHERE log_date = ? ORDER BY id",
            (today.isoformat(),),
        )
        rows = await cursor.fetchall()

        cursor = await db.execute(
            """
            SELECT id, title, priority, deadline_hint
            FROM tasks
            WHERE status = 'open'
            ORDER BY
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                id
            LIMIT 3
            """,
        )
        open_tasks = await cursor.fetchall()

        grouped: dict[str, list[str]] = {
            "note": [],
            "task": [],
            "idea": [],
            "mistake": [],
            "news": [],
            "insight": [],
        }

        created_tasks = 0
        closed_tasks = 0

        for row in rows:
            category = row["category"]
            content = row["content"]
            if category in grouped:
                grouped[category].append(content)
            if category == "task":
                if content.startswith("สร้าง task"):
                    created_tasks += 1
                if content.startswith("ปิด task"):
                    closed_tasks += 1

        lines = [
            f"📌 สรุปวันนี้ {today_text}",
            "",
            f"📅 สรุปวันที่ {today_text}",
            "",
            f"📝 NOTES ({len(grouped['note'])})",
        ]

        if grouped["note"]:
            for item in grouped["note"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", f"✅ TASKS ({created_tasks} สร้าง / {closed_tasks} ปิด)"])
        if grouped["task"]:
            for item in grouped["task"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", f"💡 IDEAS ({len(grouped['idea'])})"])
        if grouped["idea"]:
            for item in grouped["idea"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", f"❌ MISTAKES ({len(grouped['mistake'])})"])
        if grouped["mistake"]:
            for item in grouped["mistake"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", f"📰 NEWS ({len(grouped['news'])})"])
        if grouped["news"]:
            for item in grouped["news"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", f"🔍 INSIGHTS ({len(grouped['insight'])})"])
        if grouped["insight"]:
            for item in grouped["insight"]:
                lines.append(f"· {item}")
        else:
            lines.append("· ไม่มี")

        lines.extend(["", "🎯 พรุ่งนี้ top 3:"])
        if open_tasks:
            for row in open_tasks:
                emoji = _PRIORITY_EMOJI.get(row["priority"], "🟡")
                deadline = f" · {row['deadline_hint']}" if row["deadline_hint"] else ""
                lines.append(f"· {emoji} [{row['id']}] {row['title']}{deadline}")
        else:
            lines.append("· ไม่มี task ค้าง")

        summary_text = "\n".join(lines)

        await db.execute(
            """
            INSERT INTO digests (digest_type, period_start, period_end, content)
            VALUES (?, ?, ?, ?)
            """,
            ("daily", today.isoformat(), today.isoformat(), summary_text),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("daily_summary_generated", f"date={today.isoformat()}"),
        )
        await db.commit()

    return summary_text


async def generate_weekly_summary() -> str:
    today = datetime.now(_BANGKOK).date()
    start_date = today - timedelta(days=6)

    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT log_date, category, content
            FROM daily_logs
            WHERE log_date BETWEEN ? AND ?
            ORDER BY log_date, id
            """,
            (start_date.isoformat(), today.isoformat()),
        )
        rows = await cursor.fetchall()

    if not rows:
        return "📌 ยังไม่มีข้อมูลสำหรับรีวิว 7 วันที่ผ่านมา"

    log_lines = [
        f"- {row['log_date']} | {row['category']} | {row['content']}"
        for row in rows
    ]
    prompt = (
        f"สรุป weekly review ช่วง {start_date.isoformat()} ถึง {today.isoformat()} จาก log ด้านล่าง\n\n"
        + "\n".join(log_lines)
        + "\n\n"
        + "ตอบภาษาไทยแบบกระชับ โดยมีหัวข้อดังนี้:\n"
        + "1) ภาพรวมสัปดาห์\n"
        + "2) งานที่เดินหน้า/งานที่ค้าง\n"
        + "3) ไอเดียหรือบทเรียนสำคัญ\n"
        + "4) โฟกัสที่ควรทำต่อสัปดาห์หน้า\n"
        + "ห้ามเกริ่นนำยาว และไม่ต้องใส่ markdown code block"
    )
    weekly_body = (await chat(prompt, agent="summary")).strip()
    result = f"📌 รีวิว 7 วันที่ผ่านมา\n\n{weekly_body}"

    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            (
                "weekly_summary_generated",
                f"start={start_date.isoformat()} end={today.isoformat()}",
            ),
        )
        await db.commit()

    return result
