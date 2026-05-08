from app.core.agents import log_agent_run
from app.core.tts import is_voice_enabled, set_voice_mode


@log_agent_run("VoiceAgent")
async def handle_voice_command(chat_id: str, raw_args: str) -> str:
    command = (raw_args or "").strip().lower()

    if not command:
        enabled = await is_voice_enabled(chat_id)
        status = "🔊 เปิดอยู่" if enabled else "🔇 ปิดอยู่"
        return (
            f"📌 Voice Mode: {status}\n\n"
            f"🔊 /voice on  — เปิด (ส่งเสียง + ข้อความ)\n"
            f"🔇 /voice off — ปิด (ข้อความอย่างเดียว)\n\n"
            f"เสียง: ภาษาไทย หญิง (PremwadeeNeural)"
        )

    if command == "on":
        await set_voice_mode(chat_id, True)
        return "🔊 เปิด Voice Mode แล้วครับ\nAI จะส่งเสียงภาษาไทยหญิง + ข้อความเต็มพร้อมกัน"

    if command == "off":
        await set_voice_mode(chat_id, False)
        return "🔇 ปิด Voice Mode แล้วครับ\nกลับเป็นข้อความปกติ"

    return "📌 พิมพ์ /voice on หรือ /voice off ครับ"
