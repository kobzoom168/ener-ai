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
    "krebsonsecurity.com": "https://krebsonsecurity.com/feed/",
    "bleepingcomputer.com": "https://www.bleepingcomputer.com/feed/",
    "therecord.media": "https://therecord.media/feed",
    "darkreading.com": "https://www.darkreading.com/rss.xml",
    "thedebrief.org": "https://thedebrief.org/feed/",
    "mysteriousuniverse.org": "https://mysteriousuniverse.org/feed/",
    "ancient-origins.net": "https://www.ancient-origins.net/rss.xml",
    "unexplained-mysteries.com": "https://www.unexplained-mysteries.com/rss.php",
    "theblackvault.com": "https://www.theblackvault.com/documentdb/feed/",
    "producthunt.com": "https://www.producthunt.com/feed",
    "venturebeat.com": "https://venturebeat.com/feed/",
    "news.ycombinator.com": "https://hnrss.org/frontpage",
    "techsauce.co": "https://techsauce.co/feed",
    "blognone.com": "https://www.blognone.com/node/feed",
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
    "dark web",
    "darkweb",
    "ransomware",
    "breach",
    "malware",
    "hacker",
    "leaked",
    "cybercrime",
    "threat",
    "vulnerability",
    "exploit",
    "phishing",
    "zero-day",
    "botnet",
    "data leak",
    "credential",
    "hospital",
    "healthcare",
    "medical",
    "patient data",
    "health system",
    "clinic",
    "ufo",
    "uap",
    "alien",
    "extraterrestrial",
    "paranormal",
    "ghost",
    "haunted",
    "supernatural",
    "mystery",
    "unexplained",
    "ancient",
    "archaeology",
    "pyramid",
    "secret",
    "conspiracy",
    "phenomenon",
    "anomaly",
    "cryptid",
    "bigfoot",
    "bermuda",
    "energy",
    "spiritual",
    "psychic",
    "dimension",
    "meteor",
    "asteroid",
    "solar flare",
    "earthquake",
    "disaster",
    "catastrophe",
    "apocalypse",
    "launch",
    "product hunt",
    "new tool",
    "free tool",
    "open source",
    "release",
    "beta",
    "saas",
    "no-code",
    "automation tool",
    "workflow",
    "funding",
    "revenue",
    "profitable",
    "solopreneur",
    "indie",
    "bootstrapped",
    "business model",
    "monetize",
    "side project",
    "ไทย",
    "thailand",
    "เอเชีย",
    "asean",
]
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
_CATEGORY_LABELS = {
    "tools": "🛠️ AI Tools น่าลอง",
    "business": "💼 ธุรกิจน่าสนใจ",
    "ai": "🤖 AI/Tech",
    "security": "🔐 Security",
    "mystery": "👽 Mystery/UFO",
    "world": "🌍 โลก/ภัยพิบัติ",
}
_CATEGORY_ORDER = ["tools", "business", "ai", "security", "mystery", "world"]
_AI_TOOLS_KEYWORDS = [
    "launch",
    "product hunt",
    "new tool",
    "free tool",
    "open source",
    "release",
    "beta",
    "saas",
    "no-code",
    "automation tool",
    "workflow",
]
_BUSINESS_KEYWORDS = [
    "startup",
    "funding",
    "revenue",
    "profitable",
    "solopreneur",
    "indie",
    "bootstrapped",
    "business model",
    "monetize",
    "side project",
    "thailand",
    "asean",
    "ไทย",
    "เอเชีย",
]
_SECURITY_KEYWORDS = [
    "breach",
    "ransomware",
    "hack",
    "hacker",
    "malware",
    "phishing",
    "exploit",
    "vulnerability",
    "zero-day",
    "cybercrime",
    "data leak",
    "credential",
]
_MYSTERY_KEYWORDS = [
    "ufo",
    "uap",
    "alien",
    "extraterrestrial",
    "paranormal",
    "ghost",
    "haunted",
    "supernatural",
    "mystery",
    "unexplained",
    "conspiracy",
    "cryptid",
    "bigfoot",
    "psychic",
    "dimension",
]
_WORLD_KEYWORDS = [
    "earthquake",
    "disaster",
    "catastrophe",
    "meteor",
    "asteroid",
    "solar flare",
    "apocalypse",
]
SUMMARY_SYSTEM = build_system_prompt("""คุณเป็น AI วิเคราะห์ข่าวสำหรับกบ

บริบทของกบ:
- IT PM ดูแลระบบ infra โรงพยาบาล
- มี Ener-AI (personal AI assistant)
- มี Ener Scan (สแกนพลังงานพระเครื่อง LINE bot)
- ขายพระผ่าน TikTok/YouTube/Facebook
- กำลังสร้าง content และ automation system

บริบทของกบเพิ่มเติม:
- ดูแลระบบ IT infra โรงพยาบาล → ข่าว hospital/healthcare breach สำคัญมาก
- มีระบบ Ener-AI อยู่บน server → ต้องระวัง vulnerability
- กบสนใจด้านจิตวิญญาณ พลังงาน และปรากฏการณ์ลึกลับ
- มี Ener Scan ที่วิเคราะห์พลังงานพระเครื่อง

วิเคราะห์ข่าวนี้แล้วตอบ JSON:
{
  "title_th": "หัวข้อภาษาไทยกระชับ",
  "summary": "สรุปเนื้อหา 2-3 ประโยค เข้าใจง่าย",
  "apply_to": {
    "ener_ai": "นำไปใช้กับ Ener-AI ได้ยังไง (ถ้าได้)",
    "ener_scan": "เชื่อมกับ Ener Scan / พลังงาน / จิตวิญญาณได้ไหม",
    "content": "ทำ content จากข่าวนี้ได้ไหม hook คืออะไร",
    "content_moo": "ทำ content สายมู / ลึกลับได้ไหม hook คืออะไร",
    "it_work": "เกี่ยวกับงาน IT โรงพยาบาลไหม",
    "security": "กระทบ server/ระบบกบไหม ต้องทำอะไรไหม",
    "try_now": "น่าลองใช้ทันทีไหม ลองแบบไหนได้บ้าง",
    "business_idea": "เอาไอเดียนี้มาทำธุรกิจกับ Ener Scan ได้ไหม"
  },
  "action": "สิ่งที่กบควรทำต่อ 1 อย่าง (ถ้ามี)",
  "priority": "high|medium|low"
}

ถ้าไม่เกี่ยวกับกบเลย → priority: low และ apply_to ทุก field เป็น null
ถ้าข่าว hospital/healthcare breach → priority ต้องเป็น high""")


