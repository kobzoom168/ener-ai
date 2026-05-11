import re
import time

from app.core.ai import chat, chat_json, chat_with_tools
from app.core.event_log import log_event
from app.core.tools import TOOLS, execute_tool

CHECKER_PROMPT = """ตรวจคำตอบนี้ก่อนส่งให้ user ตอบ JSON เท่านั้น:
{
  "ok": true/false,
  "issues": ["issue1", "issue2"],
  "fixed_answer": "คำตอบที่แก้แล้ว หรือ null ถ้า ok=true"
}

สิ่งที่ต้องตรวจ:
1. URL ปลอม - ถ้ามี URL ที่ไม่ใช่จาก tool จริง -> ลบออก
2. ชื่อร้าน/เบอร์โทรที่ AI แต่งขึ้น - ลบออก บอกว่าไม่รู้จริงๆ
3. ผิด instruction - เช่น ใช้คำว่า "ฉัน" แทน "พี่"
4. ข้อมูลที่ฟังดู confident แต่น่าสงสัย - เพิ่ม disclaimer
5. ถ้าทุกอย่างโอเค -> ok: true, fixed_answer: null"""


def route_fast(text: str) -> dict:
    """Python keyword router — 0-5ms, no LLM call.
    Routes to optimal model per task based on cost/quality balance.
    """
    t = text.lower()

    # Location / current info → Gemini (free, grounded)
    if any(k in t for k in [
        "หาร้าน", "ที่ไหน", "ใกล้", "พิกัด", "เบอร์โทร", "ราคา",
        "ข่าวล่าสุด", "แผนที่", "restaurant", "where",
        "เวียงจันทร์", "กรุงเทพ", "เชียงใหม่", "ปทุม",
    ]):
        return {
            "complexity": "grounded",
            "domain": "location",
            "model": "gemini",
            "tools": ["make_maps_links", "search_memory"],
            "needs_check": True,
            "reason": "location/current info",
        }

    # Task / note / memory → Groq (free + fast)
    if any(k in t for k in [
        "จด", "จำไว้", "todo", "task", "เพิ่มงาน", "บันทึก",
        "/task", "/note", "/remember", "save this", "remind",
    ]):
        return {
            "complexity": "simple",
            "domain": "task",
            "model": "groq",
            "tools": ["save_task", "save_note", "remember_fact"],
            "needs_check": False,
            "reason": "task/note direct",
        }

    # Memory search → Groq (free + fast)
    if any(k in t for k in [
        "จำได้ไหม", "เมื่อก่อน", "บอกว่า", "ค้นหา", "ลืม",
        "/memory", "/search", "เคยบอก", "เคยคุย",
    ]):
        return {
            "complexity": "simple",
            "domain": "chat",
            "model": "groq",
            "tools": ["search_memory", "remember_fact"],
            "needs_check": False,
            "reason": "memory recall",
        }

    # Tarot / spiritual → Haiku (Thai best)
    if any(k in t for k in [
        "ดวง", "ไพ่", "ทาโรต์", "ทำนาย", "โชค", "เสี่ยง",
        "tarot", "fortune", "ดูดวง",
    ]):
        return {
            "complexity": "simple",
            "domain": "spiritual",
            "model": "haiku",
            "tools": ["draw_tarot", "draw_tarot_with_question"],
            "needs_check": False,
            "reason": "tarot/spiritual",
        }

    # Ener Scan content → Haiku (best Thai + creative)
    if any(k in t for k in [
        "พระ", "เครื่อง", "พลังงาน", "หลวงปู่", "สมเด็จ", "บูชา",
        "ener scan", "caption", "tiktok", "youtube", "content",
        "script", "โพสต์", "ขายพระ", "วัตถุมงคล",
    ]):
        return {
            "complexity": "complex",
            "domain": "analysis",
            "model": "haiku",
            "tools": ["analyze_amulet", "create_content",
                      "draw_tarot_with_question", "search_memory"],
            "needs_check": False,
            "reason": "ener scan / amulet / content",
        }

    # Code / GitHub → Groq (free + fast enough)
    if any(k in t for k in [
        "code", "github", "bug", "error", "โปรแกรม", "function",
        "deploy", "script", "dockerfile", "python", "api", "cursor",
        "แก้โค้ด", "debug",
    ]):
        return {
            "complexity": "complex",
            "domain": "code",
            "model": "groq",
            "tools": ["read_github_file", "list_github_repos",
                      "list_github_prs", "read_code_file",
                      "generate_cursor_prompt"],
            "needs_check": False,
            "reason": "code/technical",
        }

    # Hospital IT / vendor analysis → DeepSeek (cheap + good reasoning)
    if any(k in t for k in [
        "วิเคราะห์", "เปรียบเทียบ", "vendor", "proposal", "risk",
        "ควรเลือก", "his", "server", "network", "backup",
        "pbx", "crm", "infrastructure", "procurement", "tor", "boq", "sow",
    ]):
        return {
            "complexity": "critical",
            "domain": "analysis",
            "model": "deepseek-direct",
            "tools": ["search_memory", "run_brainstorm", "get_system_info"],
            "needs_check": True,
            "reason": "hospital IT / vendor analysis",
        }

    # Draft email / important writing → Haiku (best Thai writing)
    if any(k in t for k in [
        "draft", "email", "จดหมาย", "เขียน", "รายงาน", "สรุป",
        "ผู้บริหาร", "formal", "ทางการ", "เสนอ", "proposal",
    ]):
        return {
            "complexity": "complex",
            "domain": "writing",
            "model": "haiku",
            "tools": ["search_memory"],
            "needs_check": False,
            "reason": "formal writing / email",
        }

    # Critical decisions → Sonnet (best quality)
    if any(k in t for k in [
        "สำคัญมาก", "ตัดสินใจ", "critical", "ควรทำอะไร",
        "ช่วยคิดสำคัญ", "strategy ใหญ่", "long term",
    ]):
        return {
            "complexity": "critical",
            "domain": "analysis",
            "model": "sonnet",
            "tools": ["search_memory", "run_brainstorm"],
            "needs_check": True,
            "reason": "critical decision",
        }

    # Brainstorm / planning → DeepSeek (cheap + good reasoning)
    if any(k in t for k in [
        "brainstorm", "ช่วยคิด", "แผน", "strategy", "แนะนำ",
        "ควรทำ", "pros cons", "ข้อดี", "ข้อเสีย", "คิดให้",
    ]):
        return {
            "complexity": "complex",
            "domain": "analysis",
            "model": "deepseek-direct",
            "tools": ["run_brainstorm", "search_memory"],
            "needs_check": False,
            "reason": "brainstorm/planning",
        }

    # System info → Groq (free)
    if any(k in t for k in [
        "ระบบมี", "agent กี่", "cursor prompt", "generate prompt",
        "read code", "อ่านโค้ด", "ระบบตัวเอง",
    ]):
        return {
            "complexity": "simple",
            "domain": "code",
            "model": "groq",
            "tools": ["get_system_info", "read_code_file",
                      "generate_cursor_prompt"],
            "needs_check": False,
            "reason": "system introspection",
        }

    # Default: simple chat → Groq (free + fastest)
    return {
        "complexity": "simple",
        "domain": "chat",
        "model": "groq",
        "tools": ["save_task", "save_note",
                  "remember_fact", "search_memory"],
        "needs_check": False,
        "reason": "default simple chat",
    }


