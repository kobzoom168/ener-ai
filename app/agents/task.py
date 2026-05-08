from datetime import date
from app.core.agents import log_agent_run
from app.core.database import get_db


_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


@log_agent_run("TaskAgent")
async def create_task(title: str, priority: str = "medium", deadline_hint: str = "") -> str:
    async with get_db() as db:
        cursor = await db.execute(
            "INSERT INTO tasks (title, priority, deadline_hint) VALUES (?, ?, ?)",
            (title, priority, deadline_hint),
        )
        task_id = cursor.lastrowid

        today = date.today().isoformat()
        await db.execute(
            "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
            (today, "task", f"สร้าง task [{task_id}]: {title}"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("task_created", f"id={task_id} title={title}"),
        )
        await db.commit()

    emoji = _PRIORITY_EMOJI.get(priority, "🟡")
    lines = [
        f"📌 สร้าง task สำเร็จ",
        f"",
        f"🎯 [{task_id}] {title} {emoji}",
    ]
    if deadline_hint:
        lines.append(f"📅 deadline: {deadline_hint}")
    return "\n".join(lines)


@log_agent_run("TaskAgent")
async def list_tasks() -> str:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id, title, priority, deadline_hint FROM tasks WHERE status = 'open' ORDER BY id",
        )
        rows = await cursor.fetchall()

    if not rows:
        return "📌 ไม่มี task ค้างอยู่ 🟢"

    lines = [f"📌 Tasks ทั้งหมด ({len(rows)} รายการ)", ""]
    for row in rows:
        emoji = _PRIORITY_EMOJI.get(row["priority"], "🟡")
        deadline = f" · {row['deadline_hint']}" if row["deadline_hint"] else ""
        lines.append(f"{emoji} [{row['id']}] {row['title']}{deadline}")

    return "\n".join(lines)


@log_agent_run("TaskAgent")
async def complete_task(task_id: int) -> str:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT title FROM tasks WHERE id = ? AND status = 'open'",
            (task_id,),
        )
        row = await cursor.fetchone()

        if not row:
            return f"📌 ไม่พบ task [{task_id}] หรือปิดไปแล้ว"

        await db.execute(
            "UPDATE tasks SET status = 'done', done_at = CURRENT_TIMESTAMP WHERE id = ?",
            (task_id,),
        )
        today = date.today().isoformat()
        await db.execute(
            "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
            (today, "task", f"ปิด task [{task_id}]: {row['title']}"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("task_done", f"id={task_id}"),
        )
        await db.commit()

    return f"📌 ปิด task สำเร็จ\n\n✅ [{task_id}] {row['title']}"
