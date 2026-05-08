import asyncio
import html
import re
import feedparser
from datetime import datetime
from zoneinfo import ZoneInfo
from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.policy import ALLOWED_NEWS_SOURCES

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


def _matches_topic(topic_text: str) -> bool:
    for keyword in _TOPIC_KEYWORDS:
        if " " in keyword:
            if keyword in topic_text:
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", topic_text):
            return True
    return False


@log_agent_run("NewsAgent", triggered_by="scheduler")
async def fetch_and_summarize() -> str:
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
        return "📌 วันนี้ยังไม่พบข่าว AI/Tech ที่เข้าเงื่อนไข"

    async with get_db() as db:
        for item in items:
            prompt = (
                "สรุปข่าวต่อไปนี้เป็น JSON\n"
                "{\n"
                '  "summary": "สรุป 1 บรรทัดภาษาไทย",\n'
                '  "relevance": "ข่าวนี้เกี่ยวข้องกับกบอย่างไร 1 บรรทัดภาษาไทย"\n'
                "}\n\n"
                f"หัวข้อ: {item['title']}\n"
                f"แหล่งข่าว: {item['source']}\n"
                f"ลิงก์: {item['url']}\n"
                f"เนื้อหา: {item['summary_source']}\n\n"
                "กฎ:\n"
                "- summary ต้องเป็นไทย 1 บรรทัด กระชับ ชัดเจน\n"
                "- relevance ต้องเป็นไทย 1 บรรทัด อธิบายว่าข่าวนี้น่าติดตามสำหรับกบอย่างไร\n"
                "- ห้ามตอบเกิน JSON"
            )
            try:
                ai_result = await chat_json(prompt, agent="news")
                summary = str(ai_result.get("summary", item["title"])).strip()
                relevance = str(
                    ai_result.get("relevance", "เกี่ยวข้องกับงานและความสนใจด้าน AI/เทคโนโลยีของกบ")
                ).strip()
            except Exception:
                summary = item["title"]
                relevance = "เกี่ยวข้องกับงานและความสนใจด้าน AI/เทคโนโลยีของกบ"

            item["summary"] = summary
            item["relevance"] = relevance

            await db.execute(
                """
                INSERT INTO news_items (title, url, source, summary, relevance)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item["title"], item["url"], item["source"], summary, relevance),
            )
            today = datetime.now(_BANGKOK).date().isoformat()
            await db.execute(
                "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                (today, "news", f"[{item['source']}] {summary}"),
            )

        await db.execute(
            "INSERT INTO audit_logs (action, details) VALUES (?, ?)",
            ("news_fetch_completed", f"count={len(items)}"),
        )
        await db.commit()

    lines = [f"📌 ข่าว AI/Tech วันนี้ {len(items)} เรื่อง", "", "📰 ข่าวเด่นวันนี้"]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"{index}. {item['summary']}",
                f"   เกี่ยวกับกบ: {item['relevance']}",
                f"   ที่มา: {item['source']}",
                f"   {item['url']}",
            ]
        )

    return "\n".join(lines)
