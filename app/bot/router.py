import io
import re
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.policy import ALLOWED_CHAT_IDS
from app.core.tts import is_voice_enabled, set_voice_mode, text_to_voice_bytes
from app.agents import brain, brainstorm, chat, learn, memory, news, summary, task


async def _reply(update: Update, text: str):
    await update.message.reply_text(text, parse_mode=None)


async def _reply_smart(update: Update, text: str):
    chat_id = str(update.effective_chat.id)
    if await is_voice_enabled(chat_id):
        try:
            audio_bytes = await text_to_voice_bytes(text)
            voice_file = io.BytesIO(audio_bytes)
            voice_file.name = "ener-ai-voice.mp3"
            await update.message.reply_voice(voice=voice_file)
            await update.message.reply_text(text, parse_mode=None)
            return
        except Exception:
            await update.message.reply_text(text, parse_mode=None)
            return
    await update.message.reply_text(text, parse_mode=None)


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
    result = await brain.process_note(text, chat_id)
    await _reply_smart(update, result)


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not ctx.args:
        await _reply(update, "📌 พิมพ์ชื่อ task เช่น /task ซื้อของ")
        return
    raw = " ".join(ctx.args)

    priority = "medium"
    deadline_hint = ""

    if "!!" in raw:
        priority = "high"
        raw = raw.replace("!!", "").strip()
    elif "!" in raw:
        priority = "medium"
        raw = raw.replace("!", "").strip()

    deadline_match = re.search(r"deadline[:\s]+(.+?)(?:\s|$)", raw, re.IGNORECASE)
    if deadline_match:
        deadline_hint = deadline_match.group(1).strip()
        raw = raw[: deadline_match.start()].strip()

    result = await task.create_task(raw, priority=priority, deadline_hint=deadline_hint)
    await _reply(update, result)


async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await task.list_tasks()
    await _reply(update, result)


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await _reply(update, "📌 ระบุ ID task เช่น /done 3")
        return
    result = await task.complete_task(int(ctx.args[0]))
    await _reply(update, result)


async def cmd_learn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /learn เช่น /learn พลาดเรื่อง X เพราะ Y")
        return
    result = await learn.record_lesson(text)
    await _reply(update, result)


async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์หัวข้อหลัง /think เช่น /think ทำโปรดักต์ AI ตัวนี้ดีไหม")
        return
    result = await brainstorm.run_brainstorm(text)
    await _reply_smart(update, result)


async def cmd_park(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ไอเดียหลัง /park เช่น /park ระบบแจ้งเตือนแบบใหม่")
        return
    result = await memory.park_idea(text)
    await _reply(update, result)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์คำค้นหลัง /search เช่น /search โปรเจกต์ AI")
        return
    result = await memory.search_memory(text)
    await _reply(update, result)


async def cmd_remember(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /remember เช่น /remember ผมชอบกาแฟดำไม่ใส่น้ำตาล")
        return
    result = await memory.remember_memory(text)
    await _reply(update, result)


async def cmd_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ keyword หลัง /forget เช่น /forget Bumrungrad")
        return
    result = await memory.forget_memory(text)
    await _reply(update, result)


async def cmd_memory(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await memory.list_memory()
    await _reply(update, result)


async def cmd_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    chat_id = str(update.effective_chat.id)
    if not ctx.args:
        enabled = await is_voice_enabled(chat_id)
        status = "🔊 เปิดอยู่" if enabled else "🔇 ปิดอยู่"
        await _reply(
            update,
            f"📌 Voice Mode: {status}\n\n"
            f"🔊 /voice on  — เปิด (ส่งเสียง + ข้อความ)\n"
            f"🔇 /voice off — ปิด (ข้อความอย่างเดียว)\n\n"
            f"เสียง: ภาษาไทย หญิง (PremwadeeNeural)",
        )
        return

    cmd = ctx.args[0].lower()
    if cmd == "on":
        await set_voice_mode(chat_id, True)
        await _reply(
            update,
            "🔊 เปิด Voice Mode แล้วครับ\n"
            "AI จะส่งเสียงภาษาไทยหญิง + ข้อความเต็มพร้อมกัน",
        )
    elif cmd == "off":
        await set_voice_mode(chat_id, False)
        await _reply(
            update,
            "🔇 ปิด Voice Mode แล้วครับ\n"
            "กลับเป็นข้อความปกติ",
        )
    else:
        await _reply(update, "📌 พิมพ์ /voice on หรือ /voice off ครับ")


async def cmd_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    async with get_db() as db:
        today_cursor = await db.execute(
            "SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total FROM ai_runs WHERE date(created_at) = date('now', 'localtime')",
        )
        today_row = await today_cursor.fetchone()
        month_cursor = await db.execute(
            """
            SELECT COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
            """,
        )
        month_row = await month_cursor.fetchone()
        model_cursor = await db.execute(
            """
            SELECT model, COALESCE(SUM(estimated_cost_thb), 0) AS total
            FROM ai_runs
            WHERE strftime('%Y-%m', created_at, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
            GROUP BY model
            ORDER BY total DESC, model
            """,
        )
        model_rows = await model_cursor.fetchall()
        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("cost_viewed", f"chat_id={update.effective_chat.id}"),
        )
        await db.commit()

    lines = [
        "📌 สรุปค่าใช้จ่าย AI",
        "",
        "📊 ค่าใช้จ่าย AI",
        "",
        f"วันนี้: {float(today_row['total']):.2f} บาท",
        f"เดือนนี้: {float(month_row['total']):.2f} บาท",
        "",
        "แยกตาม model:",
    ]
    if model_rows:
        for row in model_rows:
            lines.append(f"· {row['model']}: {float(row['total']):.2f} บาท")
    else:
        lines.append("· ยังไม่มี")
    await _reply(update, "\n".join(lines))


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
        "/voice           — เปิด/ปิดตอบเป็นเสียง\n"
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
    result = await summary.generate_daily_summary()
    await _reply_smart(update, result)


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await news.fetch_and_summarize()
    await _reply_smart(update, result)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    result = await summary.generate_weekly_summary()
    await _reply_smart(update, result)


async def msg_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    text = update.message.text or ""
    if not text.strip():
        return
    chat_id = str(update.effective_chat.id)
    pending = await brain.get_pending_clarification(chat_id)
    if pending is not None:
        normalized = text.strip()
        if normalized in {"1", "2", "3", "4", "1️⃣", "2️⃣", "3️⃣", "4️⃣"}:
            pending_result = await brain.handle_pending_reply(chat_id, text)
            if pending_result is not None:
                await _reply_smart(update, pending_result)
                return
        else:
            await brain.clear_pending_clarification(chat_id)
    result = await chat.run_chat(chat_id, text)
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
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_fallback))
    return app