def _matches_topic(topic_text: str) -> bool:
    for keyword in _TOPIC_KEYWORDS:
        if " " in keyword:
            if keyword in topic_text:
                return True
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", topic_text):
            return True
    return False


def _keyword_score(topic_text: str) -> int:
    score = 0
    for keyword in _TOPIC_KEYWORDS:
        if " " in keyword:
            if keyword in topic_text:
                score += 2
            continue
        if re.search(rf"\b{re.escape(keyword)}\b", topic_text):
            score += 1
    if any(word in topic_text for word in ["hospital", "healthcare", "medical", "patient data", "clinic"]):
        score += 5
    if any(word in topic_text for word in ["breach", "ransomware", "malware", "phishing", "zero-day", "vulnerability"]):
        score += 3
    if any(word in topic_text for word in _MYSTERY_KEYWORDS):
        score += 3
    if any(word in topic_text for word in _WORLD_KEYWORDS):
        score += 2
    if any(word in topic_text for word in _AI_TOOLS_KEYWORDS):
        score += 4
    if any(word in topic_text for word in _BUSINESS_KEYWORDS):
        score += 3
    return score


def _is_hospital_security_story(topic_text: str) -> bool:
    has_hospital = any(word in topic_text for word in ["hospital", "healthcare", "medical", "patient data", "health system", "clinic"])
    has_incident = any(word in topic_text for word in ["breach", "ransomware", "malware", "phishing", "exploit", "vulnerability", "leaked", "data leak"])
    return has_hospital and has_incident


