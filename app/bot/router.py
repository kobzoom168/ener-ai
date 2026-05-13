import asyncio
from collections import defaultdict
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
from app.agents.monitor_agent import (
    cmd_errors,
    cmd_logs,
    cmd_server,
    cmd_status,
    format_nl_resource_report,
    get_server_stats,
)
from app.agents.news_discovery import approve_source, get_pending_sources_data, list_active_sources
from app.agents.standup_agent import generate_standup, parse_and_update_from_chat, send_to_line
from app.agents.tarot_agent import read_cards, read_with_image
from app.agents.vision_agent import (
    analyze_image as vision_analyze,
    analyze_multiple_images as vision_analyze_multiple,
)
from app.core.config import settings
from app.core.policy import ALLOWED_CHAT_IDS, OWNER_LOCATION
from app.core.tts import text_to_audio_bytes, text_to_voice_bytes
from app.agents.main_agent import MAIN_AGENT

logger = logging.getLogger(__name__)
_media_group_cache: dict[str, list] = defaultdict(list)
_media_group_tasks: dict[str, asyncio.Task] = {}
TAROT_KEYWORDS = [
    "ไพ่",
    "ซุ่ม",
    "ดวง",
    "ทำนาย",
    "เสี่ยง",
    "พลังงาน",
    "ทาโรต์",
    "tarot",
    "อนาคต",
    "โชค",
    "เคราะห์",
    "ฤกษ์",
]


_REPLY_MAX_LEN = 4000


async def _reply_text_with_markdown_fallback(
    message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    chunks = [text[i:i + _REPLY_MAX_LEN] for i in range(0, len(text), _REPLY_MAX_LEN)]
    for i, chunk in enumerate(chunks):
        prefix = f"({i + 1}/{len(chunks)}) " if len(chunks) > 1 else ""
        content = prefix + chunk
        is_last = i == len(chunks) - 1
        try:
            await message.reply_text(
                content,
                reply_markup=reply_markup if is_last else None,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await message.reply_text(
                content,
                reply_markup=reply_markup if is_last else None,
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

    message = update.message
    photo = message.photo[-1]
    file_obj = await ctx.bot.get_file(photo.file_id)
    photo_bytes = io.BytesIO()
    await file_obj.download_to_memory(photo_bytes)
    image_data = photo_bytes.getvalue()
    caption = message.caption or ""
    media_group_id = message.media_group_id

    if not media_group_id:
        chat_id = str(update.effective_chat.id)

        if _is_tarot_request(caption):
            spread = "single"
            lowered = caption.lower()
            if "3" in caption or "สาม" in caption or "three" in lowered:
                spread = "three"
            elif "5" in caption or "ห้า" in caption or "celtic" in lowered:
                spread = "celtic"

            thinking = await message.reply_text("🔮 กำลังซุ่มไพ่และอ่านพลังงานรูป...", parse_mode=None)
            result = await read_with_image(
                image_data,
                question=caption,
                spread=spread,
                chat_id=chat_id,
                _agent_triggered_by="user",
            )
        else:
            thinking = await message.reply_text("🔍 กำลังวิเคราะห์รูป...", parse_mode=None)
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
        return

    _media_group_cache[media_group_id].append(
        {
            "image_data": image_data,
            "caption": caption,
            "update": update,
        }
    )

    existing_task = _media_group_tasks.get(media_group_id)
    if existing_task:
        existing_task.cancel()

    _media_group_tasks[media_group_id] = asyncio.create_task(
        _process_media_group(media_group_id)
    )


async def _process_media_group(group_id: str):
    try:
        await asyncio.sleep(2)
    except asyncio.CancelledError:
        return

    photos = _media_group_cache.pop(group_id, [])
    _media_group_tasks.pop(group_id, None)
    if not photos:
        return

    base_update = photos[0]["update"]
    thinking = await base_update.message.reply_text(
        f"🔍 กำลังวิเคราะห์ {len(photos)} รูป...",
        parse_mode=None,
    )

    caption = next((item["caption"] for item in photos if item["caption"]), "")
    chat_id = str(base_update.effective_chat.id)
    if _is_tarot_request(caption):
        lowered = caption.lower()
        spread = "single"
        if "3" in caption or "สาม" in caption or "three" in lowered:
            spread = "three"
        elif "5" in caption or "ห้า" in caption or "celtic" in lowered:
            spread = "celtic"

        try:
            await thinking.edit_text("🔮 กำลังซุ่มไพ่และอ่านพลังงานรูป...", parse_mode=None)
        except Exception:
            pass
        result = await read_with_image(
            photos[0]["image_data"],
            question=caption,
            spread=spread,
            chat_id=chat_id,
            _agent_triggered_by="user",
        )
    else:
        result = await vision_analyze_multiple(
            [item["image_data"] for item in photos],
            caption,
            chat_id=chat_id,
            _agent_triggered_by="user",
        )

    try:
        await thinking.delete()
    except Exception:
        pass
    await _reply_smart(base_update, result)


def _is_allowed(update: Update) -> bool:
    chat_id = str(update.effective_chat.id)
    return chat_id in [str(x) for x in ALLOWED_CHAT_IDS]


def _is_tarot_request(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in TAROT_KEYWORDS)


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


async def send_discovery_results(update: Update, sources: list[dict]):
    message = update.message
    if not message:
        return
    for src in sources[:5]:
        domain = str(src.get("domain", "")).strip()
        desc = str(src.get("description", "")).strip() or "-"
        rss = str(src.get("rss", "")).strip() or "-"
        score = max(0, min(int(src.get("score", 0) or 0), 10))
        stars = "⭐" * score if score else "-"

        text = (
            f"🌐 {domain}\n"
            f"{desc}\n"
            f"คะแนน: {stars}\n"
            f"RSS: {rss}"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{domain}"),
            InlineKeyboardButton("❌ Skip", callback_data=f"skip:{domain}"),
        ]])

        await message.reply_text(
            text,
            reply_markup=keyboard,
            parse_mode=None,
        )


async def handle_approve_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = str(query.data or "")
    parts = data.split(":", 1)
    action = parts[0] if parts else ""
    domain = parts[1] if len(parts) > 1 else ""

    if action == "approve":
        result = await approve_source(domain, _agent_triggered_by="user")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            f"{query.message.text}\n\n{result}",
            parse_mode=None,
        )
        return

    if action == "skip":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            f"{query.message.text}\n\n❌ Skipped",
            parse_mode=None,
        )


