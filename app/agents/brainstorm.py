import asyncio
import json as _json

from app.core.ai import chat, chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import get_agent_context, log_event
from app.core.policy import build_system_prompt
from app.agents import memory as memory_agent


# ── AI Council: multi-MODEL debate via OpenRouter (single bill) → buildable spec ──
# Each seat is a genuinely different model so the debate has real diverse lenses.
_COUNCIL_SEATS = [
    {"key": "strategist", "emoji": "🧠", "name": "Strategist", "model": "anthropic/claude-opus-4.8",
     "lens": "วิสัยทัศน์ ขอบเขต จุดต่าง โอกาสตลาด — ไอเดียนี้ใหญ่พอ/คมพอไหม ควรเล็งอะไร"},
    {"key": "skeptic", "emoji": "😈", "name": "Skeptic", "model": "openai/gpt-5.5",
     "lens": "ความเสี่ยง จุดตาย เหตุผลที่จะเจ๊ง คู่แข่ง ข้อจำกัดจริง — แย้งให้เจ็บแต่สร้างสรรค์"},
    {"key": "builder", "emoji": "🔧", "name": "Builder", "model": "deepseek/deepseek-v4-pro",
     "lens": "ทำได้จริงไหม MVP เล็กสุดที่พิสูจน์คุณค่า tech stack ความซับซ้อน เวลา"},
    {"key": "ux", "emoji": "🎨", "name": "UX", "model": "google/gemini-3.5-flash",
     "lens": "ใครใช้ ใช้ตอนไหน ทำให้ใช้ง่าย/อยากใช้ยังไง ทิศทางดีไซน์/หน้าตา"},
]
_RESEARCH_MODEL = "anthropic/claude-sonnet-4.6"
_SYNTH_MODEL = "anthropic/claude-opus-4.8"
_COUNCIL_ROUNDS = 2


async def _or_chat(model: str, system: str, prompt: str, max_tokens: int = 800) -> str:
    """One OpenRouter chat call. Returns '' on failure (fail-open)."""
    from app.core.openrouter_client import openrouter_chat_completions
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    try:
        data = await openrouter_chat_completions(model, msgs, max_tokens=max_tokens)
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except Exception:
        return ""


