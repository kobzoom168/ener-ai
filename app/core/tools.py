from datetime import date
from pathlib import Path

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
    {
        "name": "read_github_file",
        "description": "อ่านไฟล์ code จาก GitHub repository ของกบ",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "ชื่อ repo เช่น ener-ai"},
                "path": {"type": "string", "description": "path ไฟล์ เช่น app/main.py"},
            },
            "required": ["repo", "path"],
        },
    },
    {
        "name": "list_github_repos",
        "description": "ดู repositories ทั้งหมดของกบบน GitHub",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "list_github_prs",
        "description": "ดู Pull Requests ที่เปิดอยู่",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "ชื่อ repo (optional)"},
            },
        },
    },
    {
        "name": "list_repo_files",
        "description": "ดูไฟล์และโฟลเดอร์ใน GitHub repository",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "path": {"type": "string", "description": "subfolder (optional)"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "draw_tarot",
        "description": "จั่วไพ่ทาโรต์ทำนายดวง ใช้เมื่อกบถามเรื่องดวง ไพ่ ทำนาย เสี่ยงทาย",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "คำถามหรือเรื่องที่อยากรู้"},
                "spread": {
                    "type": "string",
                    "enum": ["single", "three", "celtic"],
                    "description": "single=1ใบ three=3ใบ celtic=5ใบ",
                },
            },
        },
    },
    {
        "name": "draw_tarot_with_question",
        "description": "ซุ่มไพ่ทาโรต์ทำนาย ใช้เมื่อกบถามเรื่องดวง ไพ่ ทำนาย เสี่ยงทาย พลังงาน โชค",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "spread": {
                    "type": "string",
                    "enum": ["single", "three"],
                },
            },
        },
    },
    {
        "name": "make_maps_links",
        "description": (
            "สร้าง Google Maps link จริงจากชื่อร้านหรือสถานที่ที่รู้ "
            "ใช้เมื่อกบถามหาสถานที่และต้องการ link แนบ "
            "ห้ามสร้าง link เองโดยไม่ใช้ tool นี้"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "places": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "รายชื่อสถานที่ เช่น ['ร้านเหล้าแม่ค่า อุบลราชธานี', 'ร้านเหล้าพ่อพี อุบลราชธานี']",
                },
            },
            "required": ["places"],
        },
    },
    {
        "name": "search_web",
        "description": (
            "ค้นหาข้อมูลจากอินเทอร์เน็ตจริงๆ ด้วย Google Search "
            "ใช้เมื่อกบถามเรื่องที่ต้องการข้อมูลปัจจุบัน เช่น ร้านอาหาร "
            "สถานที่ ข่าว ราคา หรือข้อมูลที่ AI ไม่รู้แน่ชัด "
            "ได้ผลลัพธ์พร้อม link จริงจาก Google"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "คำค้นหาที่ต้องการค้นจากเว็บจริง"
                },
                "count": {
                    "type": "integer",
                    "description": "จำนวนผลลัพธ์ที่ต้องการ (ignored by Gemini grounding)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_system_info",
        "description": "ดูข้อมูลระบบ Ener-AI แบบ real-time: agent list, DB stats, scheduler, model ที่ใช้อยู่",
        "input_schema": {
            "type": "object",
            "properties": {
                "section": {
                    "type": "string",
                    "description": "ส่วนที่ต้องการ: all, agents, db, scheduler, files",
                    "enum": ["all", "agents", "db", "scheduler", "files"],
                }
            },
            "required": [],
        },
    },
    {
        "name": "read_code_file",
        "description": "อ่าน source code ของระบบ Ener-AI เพื่อวิเคราะห์หรือช่วย debug",
        "input_schema": {
            "type": "object",
            "properties": {
                "filepath": {
                    "type": "string",
                    "description": "path เช่น app/agents/chat.py, app/core/policy.py",
                },
                "lines": {
                    "type": "integer",
                    "description": "จำนวนบรรทัดที่ต้องการอ่าน (default 100)",
                },
            },
            "required": ["filepath"],
        },
    },
    {
        "name": "generate_cursor_prompt",
        "description": "สร้าง Cursor prompt พร้อมใช้งาน เมื่อกบต้องการเพิ่ม/แก้ feature ในระบบ",
        "input_schema": {
            "type": "object",
            "properties": {
                "feature_request": {
                    "type": "string",
                    "description": "สิ่งที่กบต้องการเพิ่มหรือแก้ไข",
                },
                "affected_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ไฟล์ที่ต้องแก้ไข เช่น ['app/main.py', 'app/agents/chat.py']",
                },
                "context": {
                    "type": "string",
                    "description": "บริบทเพิ่มเติม เช่น code เดิมที่เกี่ยวข้อง",
                },
            },
            "required": ["feature_request"],
        },
    },
]