async def handle_pending_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    sources = await get_pending_sources_data(limit=10)
    if not sources:
        await _reply(update, "📭 ไม่มีแหล่งข่าวที่รอ approve", enable_tts=False)
        return
    await send_discovery_results(update, sources)


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


async def cmd_tarot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    args = ctx.args or []
    text = " ".join(args).strip() if args else ""

    spread = "single"
    lowered = text.lower()
    if "3" in text or "สาม" in text or "three" in lowered:
        spread = "three"
    elif "5" in text or "ห้า" in text or "celtic" in lowered:
        spread = "celtic"

    question = (
        text.replace("3ใบ", "")
        .replace("3 ใบ", "")
        .replace("สามใบ", "")
        .replace("สาม ใบ", "")
        .replace("5ใบ", "")
        .replace("5 ใบ", "")
        .replace("ห้าใบ", "")
        .replace("ห้า ใบ", "")
        .strip()
    )

    thinking = await update.message.reply_text("🔮 กำลังจั่วไพ่...", parse_mode=None)
    result = await read_cards(
        question=question,
        spread=spread,
        _agent_triggered_by="user",
    )
    try:
        await thinking.delete()
    except Exception:
        pass
    await _reply_smart(update, result)


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
        "/standup — สร้าง daily standup report\n"
        "/standup_line — ส่ง standup เข้า LINE group\n\n"
        "**🧠 คิด & วิเคราะห์**\n"
        "/think — ถกไอเดีย 3 รอบ\n"
        "/brainstorm — เหมือน /think\n"
        "/learn — บันทึกบทเรียน\n"
        "/mistake — เหมือน /learn\n"
        "/ener — วิเคราะห์พระ/ener report\n"
        "/content — สร้าง caption/script\n\n"
        "**🔮 Tarot**\n"
        "/tarot [คำถาม] — ซุ่มไพ่ทาโรต์\n"
        "/ดวง [คำถาม] — เหมือน /tarot\n"
        "ส่งรูป + ไพ่/ดวง/พลังงาน → ซุ่มไพ่ + อ่านรูปรวม\n\n"
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