def _council_parse_json(raw: str) -> dict:
    import re as _re
    if not raw or not raw.strip():
        return {}
    txt = _re.sub(r"```(?:json)?", "", raw, flags=_re.IGNORECASE).replace("```", "").strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s != -1 and e > s:
        txt = txt[s:e + 1]
    try:
        d = _json.loads(txt)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def run_council(topic: str) -> dict:
    """Multi-model council: research → 4-seat debate (2 rounds, parallel) → spec.

    Returns a structured dict for the UI: {topic, research, rounds, spec}.
    All calls go through OpenRouter (single bill). Fail-open per call.
    """
    research = await _or_chat(
        _RESEARCH_MODEL,
        "คุณคือนักวิจัยไอเดีย ขยายไอเดียดิบให้เป็น brief สั้นกระชับเป็นภาษาไทย ตอบเป็น bullet",
        f"ไอเดีย: {topic}\n\nเขียน brief สั้นๆ: ปัญหาที่แก้ / กลุ่มผู้ใช้ / ฟีเจอร์หลัก 3-5 / "
        f"ของใกล้เคียงหรือคู่แข่ง / ความเสี่ยงหลัก",
        max_tokens=700,
    )

    rounds: list[dict] = []
    prev = ""
    for rnd in range(1, _COUNCIL_ROUNDS + 1):
        async def _seat_call(seat: dict, _rnd: int = rnd, _prev: str = prev) -> dict:
            system = (
                f"คุณคือ '{seat['name']}' ในวง brainstorm หลายโมเดล. มุมที่คุณรับผิดชอบ: {seat['lens']}. "
                f"ตอบสั้น คม ตรงประเด็น เป็นภาษาไทย เป็น bullet 3-6 ข้อ ไม่ต้องเกริ่น"
            )
            prompt = (
                f"ไอเดีย: {topic}\n\nBrief:\n{research}\n\n"
                + (f"ความเห็นรอบก่อนของทุกที่นั่ง:\n{_prev}\n\n" if _prev else "")
                + f"รอบที่ {_rnd}: ให้มุมของคุณ ({seat['name']}) — ต่อยอดหรือแย้งความเห็นคนอื่นอย่างเจาะจง "
                f"และชี้สิ่งที่ควรปรับให้ไอเดียดีขึ้น"
            )
            text = await _or_chat(seat["model"], system, prompt, max_tokens=650)
            return {"key": seat["key"], "name": seat["name"], "emoji": seat["emoji"],
                    "model": seat["model"], "text": text or "(ไม่มีคำตอบ)"}

        seats = await asyncio.gather(*[_seat_call(s) for s in _COUNCIL_SEATS])
        rounds.append({"round": rnd, "seats": list(seats)})
        prev = "\n\n".join(f"{s['emoji']} {s['name']}:\n{s['text']}" for s in seats)

    debate = ""
    for rd in rounds:
        debate += f"\n=== รอบ {rd['round']} ===\n" + "\n\n".join(
            f"{s['emoji']} {s['name']}:\n{s['text']}" for s in rd["seats"]
        )
    spec_raw = await _or_chat(
        _SYNTH_MODEL,
        "คุณคือ product lead สังเคราะห์ผลวง brainstorm เป็น project spec ที่ลงมือสร้างได้จริง. ตอบ JSON เท่านั้น",
        f"ไอเดีย: {topic}\n\nBrief:\n{research}\n\nวง debate:\n{debate}\n\n"
        "สรุปเป็น project spec ที่ดีที่สุด ตอบ JSON เท่านั้นรูปแบบนี้:\n"
        '{"name":"ชื่อโปรเจกต์สั้นๆ", "one_liner":"อธิบาย 1 ประโยค", "users":"ใครใช้", '
        '"features":["ฟีเจอร์ MVP 3-6 ข้อ"], "tech":"stack ที่แนะนำ", "ui":"ทิศทาง UI/หน้าตา", '
        '"cut":["สิ่งที่ตัดทิ้งใน v1"], "confidence":"go|maybe|risky"}',
        max_tokens=1200,
    )
    spec = _council_parse_json(spec_raw)

    try:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                ("council_completed", f"topic={topic[:120]}"),
            )
            await db.commit()
    except Exception:
        pass

    return {"topic": topic, "research": research, "rounds": rounds, "spec": spec}

_AI_A_SYSTEM = build_system_prompt("บทบาทรอบนี้: ช่วยกบวิจารณ์ไอเดียแบบตรงไปตรงมา หาให้ครบทั้งจุดอ่อน ความเสี่ยง และข้อที่ยังไม่ชัด")
_AI_B_SYSTEM = build_system_prompt("บทบาทรอบนี้: ช่วยกบแก้ปัญหาจากข้อวิจารณ์ของ AI_A ตอบทุกจุดและเสนอทางแก้ที่ทำได้จริง")
_AI_C_SYSTEM = build_system_prompt("บทบาทรอบนี้: ช่วยกบสรุปข้อถกเถียงของ A กับ B อย่างเป็นกลาง ชี้ทั้งจุดแข็ง จุดเสี่ยง และสิ่งที่ควรไปต่อ")
_FINAL_SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: ตัดสินผลรวมจากวง brainstorm แล้วตอบ JSON เท่านั้นในรูปแบบนี้:
{
  "summary": "สรุปภาพรวมสั้นๆ ภาษาไทย",
  "verdict": "go|pivot|stop",
  "reason": "เหตุผลสั้นๆ ภาษาไทย"
}

