import asyncio
import html
import re
import feedparser
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import get_agent_context, log_event
from app.core.policy import ALLOWED_NEWS_SOURCES, build_system_prompt

_BANGKOK = ZoneInfo("Asia/Bangkok")
_RSS_FEEDS = {
    "techcrunch.com": "https://techcrunch.com/feed/",
    "theverge.com": "https://www.theverge.com/rss/index.xml",
    "arxiv.org": "https://rss.arxiv.org/rss/cs.AI",
    "reuters.com": "https://feeds.reuters.com/reuters/technologyNews",
    "arstechnica.com": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "wired.com": "https://www.wired.com/feed/rss",
}
_TOPIC_KEYWORDS = [
    "ai",
    "artificial intelligence",
    "machine learning",
    "deep learning",
    "llm",
    "model",
    "agent",
    "openai",
    "anthropic",
    "google",
    "microsoft",
    "meta",
    "nvidia",
    "chip",
    "software",
    "developer",
    "programming",
    "cybersecurity",
    "security",
    "cloud",
    "robot",
    "automation",
    "startup",
    "app",
    "api",
    "tech",
    "technology",
]
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
SUMMARY_SYSTEM = build_system_prompt("""คุณเป็น AI วิเคราะห์ข่าวสำหรับกบ

บริบทของกบ:
- IT PM ดูแลระบบ infra โรงพยาบาล
- มี Ener-AI (personal AI assistant)
- มี Ener Scan (สแกนพลังงานพระเครื่อง LINE bot)
- ขายพระผ่าน TikTok/YouTube/Facebook
- กำลังสร้าง content และ automation system

วิเคราะห์ข่าวนี้แล้วตอบ JSON:
{
  "title_th": "หัวข้อภาษาไทยกระชับ",
  "summary": "สรุปเนื้อหา 2-3 ประโยค เข้าใจง่าย",
  "apply_to": {
    "ener_ai": "นำไปใช้กับ Ener-AI ได้ยังไง (ถ้าได้)",
    "ener_scan": "นำไปใช้กับ Ener Scan ได้ยังไง (ถ้าได้)",
    "content": "ทำ content จากข่าวนี้ได้ไหม hook คืออะไร",
    "it_work": "เกี่ยวกับงาน IT โรงพยาบาลไหม"
  },
  "action": "สิ่งที่กบควรทำต่อ 1 อย่าง (ถ้ามี)",
  "priority": "high|medium|low"
}

ถ้าไม่เกี่ยวกับกบเลย → priority: low และ apply_to ทุก field เป็น null""")


def _matches_topic(topic_text: str) -> bool:
    for keyword in _TOPIC_KEYWORDS:
        if " " in keyword:
            if keyword in topic_text:
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", topic_text):
            return True
    return False