async def cmd_standup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    message = update.message
    if not message:
        return
    await message.reply_text("⏳ กำลัง generate standup...", parse_mode=None)
    try:
        report = await generate_standup()
        if len(report) <= 4096:
            await message.reply_text(report, parse_mode=None)
            return
        chunks = [report[i:i + 4000] for i in range(0, len(report), 4000)]
        for chunk in chunks:
            await message.reply_text(chunk, parse_mode=None)
    except Exception as exc:
        await message.reply_text(f"⚠️ เกิดข้อผิดพลาด: {exc}", parse_mode=None)


async def cmd_standup_line(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    await update.message.reply_text("กำลังส่ง standup ไป LINE...", parse_mode=None)
    report = await generate_standup()
    ok, msg = await send_to_line(report)
    if ok:
        await update.message.reply_text("✅ ส่ง standup เข้า LINE group แล้วครับกบ", parse_mode=None)
    else:
        await update.message.reply_text(f"⚠️ ส่งไม่ได้: {msg}", parse_mode=None)


async def _handle_code_approval(update: Update, text: str) -> bool:
    """
    Intercept 'approve ENER-XXXX' and 'deploy' messages.
    Returns True if handled, False to continue normal routing.
    """
    upper = text.strip().upper()

    # approve ENER-XXXX  or  just ENER-XXXX
    if upper.startswith("APPROVE ") or upper.startswith("ENER-"):
        token = upper.replace("APPROVE ", "").strip()
        if not token.startswith("ENER-"):
            return False
        from app.core.database import get_pending_code_request, update_code_request_status
        from app.core.code_agent import apply_code_change

        req = await get_pending_code_request(token)
        if not req:
            await update.message.reply_text("❌ ไม่พบ token นี้ หรือหมดอายุแล้ว")
            return True
        await update.message.reply_text("⚡ กำลัง apply code...")
        await update_code_request_status(req["id"], "approved")
        result = await apply_code_change(req["id"])
        if result["ok"]:
            files_str = ", ".join(result.get("files_written", []))
            await update.message.reply_text(
                f"✅ Apply สำเร็จ!\n"
                f"ไฟล์: {files_str}\n"
                f"🌿 Branch: {result.get('branch')}\n"
                f"พิมพ์ 'deploy' เพื่อ rebuild Docker"
            )
        else:
            await update.message.reply_text(
                f"❌ ล้มเหลว (rolled back)\n{result.get('error', '')[:200]}"
            )
        return True

    if upper == "DEPLOY":
        from app.core.code_agent import deploy_after_apply

        await update.message.reply_text("🔨 กำลัง deploy...")
        r = await deploy_after_apply()
        await update.message.reply_text(
            "🚀 Deploy สำเร็จ!" if r["ok"] else f"❌ Deploy ล้มเหลว\n{r['output'][:300]}"
        )
        return True

    return False


async def cmd_diag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) > 1 else ""
    from app.core import diagnostics as diag

    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    intent_tag = ""
    if sub in ("resource", "cpu", "ram", "mem"):
        intent_tag = " intent=resource"
    await diag.log_diagnostic_audit(
        "DIAG_REQUEST",
        f"cmd=diag sub={sub or 'full'}{intent_tag} chat_id={chat_id}",
    )
    try:
        if not sub:
            o, a, b = await asyncio.gather(
                diag.diagnose_otp_loop(),
                diag.diagnose_agent_health(),
                diag.diagnose_bot_unresponsive(),
            )
            txt = diag.format_system_diagnosis_thai(o, a, b)
        elif sub in ("otp", "otp_loop", "otp-loop"):
            d = await diag.diagnose_otp_loop()
            txt = diag.format_otp_diagnosis_thai(d)
        elif sub in ("memory", "agent", "agents"):
            d = await diag.diagnose_agent_health()
            txt = diag.format_agent_diagnosis_thai(d)
        elif sub in ("bot", "telegram", "webhook"):
            d = await diag.diagnose_bot_unresponsive()
            txt = diag.format_bot_diagnosis_thai(d)
        elif sub in ("resource", "cpu", "ram", "mem"):
            d = await diag.diagnose_resource_usage()
            txt = diag.format_resource_diagnosis_thai(d)
        else:
            txt = (
                "ใช้: `/diag` หรือ `/diag otp` · `/diag memory` · `/diag bot` · `/diag resource`"
            )

        for chunk in diag.split_telegram_chunks(txt):
            await _reply_smart(update, chunk)
        await diag.log_diagnostic_audit(
            "DIAG_SUCCESS",
            f"cmd=diag sub={sub or 'full'}{intent_tag} chat_id={chat_id}",
        )
    except Exception as exc:
        await diag.log_diagnostic_audit(
            "DIAG_FAILED",
            f"cmd=diag sub={sub or 'full'}{intent_tag} chat_id={chat_id} err={type(exc).__name__}:{exc!s}"[
                :1900
            ],
        )
        logger.warning("cmd_diag failed: %s", exc, exc_info=True)
        await _reply_smart(
            update,
            f"รวบรวม diagnostic ไม่สำเร็จ ({type(exc).__name__}) — ตรวจ audit_logs DIAG_FAILED",
        )


