import base64

import anthropic

from app.core.agents import log_agent_run
from app.core.config import settings
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

VISION_SYSTEM = build_system_prompt("""
คุณเป็น Vision Agent ของ Ener-AI
วิเคราะห์รูปภาพและตอบเป็นภาษาไทย

ถ้าเห็นพระเครื่อง/เครื่องราง:
- บอกชื่อ วัด อาจารย์ ปี (ถ้ารู้)
- จุดเด่น ความหายาก
- ราคาตลาดโดยประมาณ
- พลังงานและความเชื่อ

ถ้าเห็น code/screen/error:
- อ่าน error message
- บอกสาเหตุและวิธีแก้

ถ้าเห็นเอกสาร/ข้อความ:
- สรุปเนื้อหาสำคัญ

ถ้าเห็นอย่างอื่น:
- อธิบายว่าเห็นอะไร ใช้ประโยชน์อะไรได้
""")


async def _log_vision_event(
    event_type: str,
    summary: str,
    tags: list[str],
    result: str,
    learned: str | None = None,
) -> None:
    try:
        await log_event(
            agent_name="VisionAgent",
            event_type=event_type,
            summary=summary,
            tags=tags,
            result=result,
            learned=learned,
        )
    except Exception:
        pass


def _extract_anthropic_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", "")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


async def _save_vision_messages(chat_id: str, prompt: str, result: str) -> None:
    from app.core.database import get_db

    async with get_db() as db:
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "user", f"[ส่งรูป] {prompt or 'วิเคราะห์รูปนี้'}"),
        )
        await db.execute(
            "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
            (chat_id, "assistant", result),
        )
        await db.commit()


async def _analyze_with_haiku(image_bytes: bytes, prompt: str) -> str:
    if not settings.anthropic_api_key:
        return "ไม่มี API key สำหรับวิเคราะห์รูปครับ"

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    image_b64 = base64.b64encode(image_bytes).decode()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=VISION_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": prompt or "วิเคราะห์รูปนี้เป็นภาษาไทย",
                    },
                ],
            }
        ],
    )
    return _extract_anthropic_text(response) or "ไม่สามารถวิเคราะห์รูปได้"


@log_agent_run("VisionAgent")
async def analyze_image(image_bytes: bytes, prompt: str = "", chat_id: str = "") -> str:
    user_prompt = prompt or "วิเคราะห์รูปนี้ให้ละเอียด"
    try:
        result = await _analyze_with_haiku(image_bytes, user_prompt)
        if chat_id:
            await _save_vision_messages(chat_id, prompt, result)
        await _log_vision_event(
            event_type="task_done",
            summary=f"วิเคราะห์รูป: {user_prompt[:50]}",
            tags=["vision", "image", "haiku"],
            result="success",
        )
        return result
    except Exception as exc:
        await _log_vision_event(
            event_type="task_failed",
            summary=f"วิเคราะห์รูปไม่ได้: {str(exc)[:100]}",
            tags=["vision", "error"],
            result="failure",
            learned=str(exc)[:200],
        )
        return f"วิเคราะห์รูปไม่ได้ครับ: {exc}"