def _clean_text(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _format_apply_line(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"   · {label}: {value}"


@log_agent_run("NewsAgent", triggered_by="scheduler")
async def fetch_and_summarize() -> str:
    agent_memory = await get_agent_context("NewsAgent", ["news", "ai", "tech"])
    items: list[dict[str, str]] = []
    seen_links: set[str] = set()

    for source in ALLOWED_NEWS_SOURCES:
        feed_url = _RSS_FEEDS.get(source)
        if not feed_url:
            continue
        feed = await asyncio.to_thread(feedparser.parse, feed_url)
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            raw_summary = (entry.get("summary") or entry.get("description") or "").strip()
            clean_summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_summary))).strip()
            topic_text = f"{title} {clean_summary}".lower()

            if not title or not link:
                continue
            if not _matches_topic(topic_text):
                continue
            if link in seen_links:
                continue

            seen_links.add(link)
            items.append(
                {
                    "title": title,
                    "url": link,
                    "source": source,
                    "summary_source": clean_summary[:1200],
                }
            )
            if len(items) >= 5:
                break
        if len(items) >= 5:
            break

    if not items:
        async with get_db() as db:
            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                ("news_fetch_completed", "count=0"),
            )
            await db.commit()
        try:
            await log_event(
                agent_name="NewsAgent",
                event_type="warning",
                summary="ไม่พบข่าว AI/Tech ที่เข้าเงื่อนไข",
                tags=["news", "warning"],
                result="success",
            )
        except Exception:
            pass
        return "📌 วันนี้ยังไม่พบข่าว AI/Tech ที่เข้าเงื่อนไข"

    try:
        async with get_db() as db:
            for item in items:
                prompt = (
                    f"หัวข้อ: {item['title']}\n"
                    f"แหล่งข่าว: {item['source']}\n"
                    f"ลิงก์: {item['url']}\n"
                    f"เนื้อหา: {item['summary_source']}\n\n"
                    f"{agent_memory}"
                )
                try:
                    ai_result = await chat_json(
                        prompt,
                        system=SUMMARY_SYSTEM,
                        agent="news",
                        preferred_model="groq",
                        strict_model=True,
                    )
                    title_th = _clean_text(ai_result.get("title_th")) or item["title"]
                    summary = _clean_text(ai_result.get("summary")) or item["title"]
                    apply_to = ai_result.get("apply_to", {}) or {}
                    if not isinstance(apply_to, dict):
                        apply_to = {}
                    action = _clean_text(ai_result.get("action"))
                    priority = str(ai_result.get("priority", "low")).strip().lower()
                    if priority not in _PRIORITY_EMOJI:
                        priority = "low"
                except Exception:
                    title_th = item["title"]
                    summary = item["title"]
                    apply_to = {}
                    action = None
                    priority = "low"

                item["title_th"] = title_th
                item["summary"] = summary
                item["apply_to"] = {
                    "ener_ai": _clean_text(apply_to.get("ener_ai")),
                    "ener_scan": _clean_text(apply_to.get("ener_scan")),
                    "content": _clean_text(apply_to.get("content")),
                    "it_work": _clean_text(apply_to.get("it_work")),
                }
                item["action"] = action
                item["priority"] = priority

                relevance_parts = []
                for label, value in [
                    ("Ener-AI", item["apply_to"]["ener_ai"]),
                    ("Ener Scan", item["apply_to"]["ener_scan"]),
                    ("Content", item["apply_to"]["content"]),
                    ("IT งาน", item["apply_to"]["it_work"]),
                ]:
                    if value:
                        relevance_parts.append(f"{label}: {value}")
                relevance = " | ".join(relevance_parts) or "ไม่เกี่ยวกับกบโดยตรง"

                await db.execute(
                    """
                    INSERT INTO news_items (title, url, source, summary, relevance)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (item["title_th"], item["url"], item["source"], summary, relevance),
                )
                today = datetime.now(_BANGKOK).date().isoformat()
                await db.execute(
                    "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                    (today, "news", f"[{item['priority']}] [{item['source']}] {item['title_th']}"),
                )

            await db.execute(
                "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
                ("news_fetch_completed", f"count={len(items)}"),
            )
            await db.commit()
    except Exception as exc:
        try:
            await log_event(
                agent_name="NewsAgent",
                event_type="task_failed",
                summary="สรุปข่าวล้มเหลว",
                tags=["news", "error"],
                result="failure",
                learned=str(exc)[:200],
            )
        except Exception:
            pass
        raise

    lines = [f"📌 ข่าว AI/Tech วันนี้ {len(items)} เรื่อง", "", "📰 ข่าวเด่นวันนี้"]
    high_priority_items = [item for item in items if item.get("priority") == "high"]
    if high_priority_items:
        lines.extend(["", "🔴 ข่าวที่ควรดูเป็นพิเศษ"])
        for item in high_priority_items:
            action_text = f" → {item['action']}" if item.get("action") else ""
            lines.append(f"· {item['title_th']}{action_text}")

    for index, item in enumerate(items, start=1):
        priority_emoji = _PRIORITY_EMOJI.get(item.get("priority", "low"), "🟢")
        lines.extend(
            [
                "",
                f"{index}️⃣ {item['title_th']} {priority_emoji}",
                f"   {item['summary']}",
                "",
                "   🔧 ใช้กับระบบได้:",
            ]
        )
        apply_lines = [
            _format_apply_line("Ener-AI", item["apply_to"].get("ener_ai")),
            _format_apply_line("Ener Scan", item["apply_to"].get("ener_scan")),
            _format_apply_line("Content", item["apply_to"].get("content")),
            _format_apply_line("IT งาน", item["apply_to"].get("it_work")),
        ]
        emitted = False
        for apply_line in apply_lines:
            if apply_line:
                emitted = True
                lines.append(apply_line)
        if not emitted:
            lines.append("   · ยังไม่เห็นมุมใช้ต่อที่ชัดเจน")
        if item.get("action"):
            lines.extend(["", f"   ✅ ทำต่อ: {item['action']}"])
        lines.append(f"   🔗 {item['source']}")

    result_text = "\n".join(lines)
    try:
        await log_event(
            agent_name="NewsAgent",
            event_type="insight",
            summary=f"สรุปข่าว {len(items)} เรื่อง",
            tags=["news", "ai", "tech", "deep-analysis"],
            context=result_text[:400],
            result="success",
        )
    except Exception:
        pass
    return result_text
