import hashlib
import io
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from app.agents import log_keeper
from app.agents.monitor_agent import cmd_errors, cmd_logs, cmd_server, cmd_status
from app.core.config import settings
from app.core.policy import ALLOWED_CHAT_IDS
from app.core.tts import text_to_voice_bytes
from app.agents.main_agent import MAIN_AGENT

logger = logging.getLogger(__name__)


async def _reply(update: Update, text: str):
    await _reply_smart(update, text)


async def _cache_tts_text(chat_id: str, text_hash: str, text: str):
    from app.core.database import get_db

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO memories (key, value, tag)
            VALUES (?, ?, 'tts_cache')
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                tag = 'tts_cache',
                updated_at = CURRENT_TIMESTAMP
            """,
            (f"tts_{chat_id}_{text_hash}", text[:500]),
        )
        await db.commit()


async def _get_cached_tts_text(chat_id: str, text_hash: str) -> str | None:
    from app.core.database import get_db

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM memories WHERE key = ?",
            (f"tts_{chat_id}_{text_hash}",),
        )
        row = await cursor.fetchone()
    return row["value"] if row else None


async def _reply_smart(update: Update, text: str):
    text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔊 ฟัง", callback_data=f"tts:{text_hash}")]]
    )
    chat_id = str(update.effective_chat.id)
    await _cache_tts_text(chat_id, text_hash, text)
    await update.message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode=None,
    )


async def handle_tts_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not query or not query.data or not query.data.startswith("tts:"):
        return

    text_hash = query.data.split(":", 1)[1]
    chat_id = str(query.message.chat.id)
    text = await _get_cached_tts_text(chat_id, text_hash)
    if not text:
        await query.answer("หมดอายุแล้ว ขอใหม่ได้เลย", show_alert=True)
        return

    try:
        audio_bytes = await text_to_voice_bytes(text)
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "reply.mp3"
        await query.message.reply_audio(audio=audio_file, title="Ener-AI")
    except Exception as e:
        logger.error(f"TTS callback error: {e}", exc_info=True)
        try:
            await query.answer("ส่งเสียงไม่ได้ตอนนี้", show_alert=True)
        except Exception:
            pass


def _is_allowed(update: Update) -> bool:
    chat_id = str(update.effective_chat.id)
    return chat_id in [str(x) for x in ALLOWED_CHAT_IDS]


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /note เช่น /note ไอเดีย X")
        return
    chat_id = str(update.effective_chat.id)
    result = await MAIN_AGENT.handle("note", text, chat_id)
    await _reply_smart(update, result)


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not ctx.args:
        await _reply(update, "📌 พิมพ์ชื่อ task เช่น /task ซื้อของ")
        return
    raw = " ".join(ctx.args)
    result = await MAIN_AGENT.handle("task", raw, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("tasks", "", str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await _reply(update, "📌 ระบุ ID task เช่น /done 3")
        return
    result = await MAIN_AGENT.handle("done", ctx.args[0], str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_learn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /learn เช่น /learn พลาดเรื่อง X เพราะ Y")
        return
    result = await MAIN_AGENT.handle("learn", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์หัวข้อหลัง /think เช่น /think ทำโปรดักต์ AI ตัวนี้ดีไหม")
        return
    result = await MAIN_AGENT.handle("think", text, str(update.effective_chat.id))
    await _reply_smart(update, result)


async def cmd_park(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ไอเดียหลัง /park เช่น /park ระบบแจ้งเตือนแบบใหม่")
        return
    result = await MAIN_AGENT.handle("park", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์คำค้นหลัง /search เช่น /search โปรเจกต์ AI")
        return
    result = await MAIN_AGENT.handle("search", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /remember เช่น /remember ผมชอบกาแฟดำไม่ใส่น้ำตาล")
        return
    result = await MAIN_AGENT.handle("remember", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ keyword หลัง /forget เช่น /forget Bumrungrad")
        return
    result = await MAIN_AGENT.handle("forget", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("memory", "", str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("cost", "", str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์โจทย์หลัง /code เช่น /code เขียน FastAPI health check")
        return
    result = await MAIN_AGENT.handle("code", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_ener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์รายละเอียดหลัง /ener เช่น /ener วิเคราะห์พระสมเด็จรุ่นนี้")
        return
    result = await MAIN_AGENT.handle("ener", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์โจทย์หลัง /content เช่น /content เขียน caption ขายพระลง TikTok")
        return
    result = await MAIN_AGENT.handle("content", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await log_keeper.analyze_agent_health(_agent_triggered_by="user")
    await _reply(update, result)


async def handle_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    lines = 20
    if ctx.args and ctx.args[0].isdigit():
        lines = min(int(ctx.args[0]), 100)
    result = await cmd_logs(lines, _agent_triggered_by="user")
    await _reply(update, result)


async def handle_errors(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await cmd_errors(_agent_triggered_by="user")
    await _reply(update, result)


async def handle_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await cmd_server(_agent_triggered_by="user")
    await _reply(update, result)


async def handle_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await cmd_status(_agent_triggered_by="user")
    await _reply(update, result)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = (
        "📌 คำสั่งที่ใช้ได้ใน Ener-AI\n\n"
        "/note <ข้อความ>  — จดความคิด\n"
        "/task <ชื่องาน>  — สร้าง task (ใส่ !! = high priority)\n"
        "/tasks           — ดู task ทั้งหมด\n"
        "/done <id>       — ปิด task\n"
        "/learn <ข้อความ> — บันทึกบทเรียนจากความพลาด\n"
        "/mistake <ข้อความ> — เหมือน /learn\n"
        "/think <หัวข้อ>  — ถกไอเดีย 3 รอบ\n"
        "/brainstorm <หัวข้อ> — เหมือน /think\n"
        "/park <ข้อความ>  — เก็บไอเดียไว้ก่อน\n"
        "/search <คำค้น>  — ค้น memory เดิม\n"
        "/remember <ข้อความ> — บันทึก long-term memory ทันที\n"
        "/forget <คำค้น>  — ลบ long-term memory ที่ตรงคำค้น\n"
        "/memory          — ดู long-term memory ทั้งหมด\n"
        "/code <ข้อความ>  — ช่วยเขียน/review/debug code\n"
        "/ener <ข้อความ>  — วิเคราะห์พระ/ener report\n"
        "/content <ข้อความ> — สร้าง caption/script ขายของ\n"
        "/health          — ดูสุขภาพของ agents\n"
        "/logs [n]        — ดู logs ล่าสุด n บรรทัด\n"
        "/errors          — ดู errors อย่างเดียว\n"
        "/server          — CPU/RAM/Disk + processes\n"
        "/status          — สรุปสถานะทั้งหมด + AI วิเคราะห์\n"
        "/today           — สรุปวันนี้\n"
        "/news            — ดึงข่าว AI/Tech วันนี้\n"
        "/week            — รีวิว 7 วันที่ผ่านมา\n"
        "/cost            — ดูค่าใช้จ่าย AI\n"
        "\nหรือพิมพ์ข้อความปกติ โดยไม่มี / → คุยกับ Ener-AI ได้เลย"
    )
    await _reply(update, text)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("today", "", str(update.effective_chat.id))
    await _reply_smart(update, result)


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("news", "", str(update.effective_chat.id))
    await _reply_smart(update, result)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("week", "", str(update.effective_chat.id))
    await _reply_smart(update, result)


async def msg_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = update.message.text or ""
    if not text.strip():
        return
    chat_id = str(update.effective_chat.id)
    result = await MAIN_AGENT.run(chat_id, text)
    await _reply_smart(update, result)


def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("note", cmd_note))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("mistake", cmd_learn))
    app.add_handler(CommandHandler("think", cmd_think))
    app.add_handler(CommandHandler("brainstorm", cmd_think))
    app.add_handler(CommandHandler("park", cmd_park))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("remember", cmd_remember))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("ener", cmd_ener))
    app.add_handler(CommandHandler("content", cmd_content))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("logs", handle_logs))
    app.add_handler(CommandHandler("errors", handle_errors))
    app.add_handler(CommandHandler("server", handle_server))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_tts_callback, pattern="^tts:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_fallback))
    return app
