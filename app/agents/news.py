import asyncio
import html
import re
import feedparser
from datetime import date as _date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.config import settings
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

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
SUMMARY_SYSTEM = build_system_prompt("""งานของพี่ตอนนี้: สรุปข่าวเป็นภาษาไทย แล้วตอบ JSON เท่านั้น

กฎการแปลหัวข้อ (title_th):
- แปลให้เป็นประโยคภาษาไทยที่อ่านรู้เรื่องทันที
- ใช้ตัวเลขและหน่วยแบบสากล เช่น $401B, 1,100 คน, 167 ช่องโหว่
- ห้ามแปลตรงตัวแบบ word-for-word
- ห้ามใช้ทับศัพท์โดยไม่จำเป็น ถ้ามีคำไทยที่ดีกว่า ให้ใช้
- ความยาวไม่เกิน 60 ตัวอักษร
- ตัวอย่างที่ดี: "บริษัทสูญเสีย $401B จาก GPU ที่ซื้อมาแล้วไม่ได้ใช้"
- ตัวอย่างที่ไม่ดี: "ปัญหา $401 พันล้านกับอุปกรณ์ AI ที่ไม่ได้ใช้"

กฎการสรุปเนื้อหา (summary_th):
- 1 ประโยค อธิบายว่าเกิดอะไรขึ้นและส่งผลอย่างไร
- ใช้ภาษาไทยกระชับ เป็นธรรมชาติ ไม่เป็นทางการเกินไป

กฎ apply:
- บอกว่าใช้กับงาน IT โรงพยาบาล หรือธุรกิจพระของกบได้ยังไง
- ถ้าใช้ไม่ได้จริง บอกตรงๆ สั้นๆ

ตอบ JSON นี้เท่านั้น:
{
  "title_th": "หัวข้อไทยที่อ่านรู้เรื่อง",
  "summary_th": "สรุป 1 ประโยค",
  "apply": "ใช้กับกบได้ยังไง"
}""")


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


# Telegram digest focuses on AI / IT-dev / Thai-business / security (tech-adjacent).
# Mystery (UAP/ancient) and generic world news are excluded — they were the off-topic
# noise (Piri Reis map etc.). Edit this set to retune what the morning digest sends.
_DIGEST_CATEGORIES = {"ai", "tools", "security", "business"}


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
    GUARANTEED = {"mystery": 1, "security": 1}
    PER_CATEGORY_LIMIT = 3
    TOTAL_LIMIT = 9

    ranked = sorted(
        items,
        key=lambda item: (
            1 if _is_hospital_security_story(item["topic_text"]) else 0,
            item.get("match_score", 0) + _category_bonus(
                item.get("category", "ai"), item["topic_text"]
            ),
        ),
        reverse=True,
    )

    selected: list[dict[str, str]] = []
    counts: dict[str, int] = {category: 0 for category in _CATEGORY_ORDER}

    for category, minimum in GUARANTEED.items():
        for item in ranked:
            if item.get("category") != category:
                continue
            if counts.get(category, 0) >= minimum:
                break
            selected.append(item)
            counts[category] = counts.get(category, 0) + 1

    for item in ranked:
        if item in selected:
            continue
        category = item.get("category", "ai")
        if counts.get(category, 0) >= PER_CATEGORY_LIMIT:
            continue
        selected.append(item)
        counts[category] = counts.get(category, 0) + 1
        if len(selected) >= TOTAL_LIMIT:
            break

    return selected


def _format_item_title(item: dict[str, str], index: int) -> str:
    prefix = ""
    if item.get("category") == "mystery":
        prefix = f"{_mystery_emoji(item['topic_text'])} "
    return f"{index}️⃣ {prefix}{item['title_th']} {_PRIORITY_EMOJI.get(item.get('priority', 'low'), '🟢')}"