def _select_tools(tool_names: list[str]) -> list[dict]:
    """Return only the tools needed for this intent."""
    return [tool for tool in TOOLS if tool["name"] in tool_names]


def _deterministic_check(answer: str) -> list[str]:
    """Fast regex-based check. No LLM call."""
    issues = []

    fake_internal = re.findall(
        r"https?://my-ener\.uk/(?!admin|workspace|health|webhook)\S+",
        answer,
    )
    if fake_internal:
        issues.append(f"fake_internal_url: {fake_internal}")

    phones = re.findall(r"0\d{1,2}[-\s]?\d{3}[-\s]?\d{4}", answer)
    if phones:
        issues.append(f"unverified_phone: {phones}")

    if re.search(r"\bVendor\s+[A-C]\b", answer, re.IGNORECASE):
        issues.append("generic_vendor_names")

    return issues


async def reason(
    text: str,
    history: list[dict],
    system_prompt: str,
    route: dict,
) -> str:
    from app.core.context_builder import build_context

    complexity = route.get("complexity", "simple")
    model = route.get("model", "groq")
    tool_names = route.get("tools", [])
    selected_tools = _select_tools(tool_names)

    grounded = await build_context(text, route)
    enhanced_system = system_prompt + grounded
    if complexity in ("complex", "critical"):
        enhanced_system += """

=== Reasoning Mode ===
คิดทีละขั้นตอน พิจารณาหลายมุมมอง สรุปชัดเจน
ความแม่นยำสำคัญกว่าความเร็ว"""

    try:
        response = await chat_with_tools(
            prompt=text,
            system=enhanced_system,
            messages=history,
            tools=selected_tools,
            agent="Reasoner",
            preferred_model=model,
        )
        reply = str(response.get("text", "")).strip()

        tool_results = []
        for tool_call in response.get("tool_calls") or []:
            tool_name = str(tool_call.get("name", "")).strip()
            tool_input = tool_call.get("input", {}) or {}
            if not tool_name:
                continue
            try:
                result = await execute_tool(tool_name, tool_input)
                tool_results.append(f"✅ {result}")
            except Exception as exc:
                tool_results.append(f"⚠️ {tool_name}: {exc}")

        if tool_results:
            reply = reply + "\n\n" + "\n".join(tool_results) if reply else "\n".join(tool_results)

        return reply or "ยังไม่มีคำตอบตอนนี้"
    except Exception:
        return await chat(
            text,
            system=enhanced_system,
            agent="ReasonerFallback",
            messages=history,
            preferred_model="groq",
        )