async def cmd_resource_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from app.core import diagnostics as diag

    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    await diag.log_diagnostic_audit(
        "DIAG_REQUEST",
        f"cmd=resource_debug intent=resource chat_id={chat_id}",
    )
    try:
        d = await diag.diagnose_resource_usage(debug=True)
        txt = diag.format_resource_diagnosis_thai(d) + diag.format_resource_debug_appendix(d)
        for chunk in diag.split_telegram_chunks(txt):
            await _reply_smart(update, chunk)
        await diag.log_diagnostic_audit(
            "DIAG_SUCCESS",
            f"cmd=resource_debug intent=resource chat_id={chat_id}",
        )
    except Exception as exc:
        await diag.log_diagnostic_audit(
            "DIAG_FAILED",
            f"cmd=resource_debug intent=resource chat_id={chat_id} err={type(exc).__name__}:{exc!s}"[
                :1900
            ],
        )
        logger.warning("cmd_resource_debug failed: %s", exc, exc_info=True)
        await _reply_smart(
            update,
            f"รวบรวม resource diagnostic ไม่สำเร็จ ({type(exc).__name__})",
        )


async def cmd_otp_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    from app.core import diagnostics as diag

    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    await diag.log_diagnostic_audit("DIAG_REQUEST", f"cmd=otp_debug chat_id={chat_id}")
    try:
        d = await diag.diagnose_otp_loop()
        txt = diag.format_otp_diagnosis_thai(d)
        for chunk in diag.split_telegram_chunks(txt):
            await _reply_smart(update, chunk)
        await diag.log_diagnostic_audit("DIAG_SUCCESS", f"cmd=otp_debug chat_id={chat_id}")
    except Exception as exc:
        await diag.log_diagnostic_audit(
            "DIAG_FAILED",
            f"cmd=otp_debug chat_id={chat_id} err={type(exc).__name__}:{exc!s}"[:1900],
        )
        logger.warning("cmd_otp_debug failed: %s", exc, exc_info=True)
        await _reply_smart(
            update,
            f"รวบรวม OTP diagnostic ไม่สำเร็จ ({type(exc).__name__})",
        )


async def cmd_sys_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_diag(update, ctx)


