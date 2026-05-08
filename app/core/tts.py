import os
import tempfile
import edge_tts

VOICE_FEMALE_TH = "th-TH-PremwadeeNeural"
MAX_TTS_CHARS = 500


async def text_to_voice_bytes(text: str) -> bytes:
    tts_text = text[:MAX_TTS_CHARS]
    if len(text) > MAX_TTS_CHARS:
        tts_text += "... (อ่านต่อในข้อความด้านล่าง)"

    communicate = edge_tts.Communicate(
        text=tts_text,
        voice=VOICE_FEMALE_TH,
        rate="+0%",
        volume="+0%",
    )

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name

    try:
        await communicate.save(tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def is_voice_enabled(chat_id: str) -> bool:
    from app.core.database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM memories WHERE key=?",
            (f"voice_mode_{chat_id}",),
        )
        row = await cursor.fetchone()
        return row is not None and row["value"] == "on"


async def set_voice_mode(chat_id: str, enabled: bool) -> None:
    from app.core.database import get_db

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO memories (key, value, tag)
            VALUES (?, ?, 'voice')
            ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=CURRENT_TIMESTAMP
            """,
            (f"voice_mode_{chat_id}", "on" if enabled else "off"),
        )
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("voice_mode_updated", f"chat_id={chat_id} enabled={enabled}"),
        )
        await db.commit()