async def check_answer(
    original_question: str,
    answer: str,
    route: dict,
) -> str:
    if not route.get("needs_check", False):
        return answer

    if len(answer) < 300:
        return answer

    issues = _deterministic_check(answer)
    if not issues:
        return answer

    try:
        result = await chat_json(
            f"คำถาม: {original_question}\n\nคำตอบ: {answer}\n\nPotential issues: {issues}",
            system=CHECKER_PROMPT,
            agent="Checker",
            preferred_model="groq",
            strict_model=True,
        )

        ok = bool(result.get("ok", True))
        issues = result.get("issues", []) or []
        fixed = result.get("fixed_answer")

        if ok or not fixed:
            return answer

        try:
            await log_event(
                agent_name="Checker",
                event_type="insight",
                summary=f"แก้คำตอบ: {', '.join(str(issue) for issue in issues[:2])}",
                tags=["checker", "quality"],
                result="success",
            )
        except Exception:
            pass

        return str(fixed)
    except Exception:
        return answer


async def _save_pipeline_metric(meta: dict, question_preview: str) -> None:
    try:
        from app.core.database import get_db

        async with get_db() as db:
            await db.execute(
                """
                INSERT INTO pipeline_metrics
                    (complexity, domain, model_used, router_ms, reasoner_ms,
                     checker_ms, total_ms, was_fixed, question_preview)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta.get("complexity", "simple"),
                    meta.get("domain", "chat"),
                    meta.get("model_used", "groq"),
                    int(meta.get("router_ms", 0) or 0),
                    int(meta.get("reasoner_ms", 0) or 0),
                    int(meta.get("checker_ms", 0) or 0),
                    int(meta.get("total_ms", 0) or 0),
                    int(bool(meta.get("was_fixed", False))),
                    str(question_preview or "")[:100],
                ),
            )
            await db.commit()
    except Exception:
        pass


async def run_pipeline(
    text: str,
    history: list[dict],
    system_prompt: str,
) -> tuple[str, dict]:
    total_start = time.time()

    t1 = time.time()
    route = route_fast(text)
    router_ms = int((time.time() - t1) * 1000)

    t2 = time.time()
    raw_answer = await reason(text, history, system_prompt, route)
    reasoner_ms = int((time.time() - t2) * 1000)

    t3 = time.time()
    final_answer = await check_answer(text, raw_answer, route)
    checker_ms = int((time.time() - t3) * 1000)
    total_ms = int((time.time() - total_start) * 1000)

    model_used = str(route.get("model", "groq") or "groq")

    metadata = {
        "complexity": route.get("complexity", "simple"),
        "domain": route.get("domain", "chat"),
        "model_used": model_used,
        "was_fixed": final_answer != raw_answer,
        "router_ms": router_ms,
        "reasoner_ms": reasoner_ms,
        "checker_ms": checker_ms,
        "total_ms": total_ms,
        "elapsed_ms": total_ms,
    }

    await _save_pipeline_metric(metadata, text[:100])

    return final_answer, metadata
