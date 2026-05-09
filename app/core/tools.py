from datetime import date

from app.core.database import get_db

TOOLS = [
    {
        "name": "save_task",
        "description": "บันทึก task/งานที่ต้องทำลงระบบ",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "ชื่อ task"},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "deadline_hint": {"type": "string", "description": "กำหนดส่ง เช่น พรุ่งนี้"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "save_note",
        "description": "บันทึกความคิด ไอเดีย หรือสิ่งที่กบจด",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["idea", "feeling", "question", "random"],
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "remember_fact",
        "description": "จำข้อมูลสำคัญเกี่ยวกับกบไว้ระยะยาว",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "ข้อมูลที่ควรจำ"},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "search_memory",
        "description": "ค้นหาข้อมูลจากความจำในอดีต",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "run_brainstorm",
        "description": "วิเคราะห์ไอเดียหรือตัดสินใจ แบบ ThinkTeam",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "analyze_amulet",
        "description": "วิเคราะห์พระเครื่อง ราคา คุณสมบัติ",
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {"type": "string"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "create_content",
        "description": "เขียน caption หรือ script สำหรับโพสต์ขาย",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "platform": {
                    "type": "string",
                    "enum": ["tiktok", "facebook", "youtube", "general"],
                },
            },
            "required": ["topic"],
        },
    },
]


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    from app.agents import brainstorm, content_agent, ener_agent
    from app.agents import task as task_agent
    from app.agents.memory import search_memory

    payload = tool_input or {}

    if tool_name == "save_task":
        return await task_agent.create_task(
            title=str(payload["title"]).strip(),
            priority=str(payload.get("priority", "medium")).strip().lower() or "medium",
            deadline_hint=str(payload.get("deadline_hint", "")).strip(),
            _agent_triggered_by="agent",
        )

    if tool_name == "save_note":
        content = str(payload["content"]).strip()
        category = str(payload.get("category", "random")).strip().lower() or "random"
        async with get_db() as db:
            await db.execute(
                "INSERT INTO notes (content, category, ai_summary) VALUES (?, ?, ?)",
                (content, category, content[:80]),
            )
            await db.execute(
                "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                (date.today().isoformat(), "note", f"[{category}] {content[:120]}"),
            )
            await db.commit()
        return f"บันทึกแล้ว: {content[:50]}"

    if tool_name == "remember_fact":
        fact = str(payload["fact"]).strip()
        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO long_term_memories (content)
                SELECT ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM long_term_memories WHERE lower(content) = lower(?)
                )
                """,
                (fact, fact),
            )
            await db.commit()
        return f"จำแล้ว: {fact}"

    if tool_name == "search_memory":
        return await search_memory(str(payload["query"]).strip(), _agent_triggered_by="agent")

    if tool_name == "run_brainstorm":
        return await brainstorm.run_brainstorm(str(payload["topic"]).strip(), _agent_triggered_by="agent")

    if tool_name == "analyze_amulet":
        return await ener_agent.run(str(payload["description"]).strip(), _agent_triggered_by="agent")

    if tool_name == "create_content":
        topic = str(payload["topic"]).strip()
        platform = str(payload.get("platform", "general")).strip().lower() or "general"
        return await content_agent.run(
            f"{topic} platform: {platform}",
            _agent_triggered_by="agent",
        )

    return f"ไม่รู้จัก tool: {tool_name}"
