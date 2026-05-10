import hashlib
import io
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from app.agents import github_agent, gmail_agent, log_keeper, memory_curator, memory_keeper
from app.agents.monitor_agent import cmd_errors, cmd_logs, cmd_server, cmd_status
from app.agents.news_discovery import approve_source, list_active_sources, list_pending_sources
from app.agents.vision_agent import analyze_image as vision_analyze
from app.core.config import settings
from app.core.policy import ALLOWED_CHAT_IDS, OWNER_LOCATION
from app.core.tts import text_to_audio_bytes, text_to_voice_bytes
from app.agents.main_agent import MAIN_AGENT

logger = logging.getLogger(__name__)


async def _reply_text_with_markdown_fallback(
    message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    try:
        await message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        await message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=None,
        )


def _merge_voice_button(
    text_hash: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> InlineKeyboardMarkup:
    voice_row = [InlineKeyboardButton("🔊 ฟัง", callback_data=f"tts:{text_hash}")]
    if not reply_markup:
        return InlineKeyboardMarkup([voice_row])

    rows = [list(row) for row in reply_markup.inline_keyboard]
    has_voice = any(
        any(str(button.callback_data or "").startswith("tts:") for button in row)
        for row in rows
    )
    if not has_voice:
        rows.append(voice_row)
    return InlineKeyboardMarkup(rows)


async def _reply(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    enable_tts: bool = True,
):
    if enable_tts:
        await _reply_smart(update, text, reply_markup=reply_markup)
        return
    await _reply_text_with_markdown_fallback(update.message, text, reply_markup=reply_markup)


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


async def _reply_smart(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    text_hash = hashlib.md5(text.encode()).hexdigest()[:16]
    keyboard = _merge_voice_button(text_hash, reply_markup=reply_markup)
    chat_id = str(update.effective_chat.id)
    await _cache_tts_text(chat_id, text_hash, text)
    await _reply_text_with_markdown_fallback(update.message, text, reply_markup=keyboard)


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
        voice_bytes = await text_to_voice_bytes(text)
        voice_file = io.BytesIO(voice_bytes)
        voice_file.name = "voice.ogg"
        await query.message.reply_voice(voice=voice_file)
    except Exception as e:
        logger.error(f"TTS callback error: {e}", exc_info=True)
        try:
            audio_bytes = await text_to_audio_bytes(text)
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "reply.mp3"
            await query.message.reply_audio(audio=audio_file, title="Ener-AI")
        except Exception:
            try:
                await query.answer("ส่งเสียงไม่ได้ตอนนี้", show_alert=True)
            except Exception:
                pass


async def handle_email_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    if data.startswith("email_draft:"):
        await query.answer()
        email_id = data.split(":", 1)[1]
        draft = await gmail_agent.draft_reply(email_id, _agent_triggered_by="user")
        text_hash = hashlib.md5(draft.encode()).hexdigest()[:16]
        await _cache_tts_text(str(query.message.chat.id), text_hash, draft)
        keyboard = _merge_voice_button(text_hash)
        await query.message.reply_text(draft, reply_markup=keyboard, parse_mode=None)
    elif data.startswith("email_skip:"):
        await query.answer("ข้ามแล้วครับ", show_alert=False)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]
    file_obj = await ctx.bot.get_file(photo.file_id)
    photo_bytes = io.BytesIO()
    await file_obj.download_to_memory(photo_bytes)
    image_data = photo_bytes.getvalue()
    caption = update.message.caption or ""
    chat_id = str(update.effective_chat.id)

    thinking = await update.message.reply_text("🔍 กำลังวิเคราะห์รูป...", parse_mode=None)
    result = await vision_analyze(
        image_data,
        caption,
        chat_id=chat_id,
        _agent_triggered_by="user",
    )
    try:
        await thinking.delete()
    except Exception:
        pass
    await _reply_smart(update, result)


def _is_allowed(update: Update) -> bool:
    chat_id = str(update.effective_chat.id)
    return chat_id in [str(x) for x in ALLOWED_CHAT_IDS]


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /note เช่น /note ไอเดีย X", enable_tts=False)
        return
    chat_id = str(update.effective_chat.id)
    result = await MAIN_AGENT.handle("note", text, chat_id)
    await _reply_smart(update, result)


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not ctx.args:
        await _reply(update, "📌 พิมพ์ชื่อ task เช่น /task ซื้อของ", enable_tts=False)
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
        await _reply(update, "📌 ระบุ ID task เช่น /done 3", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("done", ctx.args[0], str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_learn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /learn เช่น /learn พลาดเรื่อง X เพราะ Y", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("learn", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์หัวข้อหลัง /think เช่น /think ทำโปรดักต์ AI ตัวนี้ดีไหม", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("think", text, str(update.effective_chat.id))
    await _reply_smart(update, result)


async def cmd_park(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ไอเดียหลัง /park เช่น /park ระบบแจ้งเตือนแบบใหม่", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("park", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์คำค้นหลัง /search เช่น /search โปรเจกต์ AI", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("search", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /remember เช่น /remember ผมชอบกาแฟดำไม่ใส่น้ำตาล", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("remember", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ keyword หลัง /forget เช่น /forget Bumrungrad", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("forget", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await MAIN_AGENT.handle("memory", "", str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_memory_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await memory_keeper.run_memory_keeper(
        str(update.effective_chat.id),
        _agent_triggered_by="user",
    )
    await _reply(update, result)


async def cmd_memory_curate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await memory_curator.curate_memories(_agent_triggered_by="user")
    await _reply(update, result)


async def cmd_github(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    args = ctx.args or []
    if not args:
        prs = await github_agent.list_prs(_agent_triggered_by="user")
        issues = await github_agent.list_issues(_agent_triggered_by="user")
        await _reply(update, f"{prs}\n\n{issues}")
        return

    subcommand = str(args[0]).strip().lower()
    if subcommand == "prs":
        repo_name = str(args[1]).strip() if len(args) > 1 else None
        result = await github_agent.list_prs(repo_name, _agent_triggered_by="user")
        await _reply(update, result)
        return

    if subcommand == "issues":
        repo_name = str(args[1]).strip() if len(args) > 1 else None
        result = await github_agent.list_issues(repo_name, _agent_triggered_by="user")
        await _reply(update, result)
        return

    if subcommand == "repos":
        result = await github_agent.list_repos(_agent_triggered_by="user")
        await _reply(update, result)
        return

    if subcommand == "read":
        if len(args) < 3:
            await _reply(update, "📌 ใช้แบบนี้: /github read <repo> <path>", enable_tts=False)
            return
        repo_name = str(args[1]).strip()
        file_path = " ".join(args[2:]).strip()
        result = await github_agent.read_file(repo_name, file_path, _agent_triggered_by="user")
        await _reply(update, result)
        return

    await _reply(
        update,
        "📌 ใช้ /github, /github prs, /github issues, /github repos, หรือ /github read <repo> <path>",
        enable_tts=False,
    )


async def cmd_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    subcommand = str(ctx.args[0]).strip().lower() if ctx.args else ""
    if subcommand == "home":
        text = (
            "📍 บ้าน: "
            f"{OWNER_LOCATION['home']}\n"
            "[ดูบน Maps](https://maps.google.com/maps?q=eco house+วงแหวนลำลูกกา+ปทุมธานี)"
        )
        await _reply(update, text, enable_tts=False)
        return

    if subcommand == "work":
        text = (
            "🏥 ที่ทำงาน: "
            f"{OWNER_LOCATION['work']}\n"
            "[ดูบน Maps](https://maps.google.com/maps?q=โรงพยาบาลจักษุ+รัตนิน+กรุงเทพ)"
        )
        await _reply(update, text, enable_tts=False)
        return

    text = (
        "📍 Location ของกบ\n\n"
        f"บ้าน: {OWNER_LOCATION['home']}\n"
        f"งาน: {OWNER_LOCATION['work']}\n\n"
        "ใช้ `/location home` เพื่อดู Maps ใกล้บ้าน\n"
        "ใช้ `/location work` เพื่อดู Maps ใกล้ที่ทำงาน"
    )
    await _reply(update, text, enable_tts=False)


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
        await _reply(update, "📌 พิมพ์โจทย์หลัง /code เช่น /code เขียน FastAPI health check", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("code", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_ener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์รายละเอียดหลัง /ener เช่น /ener วิเคราะห์พระสมเด็จรุ่นนี้", enable_tts=False)
        return
    result = await MAIN_AGENT.handle("ener", text, str(update.effective_chat.id))
    await _reply(update, result)


async def cmd_content(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์โจทย์หลัง /content เช่น /content เขียน caption ขายพระลง TikTok", enable_tts=False)
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


async def handle_approve_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    domain = " ".join(ctx.args).strip() if ctx.args else ""
    if not domain:
        await _reply(update, "📌 พิมพ์ domain หลัง /approve_source เช่น /approve_source example.com", enable_tts=False)
        return
    result = await approve_source(domain, _agent_triggered_by="user")
    await _reply(update, result)


async def handle_pending_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await list_pending_sources(_agent_triggered_by="user")
    await _reply(update, result)


async def handle_list_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await list_active_sources(_agent_triggered_by="user")
    await _reply(update, result)


async def cmd_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    if not ctx.args:
        emails = await gmail_agent.fetch_unread_emails(_agent_triggered_by="user")
        result = await gmail_agent.summarize_emails(_agent_triggered_by="user")
        if not emails:
            await _reply_smart(update, result)
            return

        buttons = []
        for email in emails[:5]:
            subject_short = email["subject"][:20]
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"✍️ Draft: {subject_short}",
                        callback_data=f"email_draft:{email['id']}",
                    ),
                    InlineKeyboardButton(
                        "🗑️ Skip",
                        callback_data=f"email_skip:{email['id']}",
                    ),
                ]
            )

        keyboard = InlineKeyboardMarkup(buttons)
        await _reply_smart(update, result, reply_markup=keyboard)
        return

    subcommand = str(ctx.args[0]).strip().lower()
    if subcommand == "reply":
        if len(ctx.args) < 3:
            await _reply(update, "📌 ใช้แบบนี้: /email reply <id> <ข้อความตอบกลับ>", enable_tts=False)
            return
        email_id = ctx.args[1].strip()
        reply_text = " ".join(ctx.args[2:]).strip()
        result = await gmail_agent.reply_email(email_id, reply_text, _agent_triggered_by="user")
        await _reply(update, result)
        return

    if subcommand == "draft":
        if len(ctx.args) < 2:
            await _reply(update, "📌 ใช้แบบนี้: /email draft <id>", enable_tts=False)
            return
        email_id = ctx.args[1].strip()
        result = await gmail_agent.draft_reply(email_id, _agent_triggered_by="user")
        await _reply_smart(update, result)
        return

    await _reply(update, "📌 ใช้ /email, /email draft <id>, หรือ /email reply <id> <ข้อความ>", enable_tts=False)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = (
        "📋 **คำสั่ง Ener-AI**\n\n"
        "**💬 บทสนทนา**\n"
        "พิมพ์ตรงๆ → คุยกับ Ener-AI\n"
        "ส่งรูป → วิเคราะห์อัตโนมัติ\n\n"
        "**📝 จด & จำ**\n"
        "/note — จดความคิด\n"
        "/remember — บันทึก long-term memory\n"
        "/memory — ดู memory ทั้งหมด\n"
        "/memory_sync — สกัด & dedup memory\n"
        "/memory_curate — รวม memory เป็น cards\n"
        "/forget — ลบ memory\n"
        "/search — ค้นหา memory เก่า\n"
        "/park — เก็บไอเดียไว้ก่อน\n\n"
        "**✅ Task**\n"
        "/task — สร้าง task (!! = high priority)\n"
        "/tasks — ดู task ทั้งหมด\n"
        "/done — ปิด task\n\n"
        "**🧠 คิด & วิเคราะห์**\n"
        "/think — ถกไอเดีย 3 รอบ\n"
        "/brainstorm — เหมือน /think\n"
        "/learn — บันทึกบทเรียน\n"
        "/mistake — เหมือน /learn\n"
        "/ener — วิเคราะห์พระ/ener report\n"
        "/content — สร้าง caption/script\n\n"
        "**💻 Code & GitHub**\n"
        "/code — เขียน/review/debug code\n"
        "/github — ดู PRs + Issues\n"
        "/github repos — ดู repos\n"
        "/github prs — ดู open PRs\n"
        "/github issues — ดู open Issues\n"
        "/github read <repo> <path> — อ่านไฟล์\n\n"
        "**📧 Email**\n"
        "/email — สรุป email ใหม่\n"
        "/email draft <id> — ร่างคำตอบ\n"
        "/email reply <id> <ข้อความ> — ตอบเลย\n\n"
        "**📰 ข่าว & สรุป**\n"
        "/news — ข่าว AI/Tech วันนี้\n"
        "/today — สรุปวันนี้\n"
        "/week — รีวิว 7 วัน\n"
        "/list_sources — แหล่งข่าวที่ใช้\n"
        "/pending_sources — แหล่งข่าวที่รอ approve\n"
        "/approve_source <domain> — อนุมัติแหล่งข่าวใหม่\n\n"
        "**🖥️ Server**\n"
        "/status — สรุปสถานะ + AI วิเคราะห์\n"
        "/server — CPU/RAM/Disk\n"
        "/logs — logs ล่าสุด\n"
        "/errors — errors อย่างเดียว\n"
        "/health — สุขภาพ agents\n\n"
        "**📍 อื่นๆ**\n"
        "/location — ดู location บ้าน/งาน\n"
        "/cost — ค่าใช้จ่าย AI\n"
        "/help — ดูคำสั่งทั้งหมด\n"
        "/start — เหมือน /help"
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
    app.add_handler(CommandHandler("memory_sync", cmd_memory_sync))
    app.add_handler(CommandHandler("memory_curate", cmd_memory_curate))
    app.add_handler(CommandHandler("github", cmd_github))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("ener", cmd_ener))
    app.add_handler(CommandHandler("content", cmd_content))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("logs", handle_logs))
    app.add_handler(CommandHandler("errors", handle_errors))
    app.add_handler(CommandHandler("server", handle_server))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("approve_source", handle_approve_source))
    app.add_handler(CommandHandler("pending_sources", handle_pending_sources))
    app.add_handler(CommandHandler("list_sources", handle_list_sources))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("?", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_email_callback, pattern="^email_"))
    app.add_handler(CallbackQueryHandler(handle_tts_callback, pattern="^tts:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_fallback))
    return app
