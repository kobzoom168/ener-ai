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

    # ── REAL analysis straight from the DB (always works — no LLM needed) ──
    fail_by_agent: dict[str, int] = {}
    total_runs = total_fail = 0
    for row in stats:
        c = int(row["count"] or 0)
        total_runs += c
        if str(row["result"]).strip().lower() == "failure":
            fail_by_agent[row["agent_name"]] = fail_by_agent.get(row["agent_name"], 0) + c
            total_fail += c
    worst = max(fail_by_agent.values()) if fail_by_agent else 0
    health = "critical" if (worst >= 5 or total_fail > 15) else ("warning" if total_fail else "healthy")
    rate = round(100 * (total_runs - total_fail) / total_runs) if total_runs else 100
    issues = [f"{a} ล้มเหลว {n} ครั้ง/24ชม." for a, n in
              sorted(fail_by_agent.items(), key=lambda x: -x[1])]
    if failures:  # newest failure detail (most actionable line)
        issues.append(f"ล่าสุด: {failures[0]['agent_name']} — {str(failures[0]['summary'] or '')[:90]}")
    insights = [f"24ชม.: รัน {total_runs} ครั้ง สำเร็จ {rate}%"
                + ("" if total_fail else " — ไม่มี agent ล้ม ระบบแข็งแรง")]
    recommendations = []
    if worst >= 3:
        top = max(fail_by_agent, key=fail_by_agent.get)
        recommendations.append(f"เช็ค {top} (ล้มซ้ำ {fail_by_agent[top]} ครั้ง) — ดู logs/timeout/คีย์ API")

    # ── AI bonus: deeper insight if the model answers cleanly; harmless if it fails ──
    prompt = f"Agent Stats (24h):\n{stats_text}\n\nRecent Failures:\n{failures_text}\n\nวิเคราะห์สุขภาพระบบเชิงลึก"
    try:
        ai = await chat_json(prompt, system=LOG_KEEPER_SYSTEM, agent="logkeeper")
        for x in _clean_list(ai.get("insights", [])):
            if x not in insights:
                insights.append(x)
        for x in _clean_list(ai.get("recommendations", [])):
            if x not in recommendations:
                recommendations.append(x)
        ah = str(ai.get("health", "")).strip().lower()
        if ah == "critical" or (ah == "warning" and health == "healthy"):
            health = ah  # let the AI ESCALATE severity, never downgrade the real data
    except Exception:
        pass  # deterministic analysis above already stands — no "วิเคราะห์ไม่ได้" placeholder

    issues = _clean_list(issues)
    insights = _clean_list(insights)
    recommendations = _clean_list(recommendations)

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