async def _run_nl_intent_section(intent: str, text: str, chat_id: str) -> tuple[str, str]:
    """Build one markdown section for multi-intent NL routing (real tools / diagnostics)."""
    from app.core import diagnostics as diag

    if intent == "communication":
        return "### 💬 การติดตามลูกค้า / ทีม", diag.communication_followup_reply_thai(text)
    if intent == "system_status":
        body = await cmd_status(_agent_triggered_by="user")
        return "### ระบบ", body
    if intent == "system_errors":
        body = await cmd_errors(_agent_triggered_by="user")
        return "### Errors", body
    if intent == "system_logs":
        body = await cmd_logs(50, _agent_triggered_by="user")
        return "### Logs", body
    if intent == "system_server":
        stats = get_server_stats()
        return "### เครื่อง / ทรัพยากร", format_nl_resource_report(stats)
    if intent == "diag_resource":
        await diag.log_diagnostic_audit(
            "DIAG_REQUEST",
            f"intent=resource nl=1 chat_id={chat_id}",
        )
        try:
            d = await diag.diagnose_resource_usage()
            body = diag.format_resource_diagnosis_thai(d)
            await diag.log_diagnostic_audit(
                "DIAG_SUCCESS",
                f"intent=resource nl=1 chat_id={chat_id}",
            )
        except Exception as exc:
            await diag.log_diagnostic_audit(
                "DIAG_FAILED",
                f"intent=resource nl=1 chat_id={chat_id} err={type(exc).__name__}:{exc!s}"[:1900],
            )
            raise
        return "### Resource (หลักฐานจริง)", body
    if intent == "diag_otp":
        d = await diag.diagnose_otp_loop()
        return "### OTP", diag.format_otp_diagnosis_thai(d)
    if intent == "diag_bot":
        d = await diag.diagnose_bot_unresponsive()
        return "### Bot / Telegram", diag.format_bot_diagnosis_thai(d)
    if intent == "diag_agent":
        d = await diag.diagnose_agent_health()
        return "### Agent / Memory", diag.format_agent_diagnosis_thai(d)
    return "### อื่น ๆ", "ไม่รองรับ intent นี้"


async def msg_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = update.message.text or ""
    if not text.strip():
        return

    # Check code approval flow first
    if await _handle_code_approval(update, text):
        return

    from app.core import diagnostics as diag
    from app.core.tool_router import classify_message_intents

    intents = classify_message_intents(text)
    if intents:
        parts: list[str] = []
        chat_id = str(update.effective_chat.id)
        for intent in intents:
            heading, body = await _run_nl_intent_section(intent, text, chat_id)
            parts.append(f"{heading}\n{body}")
        out = "\n\n".join(parts)
        for chunk in diag.split_telegram_chunks(out):
            await _reply_smart(update, chunk)
        return

    nl_diag = diag.classify_diagnostic_intent(text)
    if nl_diag:
        chat_id = str(update.effective_chat.id)
        try:
            reply = await diag.diagnose_user_message(text, chat_id)
        except Exception as exc:
            logger.warning("diagnose_user_message failed: %s", exc, exc_info=True)
            await _reply_smart(update, f"diagnostic error: `{type(exc).__name__}`")
            return
        for chunk in diag.split_telegram_chunks(reply):
            await _reply_smart(update, chunk)
        return

    lowered = text.lower()
    if any(keyword in lowered for keyword in ["อัปเดต", "update", "%", "เปอร์เซ็น", "เสร็จแล้ว", "complete"]):
        result = await parse_and_update_from_chat(text)
        if result:
            await _reply(update, result, enable_tts=False)
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
    app.add_handler(CommandHandler("tarot", cmd_tarot))
    app.add_handler(CommandHandler("card", cmd_tarot))
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
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("otp_debug", cmd_otp_debug))
    app.add_handler(CommandHandler("resource_debug", cmd_resource_debug))
    app.add_handler(CommandHandler("sys_debug", cmd_sys_debug))
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
    app.add_handler(CommandHandler("standup", cmd_standup))
    app.add_handler(CommandHandler("standup_line", cmd_standup_line))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("h", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_approve_callback, pattern="^(approve|skip):"))
    app.add_handler(CallbackQueryHandler(handle_email_callback, pattern="^email_"))
    app.add_handler(CallbackQueryHandler(handle_tts_callback, pattern="^tts:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_fallback))
    return app
