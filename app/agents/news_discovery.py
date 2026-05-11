import asyncio
import json
import random

import feedparser

from app.agents.news import _get_all_feeds
from app.core.ai import chat_json
from app.core.agents import log_agent_run
from app.core.database import get_db
from app.core.event_log import log_event
from app.core.policy import ALLOWED_NEWS_SOURCES, build_system_prompt

DISCOVERY_SYSTEM = build_system_prompt("""คุณเป็น News Source Hunter สำหรับกบ

บริบทกบ:
- IT PM โรงพยาบาล
- Ener Scan (พระเครื่อง/จิตวิญญาณ)
- AI tools enthusiast
- ขาย content TikTok/YouTube

หาแหล่งข่าวใหม่ที่น่าสนใจ ตอบ JSON:
{
  "sources": [
    {
      "domain": "example.com",
      "rss_url": "https://example.com/feed",
      "category": "ai_tools|security|paranormal|business|thai",
      "reason": "ทำไมเหมาะกับกบ",
      "quality_score": 1-10
    }
  ]
}

กฎ:
- ต้องมี RSS feed จริง
- quality_score >= 7 เท่านั้น
- ไม่ซ้ำกับที่มีอยู่แล้ว
- ฟรี ไม่ต้อง login
""")

SEARCH_QUERIES = [
    "best AI news RSS feed 2026",
    "UFO paranormal news RSS feed",
    "cybersecurity dark web news RSS",
    "new startup business ideas RSS",
    "Thailand tech news RSS feed",
    "Buddhist amulet spiritual news RSS",
    "AI tools product launch RSS",
]


def _normalize_domain(value: object) -> str:
    return str(value or "").strip().lower()


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


async def _existing_sources() -> set[str]:
    known = {_normalize_domain(source) for source in ALLOWED_NEWS_SOURCES}
    async with get_db() as db:
        cursor = await db.execute("SELECT domain FROM approved_news_sources")
        rows = await cursor.fetchall()
    for row in rows:
        domain = _normalize_domain(row["domain"])
        if domain:
            known.add(domain)
    return known