กฎ:
- verdict ต้องเป็น go หรือ pivot หรือ stop เท่านั้น
- summary และ reason ต้องเป็นภาษาไทย""")


@log_agent_run("ThinkTeam")
async def run_brainstorm(topic: str) -> str:
    agent_memory = await get_agent_context("ThinkTeam", ["brainstorm", "think"])
    history = []
    context = f"หัวข้อเริ่มต้น: {topic}"
    if agent_memory:
        context = agent_memory + "\n\n" + context

    try:
        for round_number in range(1, 4):
            prompt_a = (
                f"หัวข้อ: {topic}\n"
                f"บริบทรอบก่อนหน้า:\n{context}\n\n"
                f"นี่คือรอบที่ {round_number}\n"
                "โจมตีไอเดียนี้แบบตรงไปตรงมาและเป็นภาษาไทย"
            )
            ai_a = (await chat(prompt_a, system=_AI_A_SYSTEM, agent="brainstorm")).strip()

            prompt_b = (
                f"หัวข้อ: {topic}\n"
                f"บริบทรอบก่อนหน้า:\n{context}\n\n"
                f"ข้อโจมตีจาก AI_A รอบ {round_number}:\n{ai_a}\n\n"
                "ตอบโต้ทุกประเด็นและเสนอทางแก้เป็นภาษาไทย"
            )
            ai_b = (await chat(prompt_b, system=_AI_B_SYSTEM, agent="brainstorm")).strip()

            prompt_c = (
                f"หัวข้อ: {topic}\n"
                f"ข้อโจมตีจาก AI_A รอบ {round_number}:\n{ai_a}\n\n"
                f"คำตอบจาก AI_B รอบ {round_number}:\n{ai_b}\n\n"
                "สรุปอย่างเป็นกลางว่ารอบนี้เห็นอะไร จุดไหนยังเสี่ยง จุดไหนน่าไปต่อ"
            )
            ai_c = (await chat(prompt_c, system=_AI_C_SYSTEM, agent="brainstorm")).strip()

            history.append(
                {
                    "round": round_number,
                    "ai_a": ai_a,
                    "ai_b": ai_b,
                    "ai_c": ai_c,
                }
            )
            context = (
                f"หัวข้อ: {topic}\n"
                + "\n\n".join(
                    [
                        f"รอบ {item['round']}\nAI_A:\n{item['ai_a']}\n\nAI_B:\n{item['ai_b']}\n\nAI_C:\n{item['ai_c']}"
                        for item in history
                    ]
                )
            )

        final_prompt = (
            f"หัวข้อ brainstorm: {topic}\n\n"
            + "\n\n".join(
                [
                    f"รอบ {item['round']}\nAI_A:\n{item['ai_a']}\n\nAI_B:\n{item['ai_b']}\n\nAI_C:\n{item['ai_c']}"
                    for item in history
                ]
            )
            + "\n\nตัดสินผลรวมทั้งหมด"
        )
        final_result = await chat_json(final_prompt, system=_FINAL_SYSTEM, agent="brainstorm")
    except Exception as exc:
        try:
            await log_event(
                agent_name="ThinkTeam",
                event_type="task_failed",
                summary=f"brainstorm fail: {topic[:80]}",
                tags=["brainstorm", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise
    summary = str(final_result.get("summary", "สรุปไอเดียเสร็จแล้ว")).strip()
    verdict = str(final_result.get("verdict", "pivot")).strip().lower()
    reason = str(final_result.get("reason", "")).strip()

    async with get_db() as db:
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("brainstorm_completed", f"topic={topic} verdict={verdict}"),
        )
        await db.commit()

    await memory_agent.remember_memory(
        f"brainstorm เรื่อง {topic} verdict: {verdict}",
        _agent_triggered_by="agent",
    )

    verdict_text = {
        "go": "GO",
        "pivot": "PIVOT",
        "stop": "STOP",
    }.get(verdict, "PIVOT")

    lines = [f"📌 brainstorm เสร็จแล้ว: {summary}", ""]
    for item in history:
        lines.extend(
            [
                f"รอบ {item['round']}",
                f"AI_A: {item['ai_a']}",
                f"AI_B: {item['ai_b']}",
                f"AI_C: {item['ai_c']}",
                "",
            ]
        )
    lines.append(f"🎯 คำตัดสิน: {verdict_text}")
    if reason:
        lines.append(f"เหตุผล: {reason}")
    result_text = "\n".join(lines)
    try:
        await log_event(
            agent_name="ThinkTeam",
            event_type="decision",
            summary=f"brainstorm {topic[:80]} → {verdict}",
            tags=["brainstorm", "think"],
            context=summary,
            result="success",
            learned=reason or None,
        )
    except Exception:
        pass
    return result_text