def _detect_category(topic_text: str) -> str:
    if any(word in topic_text for word in _AI_TOOLS_KEYWORDS):
        return "tools"
    if any(word in topic_text for word in _SECURITY_KEYWORDS):
        return "security"
    if any(word in topic_text for word in _MYSTERY_KEYWORDS):
        return "mystery"
    if any(word in topic_text for word in _WORLD_KEYWORDS):
        return "world"
    if any(word in topic_text for word in _BUSINESS_KEYWORDS):
        return "business"
    return "ai"


def _mystery_emoji(topic_text: str) -> str:
    if any(word in topic_text for word in ["ufo", "uap", "alien", "extraterrestrial"]):
        return "👽"
    if any(word in topic_text for word in ["ghost", "haunted", "spiritual", "psychic", "paranormal"]):
        return "🔮"
    return "🌀"


def _category_bonus(category: str, topic_text: str) -> int:
    bonus = {
        "tools": 5,
        "business": 4,
        "security": 4,
        "mystery": 3,
        "world": 2,
        "ai": 1,
    }.get(category, 0)
    if category == "security" and _is_hospital_security_story(topic_text):
        bonus += 5
    return bonus


def _extract_upvotes(entry: object) -> int | None:
    if not hasattr(entry, "get"):
        return None
    for key in ["votes", "upvotes", "score", "points"]:
        raw = entry.get(key)
        if raw is None:
            continue
        text = str(raw).strip().replace(",", "")
        if text.isdigit():
            return int(text)
    raw_text = " ".join(
        str(part or "")
        for part in [
            entry.get("summary"),
            entry.get("description"),
            entry.get("title"),
        ]
    )
    match = re.search(r"(\d[\d,]*)\s*(?:upvotes?|votes?|points?)", raw_text, re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _pick_top_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    per_category_limit = 3
    total_limit = 8
    counts = {category: 0 for category in _CATEGORY_ORDER}

    ranked = sorted(
        items,
        key=lambda item: (
            1 if _is_hospital_security_story(item["topic_text"]) else 0,
            item.get("match_score", 0) + _category_bonus(item.get("category", "ai"), item["topic_text"]),
        ),
        reverse=True,
    )

    for item in ranked:
        category = item.get("category", "ai")
        if counts.get(category, 0) >= per_category_limit:
            continue
        selected.append(item)
        counts[category] = counts.get(category, 0) + 1
        if len(selected) >= total_limit:
            break

    return selected


def _format_item_title(item: dict[str, str], index: int) -> str:
    prefix = ""
    if item.get("category") == "mystery":
        prefix = f"{_mystery_emoji(item['topic_text'])} "
    return f"{index}️⃣ {prefix}{item['title_th']} {_PRIORITY_EMOJI.get(item.get('priority', 'low'), '🟢')}"


def _format_news_message(items: list[dict[str, str]]) -> str:
    lines = [f"📌 ข่าววันนี้ {len(items)} เรื่อง"]

    high_priority_items = [item for item in items if item.get("priority") == "high"]
    if high_priority_items:
        lines.extend(["", "🔴 ข่าวที่ควรดูเป็นพิเศษ"])
        for item in high_priority_items:
            action_text = f" → {item['action']}" if item.get("action") else ""
            lines.append(f"· {item['title_th']}{action_text}")

    grouped = {category: [] for category in _CATEGORY_ORDER}
    for item in items:
        grouped.setdefault(item.get("category", "ai"), []).append(item)

    for category in _CATEGORY_ORDER:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        lines.extend(["", f"{_CATEGORY_LABELS[category]} ({len(category_items)} เรื่อง)"])
        for index, item in enumerate(category_items, start=1):
            lines.extend(
                [
                    "",
                    _format_item_title(item, index),
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
            if item["apply_to"].get("try_now"):
                emitted = True
                lines.append(f"   🎮 ลองได้เลย: {item['apply_to']['try_now']}")
            if item["apply_to"].get("business_idea"):
                emitted = True
                lines.append(f"   💡 ไอเดียธุรกิจ: {item['apply_to']['business_idea']}")
            if item["apply_to"].get("content_moo"):
                emitted = True
                lines.append(f"   🔮 Content มู: {item['apply_to']['content_moo']}")
            if item["apply_to"].get("security"):
                emitted = True
                lines.append(f"   🔐 Security: {item['apply_to']['security']}")
            if not emitted:
                lines.append("   · ยังไม่เห็นมุมใช้ต่อที่ชัดเจน")
            if item.get("action"):
                lines.extend(["", f"   ✅ ทำต่อ: {item['action']}"])
            lines.append(f"   🔗 {item['source']}")

    return "\n".join(lines)


async def _parse_feed(feed_url: str):
    try:
        return await asyncio.wait_for(asyncio.to_thread(feedparser.parse, feed_url), timeout=10)
    except (TimeoutError, Exception):
        return None


def _clean_text(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _format_apply_line(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"   · {label}: {value}"


@log_agent_run("NewsAgent", triggered_by="scheduler")
async def fetch_and_summarize() -> str:
    agent_memory = await get_agent_context("NewsAgent", ["news", "ai", "tech", "security", "mystery", "tools", "business"])
    items: list[dict[str, str]] = []
    seen_links: set[str] = set()

    feed_jobs = []
    ordered_sources = []
    for source in ALLOWED_NEWS_SOURCES:
        feed_url = _RSS_FEEDS.get(source)
        if not feed_url:
            continue
        ordered_sources.append(source)
        feed_jobs.append(_parse_feed(feed_url))

    feeds = await asyncio.gather(*feed_jobs, return_exceptions=True)

    for source, feed in zip(ordered_sources, feeds):
        if feed is None or isinstance(feed, Exception):
            continue
        entries = list(getattr(feed, "entries", []))
        if source == "news.ycombinator.com":
            entries = entries[:20]
        for entry in entries:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            raw_summary = (entry.get("summary") or entry.get("description") or "").strip()
            clean_summary = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw_summary))).strip()
            topic_text = f"{title} {clean_summary}".lower()

            if not title or not link:
                continue
            if source == "producthunt.com":
                upvotes = _extract_upvotes(entry)
                if upvotes is not None and upvotes <= 100:
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
                    "topic_text": topic_text,
                    "match_score": _keyword_score(topic_text),
                    "category": _detect_category(topic_text),
                }
            )

    items = _pick_top_items(items)

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
                summary="ไม่พบข่าวที่เข้าเงื่อนไข",
                tags=["news", "warning"],
                result="success",
            )
        except Exception:
            pass
        return "📌 วันนี้ยังไม่พบข่าวที่เข้าเงื่อนไข"

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

                if _is_hospital_security_story(item["topic_text"]):
                    priority = "high"

                item["title_th"] = title_th
                item["summary"] = summary
                item["apply_to"] = {
                    "ener_ai": _clean_text(apply_to.get("ener_ai")),
                    "ener_scan": _clean_text(apply_to.get("ener_scan")),
                    "content": _clean_text(apply_to.get("content")),
                    "content_moo": _clean_text(apply_to.get("content_moo")),
                    "it_work": _clean_text(apply_to.get("it_work")),
                    "security": _clean_text(apply_to.get("security")),
                    "try_now": _clean_text(apply_to.get("try_now")),
                    "business_idea": _clean_text(apply_to.get("business_idea")),
                }
                item["action"] = action
                item["priority"] = priority

                relevance_parts = []
                for label, value in [
                    ("Ener-AI", item["apply_to"]["ener_ai"]),
                    ("Ener Scan", item["apply_to"]["ener_scan"]),
                    ("Content", item["apply_to"]["content"]),
                    ("Content มู", item["apply_to"]["content_moo"]),
                    ("IT งาน", item["apply_to"]["it_work"]),
                    ("Security", item["apply_to"]["security"]),
                    ("Try Now", item["apply_to"]["try_now"]),
                    ("Business Idea", item["apply_to"]["business_idea"]),
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

    result_text = _format_news_message(items)
    try:
        await log_event(
            agent_name="NewsAgent",
            event_type="insight",
            summary=f"สรุปข่าว {len(items)} เรื่อง",
            tags=["news", "ai", "tech", "security", "mystery", "tools", "business", "deep-analysis"],
            context=result_text[:400],
            result="success",
        )
    except Exception:
        pass
    return result_text
