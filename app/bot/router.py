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
from app.agents import brain, brainstorm, learn, memory, news, summary, task


async def _reply(update: Update, text: str):
    await update.message.reply_text(text, parse_mode=None)


async def cmd_note(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /note เช่น /note ไอเดีย X")
        return
    chat_id = str(update.effective_chat.id)
    result = await brain.process_note(text, chat_id)
    await _reply(update, result)


async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
    result = await task.list_tasks()
    await _reply(update, result)


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args or not ctx.args[0].isdigit():
        await _reply(update, "📌 ระบุ ID task เช่น /done 3")
        return
    result = await task.complete_task(int(ctx.args[0]))
    await _reply(update, result)


async def cmd_learn(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ข้อความหลัง /learn เช่น /learn พลาดเรื่อง X เพราะ Y")
        return
    result = await learn.record_lesson(text)
    await _reply(update, result)


async def cmd_think(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์หัวข้อหลัง /think เช่น /think ทำโปรดักต์ AI ตัวนี้ดีไหม")
        return
    result = await brainstorm.run_brainstorm(text)
    await _reply(update, result)


async def cmd_park(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์ไอเดียหลัง /park เช่น /park ระบบแจ้งเตือนแบบใหม่")
        return
    result = await memory.park_idea(text)
    await _reply(update, result)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await _reply(update, "📌 พิมพ์คำค้นหลัง /search เช่น /search โปรเจกต์ AI")
        return
    result = await memory.search_memory(text)
    await _reply(update, result)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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
        "/today           — สรุปวันนี้\n"
        "/news            — ดึงข่าว AI/Tech วันนี้\n"
        "/week            — รีวิว 7 วันที่ผ่านมา\n"
        "\nหรือพิมพ์ตรงๆ โดยไม่มี / → บันทึกใน brain อัตโนมัติ"
    )
    await _reply(update, text)


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await summary.generate_daily_summary()
    await _reply(update, result)


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await news.fetch_and_summarize()
    await _reply(update, result)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    result = await summary.generate_weekly_summary()
    await _reply(update, result)


async def msg_fallback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.strip():
        return
    chat_id = str(update.effective_chat.id)
    pending_result = await brain.handle_pending_reply(chat_id, text)
    if pending_result is not None:
        await _reply(update, pending_result)
        return
    result = await brain.process_note(text, chat_id)
    await _reply(update, result)


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
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, msg_fallback))
    return app