def _derive_priority(item: dict[str, str]) -> str:
    if _is_hospital_security_story(item["topic_text"]):
        return "high"
    if item.get("category") in {"security", "tools", "business"}:
        return "medium"
    if int(item.get("match_score", 0) or 0) >= 6:
        return "medium"
    return "low"


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
                    f"   {item['summary_th']}",
                    f"   → {item['apply']}",
                    f"   🔗 {item.get('url') or item['source']}",
                ]
            )

    return "\n".join(lines)


async def _parse_feed(feed_url: str):
    try:
        return await asyncio.wait_for(asyncio.to_thread(feedparser.parse, feed_url), timeout=10)
    except (TimeoutError, Exception):
        return None


async def _get_all_feeds() -> dict[str, str]:
    feeds = dict(_RSS_FEEDS)
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT domain, rss_url FROM approved_news_sources WHERE active = 1"
        )
        rows = await cursor.fetchall()
    for row in rows:
        domain = str(row["domain"] or "").strip()
        rss_url = str(row["rss_url"] or "").strip()
        if domain and rss_url:
            feeds[domain] = rss_url
    return feeds


def _clean_text(value: object) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text or None


def _format_apply_line(label: str, value: str | None) -> str | None:
    if not value:
        return None
    return f"   · {label}: {value}"


