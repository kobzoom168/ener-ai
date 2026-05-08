from app.core.agents import log_agent_run
from app.core.database import get_db


@log_agent_run("CostAgent")
async def get_cost_report(chat_id: str) -> str:
    async with get_db() as db:
        today_cursor = await db.execute(
            "SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total FROM ai_runs WHERE date(created_at) = date('now', 'localtime')",
        )
        today_row = await today_cursor.fetchone()
        month_cursor = await db.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
            """,
        )
        month_row = await month_cursor.fetchone()
        model_cursor = await db.execute(
            """
            SELECT model, COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
            GROUP BY model
            ORDER BY total DESC, model
            """,
        )
        model_rows = await model_cursor.fetchall()
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("cost_viewed", f"chat_id={chat_id}"),
        )
        await db.commit()

    lines = [
        "📌 สรุปค่าใช้จ่าย AI",
        "",
        "📊 ค่าใช้จ่าย AI",
        "",
        f"วันนี้: {float(today_row['total']):.2f} บาท",
        f"เดือนนี้: {float(month_row['total']):.2f} บาท",
        "",
        "แยกตาม model:",
    ]
    if model_rows:
        for row in model_rows:
            lines.append(f"· {row['model']}: {float(row['total']):.2f} บาท")
    else:
        lines.append("· ยังไม่มี")
    return "\n".join(lines)
