from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import get_agent_context
from app.core.policy import build_system_prompt

LOG_KEEPER_SYSTEM = build_system_prompt("""
งานของพี่ตอนนี้: ดูแล agent memory ของ Ener-AI

หน้าที่:
1. วิเคราะห์ agent_events และหา pattern
2. แจ้งเตือนถ้ามี agent fail ซ้ำๆ
3. สรุปสิ่งที่ agents เรียนรู้
4. ให้คำแนะนำก่อน agent ทำงาน

ตอบเป็น JSON:
{
  "health": "healthy|warning|critical",
  "issues": ["agent X fail 3 ครั้งใน 1 ชั่วโมง"],
  "insights": ["CodeAgent ทำงานดีขึ้น 20% หลัง fix async"],
  "recommendations": ["ควร retry CodeAgent ด้วย timeout 30s"]
}
""")


def _clean_list(values, limit: int = 5) -> list[str]:
    if not isinstance(values, list):
        return []
    items = []
    seen = set()
    for value in values:
        text = " ".join(str(value or "").split()).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


@log_agent_run("LogKeeper", triggered_by="scheduler")
async def analyze_agent_health() -> str:
    """วิเคราะห์สุขภาพของ agent ทั้งหมด"""
    async with get_db() as db:
        stats = await (
            await db.execute(
                """
                SELECT agent_name, result, COUNT(*) as count
                FROM agent_events
                WHERE created_at > datetime('now', '-24 hours')
                GROUP BY agent_name, result
                ORDER BY agent_name, result
                """
            )
        ).fetchall()

        failures = await (
            await db.execute(
                """
                SELECT agent_name, summary, learned,
                       datetime(created_at, '+7 hours') as local_time
                FROM agent_events
                WHERE result = 'failure'
                AND created_at > datetime('now', '-24 hours')
                ORDER BY created_at DESC
                LIMIT 10
                """
            )
        ).fetchall()

    stats_text = "\n".join(
        f"{row['agent_name']}: {row['result']} × {row['count']}"
        for row in stats
    ) or "- ไม่มีสถิติ"

    failures_text = "\n".join(
        f"{row['agent_name']}: {row['summary']}"
        for row in failures
    ) or "- ไม่มี failure"

    prompt = f"""
Agent Stats (24h):
{stats_text}

Recent Failures:
{failures_text}

วิเคราะห์สุขภาพระบบ
"""

    try:
        result = await chat_json(prompt, system=LOG_KEEPER_SYSTEM, agent="logkeeper")
    except Exception:
        result = {
            "health": "warning" if failures else "healthy",
            "issues": ["ยังวิเคราะห์เชิงลึกไม่ได้"] if failures else [],
            "insights": ["ระบบยังเดินต่อได้"],
            "recommendations": ["ตรวจสอบ logs เพิ่มเติมถ้ามี failure ซ้ำ"],
        }

    health = str(result.get("health", "healthy")).strip().lower() or "healthy"
    issues = _clean_list(result.get("issues", []))
    insights = _clean_list(result.get("insights", []))
    recommendations = _clean_list(result.get("recommendations", []))

    health_emoji = {"healthy": "✅", "warning": "⚠️", "critical": "🔴"}.get(health, "✅")
    lines = [f"🔍 Agent Health: {health_emoji} {health}"]

    if issues:
        lines.append("")
        lines.append("⚠️ Issues:")
        for issue in issues:
            lines.append(f"  · {issue}")

    if insights:
        lines.append("")
        lines.append("💡 Insights:")
        for insight in insights:
            lines.append(f"  · {insight}")

    if recommendations:
        lines.append("")
        lines.append("🎯 แนะนำ:")
        for item in recommendations:
            lines.append(f"  · {item}")

    return "\n".join(lines)


async def get_pre_flight_context(agent_name: str, tags: list[str]) -> str:
    """context ที่ agent ดึงก่อนทำงาน"""
    return await get_agent_context(agent_name, tags)