@log_agent_run("NewsAgent", triggered_by="scheduler")
async def fetch_and_summarize(force: bool = False, _agent_triggered_by: str = "manual") -> str:
    if not force:
        today_str = _date.today().isoformat()
        async with get_db() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) AS c
                FROM news_items
                WHERE date(datetime(fetched_at, '+7 hours')) = ?
                """,
                (today_str,),
            )
            row = await cursor.fetchone()
            today_count = row["c"] if row else 0

            if today_count >= 5:
                cursor2 = await db.execute(
                    """
                    SELECT title, url, source, summary, relevance
                    FROM news_items
                    WHERE date(datetime(fetched_at, '+7 hours')) = ?
                    ORDER BY id DESC
                    LIMIT 9
                    """,
                    (today_str,),
                )
                cached_rows = await cursor2.fetchall()
                if cached_rows:
                    cached_items: list[dict[str, str]] = []
                    for cached_row in cached_rows:
                        topic_text = (
                            f"{str(cached_row['title'] or '')} {str(cached_row['summary'] or '')}"
                        ).lower()
                        cached_item = {
                            "title_th": str(cached_row["title"] or ""),
                            "url": str(cached_row["url"] or ""),
                            "source": str(cached_row["source"] or ""),
                            "summary_th": str(cached_row["summary"] or "ไม่มีสรุป"),
                            "apply": str(cached_row["relevance"] or "ยังไม่เห็นมุมใช้ต่อ"),
                            "topic_text": topic_text,
                            "category": _detect_category(topic_text),
                        }
                        cached_item["priority"] = _derive_priority(cached_item)
                        cached_items.append(cached_item)
                    cached_items = [c for c in cached_items if c.get("category") in _DIGEST_CATEGORIES]
                    if cached_items:
                        return _format_news_message(cached_items) + "\n\n📦 (จาก cache วันนี้)"

    items: list[dict[str, str]] = []
    seen_links: set[str] = set()

    all_feeds = await _get_all_feeds()
    # Followed social accounts (X via nitter): the user explicitly follows them, so
    # their posts BYPASS the topic keyword filter (else most of e.g. @elonmusk would
    # be dropped) — but are capped per account so they don't flood the digest.
    social_sources = {s for s, u in all_feeds.items() if "nitter.net" in u}
    feed_jobs = []
    ordered_sources = []
    for source, feed_url in all_feeds.items():
        ordered_sources.append(source)
        feed_jobs.append(_parse_feed(feed_url))

    feeds = await asyncio.gather(*feed_jobs, return_exceptions=True)

    for source, feed in zip(ordered_sources, feeds):
        if feed is None or isinstance(feed, Exception):
            continue
        entries = list(getattr(feed, "entries", []))
        is_social = source in social_sources
        if source == "news.ycombinator.com":
            entries = entries[:20]
        if is_social:
            entries = entries[:4]  # cap followed-account posts so they don't flood
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
            if not is_social and not _matches_topic(topic_text):
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
                    # followed accounts get a small boost so _pick_top_items keeps them
                    "match_score": _keyword_score(topic_text) + (4 if is_social else 0),
                    "category": _detect_category(topic_text),
                    "is_social": is_social,
                }
            )

    if len(items) < 9 and settings.gemini_api_key:
        try:
            from app.core.ai import _gemini_grounded_search

            today_str = _date.today().strftime("%d %B %Y")
            search_queries = [
                f"latest AI machine learning LLM developer tools tech product news today {today_str}",
                f"ข่าว AI เทคโนโลยี IT ซอฟต์แวร์ สตาร์ทอัพไทย ความมั่นคงไซเบอร์ น่าสนใจวันนี้ {today_str}",
            ]

            for search_query in search_queries:
                gemini_result = await _gemini_grounded_search(search_query)
                if not gemini_result or "⚠️" in gemini_result:
                    continue
                for line in gemini_result.split("\n"):
                    line = line.strip()
                    if not line.startswith("🔗"):
                        continue
                    url = line.replace("🔗", "").strip()
                    if not url.startswith("http"):
                        continue
                    from urllib.parse import urlparse

                    domain = urlparse(url).netloc.replace("www.", "")
                    topic_text = gemini_result.lower()
                    if not _matches_topic(topic_text) or url in seen_links:
                        continue
                    seen_links.add(url)
                    items.append(
                        {
                            "title": f"[Gemini] {domain}",
                            "url": url,
                            "source": domain,
                            "summary_source": gemini_result[:800],
                            "topic_text": topic_text,
                            "match_score": _keyword_score(topic_text),
                            "category": _detect_category(topic_text),
                        }
                    )
        except Exception:
            pass

    # Keep only the focused tech categories for the digest (drop mystery/world noise),
    # but always keep posts from followed social accounts.
    items = [it for it in items if it.get("category") in _DIGEST_CATEGORIES or it.get("is_social")]
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
        for item in items:
            prompt = (
                f"หัวข้อ: {item['title']}\n"
                f"แหล่งข่าว: {item['source']}\n"
                f"ลิงก์: {item['url']}\n"
                f"เนื้อหา: {item['summary_source']}\n"
            )
            try:
                ai_result = await chat_json(
                    prompt,
                    system=SUMMARY_SYSTEM,
                    agent="news",
                    preferred_model="groq",
                    strict_model=True,
                )
                item["title_th"] = _clean_text(ai_result.get("title_th")) or item["title"]
                item["summary_th"] = _clean_text(ai_result.get("summary_th")) or "ไม่มีสรุป"
                item["apply"] = _clean_text(ai_result.get("apply")) or "ยังไม่เห็นมุมใช้ต่อ"
            except Exception:
                item["title_th"] = item["title"]
                item["summary_th"] = "ไม่มีสรุป"
                item["apply"] = "ยังไม่เห็นมุมใช้ต่อ"

            item["priority"] = _derive_priority(item)

        today = datetime.now(_BANGKOK).date().isoformat()
        news_rows = [
            (
                item["title_th"],
                item["url"],
                item["source"],
                item["summary_th"],
                item["apply"],
            )
            for item in items
        ]
        daily_log_rows = [
            (today, "news", f"[{item['priority']}] [{item['source']}] {item['title_th']}")
            for item in items
        ]

        async with get_db() as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO news_items (title, url, source, summary, relevance)
                VALUES (?, ?, ?, ?, ?)
                """,
                news_rows,
            )
            await db.executemany(
                "INSERT INTO daily_logs (log_date, category, content) VALUES (?, ?, ?)",
                daily_log_rows,
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