def _make_maps_links(places: list[str]) -> str:
    """Generate real Google Maps search URLs from place names."""
    import urllib.parse

    if not places:
        return "ไม่มีชื่อสถานที่"
    lines = ["📍 Google Maps Links:\n"]
    for place in places:
        place = str(place).strip()
        if not place:
            continue
        encoded = urllib.parse.quote_plus(place)
        url = f"https://www.google.com/maps/search/{encoded}"
        lines.append(f"• {place}")
        lines.append(f"  🗺️ {url}\n")
    return "\n".join(lines)


async def execute_tool(tool_name: str, tool_input: dict) -> str:
    from app.agents import brainstorm, content_agent, ener_agent
    from app.agents.github_agent import list_prs, list_repo_files, list_repos, read_file
    from app.agents.tarot_agent import read_cards
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

    if tool_name == "read_github_file":
        return await read_file(
            str(payload["repo"]).strip(),
            str(payload["path"]).strip(),
            _agent_triggered_by="agent",
        )

    if tool_name == "list_github_repos":
        return await list_repos(_agent_triggered_by="agent")

    if tool_name == "list_github_prs":
        repo_name = str(payload.get("repo", "")).strip() or None
        return await list_prs(repo_name, _agent_triggered_by="agent")

    if tool_name == "list_repo_files":
        return await list_repo_files(
            str(payload["repo"]).strip(),
            str(payload.get("path", "")).strip(),
            _agent_triggered_by="agent",
        )

    if tool_name == "draw_tarot":
        return await read_cards(
            question=str(payload.get("question", "")).strip(),
            spread=str(payload.get("spread", "single")).strip() or "single",
            _agent_triggered_by="agent",
        )

    if tool_name == "draw_tarot_with_question":
        return await read_cards(
            question=str(payload.get("question", "")).strip(),
            spread=str(payload.get("spread", "single")).strip() or "single",
            _agent_triggered_by="agent",
        )

    if tool_name == "make_maps_links":
        places = payload.get("places", [])
        if isinstance(places, str):
            places = [places]
        return _make_maps_links(list(places))

    if tool_name == "search_web":
        from app.core.ai import _gemini_grounded_search

        query = str(payload.get("query", "")).strip()
        if not query:
            return "กรุณาระบุคำค้นหา"
        return await _gemini_grounded_search(query)

    if tool_name == "get_system_info":
        from app.core.ai import get_active_model, get_model_label
        from app.core.database import get_system_stats

        section = str(payload.get("section", "all")).strip().lower() or "all"
        stats = await get_system_stats()
        active_model = await get_active_model()
        model_label = get_model_label(active_model or "")
        agents_dir = Path(__file__).resolve().parent.parent / "agents"
        try:
            agent_files = sorted(
                file_path.stem
                for file_path in agents_dir.glob("*.py")
                if file_path.name != "__init__.py"
            )
        except Exception:
            agent_files = []

        model_info = f"Model: {model_label}" if section == "all" else ""
        arch_info = "Architecture: FastAPI + SQLite + Telegram + Web Workspace" if section == "all" else ""
        db_info = ""
        agents_info = ""
        sched_info = ""
        files_info = ""

        if section in {"all", "db"}:
            db_info = (
                f"Messages: {stats.get('messages', 0)} | "
                f"Notes: {stats.get('notes', 0)} | "
                f"Tasks: {stats.get('tasks', 0)} (open: {stats.get('open_tasks', 0)}) | "
                f"Memories: {stats.get('memories', 0)} | "
                f"Long-term: {stats.get('long_term_memories', 0)} | "
                f"AI Runs: {stats.get('ai_runs', 0)} | "
                f"Uploads: {stats.get('uploads', 0)}"
            )
        if section in {"all", "agents"}:
            agents_info = f"Agents ({len(agent_files)}): {', '.join(agent_files)}"
        if section in {"all", "scheduler"}:
            sched_info = (
                "Scheduler: 07:30 Standup | 08:00 News+Briefing | "
                "21:00 Digest+Session Log | จันทร์ 09:00 Weekly Review"
            )
        if section in {"all", "files"}:
            files_info = "\n".join(
                [
                    "Files:",
                    f"- app/agents/ -> {len(agent_files)} agents",
                    "- app/core/ -> ai.py, database.py, policy.py, tools.py, memory.py",
                    "- app/bot/router.py -> Telegram handlers",
                    "- app/main.py -> FastAPI routes + Web UI",
                    "- app/scheduler.py -> Cron jobs",
                ]
            )
        return "\n".join(filter(None, [model_info, arch_info, db_info, agents_info, sched_info, files_info]))

    if tool_name == "read_code_file":
        filepath = str(payload.get("filepath", "")).strip().replace("\\", "/")
        try:
            lines_limit = int(payload.get("lines", 100))
        except (TypeError, ValueError):
            lines_limit = 100
        lines_limit = max(1, min(lines_limit, 400))
        project_root = Path(__file__).resolve().parent.parent.parent
        app_root = (project_root / "app").resolve(strict=False)
        if not filepath or not filepath.startswith("app/"):
            return "❌ อนุญาตให้อ่านเฉพาะไฟล์ใน app/ เท่านั้น"
        full_path = (project_root / filepath).resolve(strict=False)
        try:
            full_path.relative_to(app_root)
        except ValueError:
            return "❌ ไม่อนุญาตให้อ่านไฟล์นอก app/ directory"
        try:
            content = full_path.read_text(encoding="utf-8").splitlines(keepends=True)
            total = len(content)
            preview = "".join(content[:lines_limit])
            return (
                f"📄 {filepath} ({total} บรรทัด, แสดง {min(lines_limit, total)} บรรทัด)\n\n"
                f"{preview}"
            )
        except FileNotFoundError:
            return f"❌ ไม่พบไฟล์ {filepath}"
        except Exception as exc:
            return f"❌ อ่านไม่ได้: {exc}"

    if tool_name == "generate_cursor_prompt":
        feature_request = str(payload.get("feature_request", "")).strip()
        affected_files = payload.get("affected_files", [])
        if not isinstance(affected_files, list):
            affected_files = [str(affected_files)]
        affected_files = [str(item).strip() for item in affected_files if str(item).strip()]
        context = str(payload.get("context", "")).strip()
        files_section = ""
        if affected_files:
            files_section = "\n\n## FILES TO MODIFY\n" + "\n".join(f"- {file_path}" for file_path in affected_files)
        context_section = f"\n\n## CONTEXT\n{context}" if context else ""
        prompt = f"""```cursor-prompt
{feature_request}{context_section}{files_section}

## RULES
- Maintain existing code style
- Do NOT break existing functionality
- Thai language for user-facing text
- No new dependencies unless necessary
```"""
        return f"📋 Cursor Prompt พร้อมใช้:\n\n{prompt}"

    return f"ไม่รู้จัก tool: {tool_name}"
