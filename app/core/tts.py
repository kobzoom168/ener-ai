import asyncio
import io
import os
import subprocess
import tempfile

from gtts import gTTS

MAX_TTS_CHARS = 500
SPEECH_SPEED = 1.3


def _build_tts_text(text: str) -> str:
    tts_text = text[:MAX_TTS_CHARS]
    if len(text) > MAX_TTS_CHARS:
        tts_text += "... อ่านต่อในข้อความด้านล่าง"
    return tts_text


def _generate_mp3_bytes(tts_text: str) -> bytes:
    tts = gTTS(text=tts_text, lang="th", slow=False)
    mp3_buffer = io.BytesIO()
    tts.write_to_fp(mp3_buffer)
    return mp3_buffer.getvalue()


async def text_to_audio_bytes(text: str) -> bytes:
    tts_text = _build_tts_text(text)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _generate_mp3_bytes(tts_text))


async def text_to_voice_bytes(text: str) -> bytes:
    """Generate Thai TTS -> ogg/opus for Telegram voice message."""
    tts_text = _build_tts_text(text)

    def _generate():
        mp3_bytes = _generate_mp3_bytes(tts_text)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3_file:
            mp3_file.write(mp3_bytes)
            mp3_path = mp3_file.name

        ogg_path = mp3_path.replace(".mp3", ".ogg")

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    mp3_path,
                    "-filter:a",
                    f"atempo={SPEECH_SPEED}",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "64k",
                    ogg_path,
                ],
                capture_output=True,
                timeout=30,
                check=True,
            )
            with open(ogg_path, "rb") as file_obj:
                return file_obj.read()
        finally:
            os.unlink(mp3_path)
            if os.path.exists(ogg_path):
                os.unlink(ogg_path)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate)


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
