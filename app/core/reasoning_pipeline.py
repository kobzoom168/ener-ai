import time

from app.core.ai import chat, chat_json, chat_with_tools
from app.core.event_log import log_event
from app.core.tools import TOOLS, execute_tool

ROUTER_PROMPT = """Classify this message. Reply JSON only:
{
  "complexity": "simple|complex|critical",
  "domain": "chat|analysis|code|location|spiritual|task",
  "needs_thinking": true/false,
  "reason": "one line why"
}

Rules:
- simple: greetings, short questions, task CRUD, memory save
- complex: analysis, comparison, planning, strategy, "ช่วยคิด", "แนะนำ", "วิเคราะห์", "ควรทำ"
- critical: medical, legal, financial decisions, hospital IT decisions
- needs_thinking: true if complex or critical"""

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


async def route_message(text: str) -> dict:
    try:
        result = await chat_json(
            f"Message: {text}",
            system=ROUTER_PROMPT,
            agent="Router",
            preferred_model="groq",
            strict_model=True,
        )
        return {
            "complexity": str(result.get("complexity", "simple") or "simple"),
            "domain": str(result.get("domain", "chat") or "chat"),
            "needs_thinking": bool(result.get("needs_thinking", False)),
            "reason": str(result.get("reason", "") or ""),
        }
    except Exception:
        return {
            "complexity": "simple",
            "domain": "chat",
            "needs_thinking": False,
            "reason": "router failed",
        }


async def reason(
    text: str,
    history: list[dict],
    system_prompt: str,
    route: dict,
) -> str:
    complexity = route.get("complexity", "simple")
    needs_thinking = route.get("needs_thinking", False)

    if complexity == "critical":
        model = "haiku"
    elif needs_thinking:
        model = "deepseek"
    else:
        model = "groq"

    enhanced_system = system_prompt
    if needs_thinking:
        enhanced_system += """

=== Reasoning Mode ===
คำถามนี้ต้องการการวิเคราะห์อย่างละเอียด
กรุณา:
1. คิดทีละขั้นตอน (step by step)
2. พิจารณาหลายมุมมอง
3. สรุปคำตอบที่ชัดเจนในตอนท้าย
อย่ารีบตอบ - ความแม่นยำสำคัญกว่าความเร็ว"""

    try:
        response = await chat_with_tools(
            prompt=text,
            system=enhanced_system,
            messages=history,
            tools=TOOLS,
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
    if route.get("complexity") == "simple" and len(answer) < 200:
        return answer

    try:
        result = await chat_json(
            f"คำถาม: {original_question}\n\nคำตอบ: {answer}",
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
    route = await route_message(text)
    router_ms = int((time.time() - t1) * 1000)

    t2 = time.time()
    raw_answer = await reason(text, history, system_prompt, route)
    reasoner_ms = int((time.time() - t2) * 1000)

    t3 = time.time()
    final_answer = await check_answer(text, raw_answer, route)
    checker_ms = int((time.time() - t3) * 1000)
    total_ms = int((time.time() - total_start) * 1000)

    model_used = (
        "haiku"
        if route.get("complexity") == "critical"
        else "deepseek-r1"
        if route.get("needs_thinking")
        else "groq"
    )

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