async def _validate_rss_url(rss_url: str) -> tuple[bool, str]:
    url = str(rss_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, "RSS URL ไม่ถูกต้อง"
    try:
        feed = await asyncio.wait_for(asyncio.to_thread(feedparser.parse, url), timeout=10)
    except Exception as exc:
        return False, f"ตรวจ RSS ไม่สำเร็จ: {exc}"
    entries = list(getattr(feed, "entries", []))
    if entries:
        return True, ""
    feed_info = getattr(feed, "feed", {}) or {}
    if getattr(feed, "bozo", 0):
        return False, "RSS feed อ่านไม่ได้หรือ format ผิด"
    if feed_info.get("title"):
        return True, ""
    return False, "RSS feed นี้ไม่มีข้อมูลข่าว"


async def _save_pending_sources(sources: list[dict]) -> None:
    async with get_db() as db:
        for source in sources:
            domain = _normalize_domain(source.get("domain"))
            if not domain:
                continue
            payload = json.dumps(source, ensure_ascii=False)
            await db.execute(
                """
                INSERT INTO memories (key, value, tag)
                VALUES (?, ?, 'pending_news_source')
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    tag = 'pending_news_source',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (f"pending_source_{domain}", payload),
            )
        await db.commit()


@log_agent_run("NewsDiscoveryAgent", triggered_by="scheduler")
async def discover_new_sources(structured: bool = False) -> str | list[dict]:
    from app.core.ai import _gemini_grounded_search

    known_sources = await _existing_sources()
    queries = random.sample(SEARCH_QUERIES, k=min(3, len(SEARCH_QUERIES)))
    all_suggestions: list[dict] = []

    for query in queries:
        search_result = await _gemini_grounded_search(
            f"{query} site:feedburner.com OR inurl:/feed OR inurl:/rss"
        )

        prompt = f"""
จากผลการค้นหา Google นี้:
{search_result[:2000]}

แหล่งที่มีอยู่แล้ว (ห้ามซ้ำ): {', '.join(sorted(known_sources))}

สกัดเฉพาะแหล่งข่าวที่มี RSS feed จริงจากผลค้นหาด้านบน
ห้ามแต่งหรือเดา URL ใดๆ ใช้เฉพาะข้อมูลที่เห็นในผลค้นหาเท่านั้น
"""
        try:
            result = await chat_json(
                prompt,
                system=DISCOVERY_SYSTEM,
                agent="newsdiscovery",
                preferred_model="haiku",
                strict_model=True,
            )
        except Exception:
            continue

        suggestions = result.get("sources", [])
        if not isinstance(suggestions, list):
            continue
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            domain = _normalize_domain(suggestion.get("domain"))
            rss_url = str(suggestion.get("rss_url") or "").strip()
            quality_score = _safe_int(suggestion.get("quality_score"), 0)
            if not domain or domain in known_sources or quality_score < 7 or not rss_url:
                continue
            all_suggestions.append(
                {
                    "domain": domain,
                    "rss_url": rss_url,
                    "category": str(suggestion.get("category") or "general").strip() or "general",
                    "reason": str(suggestion.get("reason") or "").strip() or "พบจาก Google Search",
                    "quality_score": quality_score,
                }
            )

    if not all_suggestions:
        try:
            await log_event(
                agent_name="NewsDiscoveryAgent",
                event_type="warning",
                summary="ไม่พบแหล่งข่าวใหม่สัปดาห์นี้",
                tags=["news", "discovery", "warning"],
                result="success",
            )
        except Exception:
            pass
        return "ไม่พบแหล่งข่าวใหม่สัปดาห์นี้"

    seen: set[str] = set()
    unique: list[dict] = []
    for suggestion in sorted(all_suggestions, key=lambda item: _safe_int(item.get("quality_score"), 0), reverse=True):
        domain = _normalize_domain(suggestion.get("domain"))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        unique.append(suggestion)

    await _save_pending_sources(unique)

    if structured:
        return [
            {
                "domain": suggestion["domain"],
                "rss": str(suggestion.get("rss_url") or "").strip(),
                "description": str(suggestion.get("reason") or "").strip(),
                "score": _safe_int(suggestion.get("quality_score"), 0),
                "category": str(suggestion.get("category") or "general").strip() or "general",
            }
            for suggestion in unique
        ]

    category_emoji = {
        "ai_tools": "🤖",
        "security": "🔐",
        "paranormal": "👽",
        "business": "💼",
        "thai": "🇹🇭",
    }
    lines = ["🔍 พบแหล่งข่าวใหม่ที่น่าสนใจ", ""]
    for index, suggestion in enumerate(unique[:5], start=1):
        emoji = category_emoji.get(str(suggestion.get("category") or "").strip(), "📰")
        stars = max(1, min(_safe_int(suggestion.get("quality_score"), 7), 10))
        lines.append(
            f"{index}. {emoji} {suggestion['domain']}\n"
            f"   {suggestion['reason']}\n"
            f"   คะแนน: {'⭐' * stars}\n"
            f"   RSS: {suggestion.get('rss_url', 'ไม่มีข้อมูล')}"
        )
    lines.append("\nพิมพ์ /pending_sources เพื่อ approve ผ่านปุ่มได้เลย")

    result_text = "\n".join(lines)
    try:
        await log_event(
            agent_name="NewsDiscoveryAgent",
            event_type="insight",
            summary=f"พบแหล่งข่าวใหม่ {len(unique)} แหล่ง",
            tags=["news", "discovery", "rss"],
            context=result_text[:400],
            result="success",
        )
    except Exception:
        pass
    return result_text


async def get_pending_sources_data(limit: int = 10) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT key, value, updated_at
            FROM memories
            WHERE tag = 'pending_news_source'
            ORDER BY updated_at DESC
            """,
        )
        rows = await cursor.fetchall()

    items: list[dict] = []
    for row in rows[: max(1, int(limit))]:
        try:
            data = json.loads(row["value"])
        except Exception:
            continue
        domain = _normalize_domain(data.get("domain"))
        if not domain:
            continue
        items.append(
            {
                "domain": domain,
                "rss": str(data.get("rss_url") or "").strip(),
                "description": str(data.get("reason") or "-").strip() or "-",
                "score": _safe_int(data.get("quality_score"), 0),
                "category": str(data.get("category") or "general").strip() or "general",
            }
        )
    return items


@log_agent_run("NewsDiscoveryAgent")
async def approve_source(domain: str) -> str:
    clean_domain = _normalize_domain(domain)
    if not clean_domain:
        return "❌ กรุณาระบุ domain เช่น /approve_source example.com"

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT value FROM memories WHERE key = ? AND tag = 'pending_news_source'",
            (f"pending_source_{clean_domain}",),
        )
        row = await cursor.fetchone()

    if not row:
        return f"❌ ไม่พบ {clean_domain} ในรายการรอ approve"

    try:
        data = json.loads(row["value"])
    except Exception:
        return f"❌ ข้อมูลของ {clean_domain} อ่านไม่ได้"

    rss_url = str(data.get("rss_url") or "").strip()
    is_valid, error_message = await _validate_rss_url(rss_url)
    if not is_valid:
        return f"❌ เพิ่ม {clean_domain} ไม่ได้\n{error_message}\nRSS: {rss_url or '-'}"

    async with get_db() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO approved_news_sources (domain, rss_url, category, reason)
            VALUES (?, ?, ?, ?)
            """,
            (
                clean_domain,
                rss_url,
                str(data.get("category") or "general").strip() or "general",
                str(data.get("reason") or "").strip(),
            ),
        )
        await db.execute(
            "DELETE FROM memories WHERE key = ?",
            (f"pending_source_{clean_domain}",),
        )
        await db.commit()

    try:
        await log_event(
            agent_name="NewsDiscoveryAgent",
            event_type="approval",
            summary=f"อนุมัติแหล่งข่าว {clean_domain}",
            tags=["news", "discovery", "approved"],
            result="success",
        )
    except Exception:
        pass
    return (
        f"✅ เพิ่ม {clean_domain} แล้วครับ\n"
        f"RSS: {rss_url}\n"
        "จะใช้ในการดึงข่าวรอบถัดไป"
    )


@log_agent_run("NewsDiscoveryAgent")
async def list_pending_sources() -> str:
    items = await get_pending_sources_data(limit=10)
    if not items:
        return "📭 ไม่มีแหล่งข่าวที่รอ approve"

    lines = ["📭 Pending news sources", ""]
    for index, item in enumerate(items, start=1):
        lines.append(
            f"{index}. {item['domain']}\n"
            f"   หมวด: {item.get('category', 'general')}\n"
            f"   เหตุผล: {item.get('description', '-')}\n"
            f"   คะแนน: {item.get('score', 0)}/10\n"
            f"   RSS: {item.get('rss', '-')}"
        )
    return "\n".join(lines)


@log_agent_run("NewsDiscoveryAgent")
async def list_active_sources() -> str:
    feeds = await _get_all_feeds()
    hardcoded = sorted({_normalize_domain(source) for source in ALLOWED_NEWS_SOURCES})
    approved_only = sorted(domain for domain in feeds if domain not in hardcoded)

    lines = [
        f"📰 แหล่งข่าวที่ใช้อยู่ทั้งหมด {len(feeds)} แหล่ง",
        f"• Built-in: {len(hardcoded)}",
        f"• Approved เพิ่มเอง: {len(approved_only)}",
    ]

    if hardcoded:
        lines.extend(["", "Built-in sources:"])
        for domain in hardcoded:
            lines.append(f"• {domain} -> {feeds.get(domain, '-')}")

    if approved_only:
        lines.extend(["", "Approved sources เพิ่มเติม:"])
        for domain in approved_only[:20]:
            lines.append(f"• {domain} -> {feeds[domain]}")
    return "\n".join(lines)
