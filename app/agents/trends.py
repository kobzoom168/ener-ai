"""Free trend signals for the Trend Radar — no API key, all fail-open:
  • Google/YouTube autocomplete  (what people are searching RIGHT NOW)
  • Google News RSS (TH)          (current news/กระแส)
  • Google Trends daily RSS (TH)  (today's trending searches)
These raw signals are handed to an LLM (in vdo_agent.suggest_topics) which turns them into
ranked, specific-question video topics for the channel.
"""
from __future__ import annotations

import asyncio

import httpx


async def _autocomplete(seed: str, ds: str = "yt") -> list[str]:
    """Google suggest. ds='yt' = YouTube suggestions, ds='' = Google web suggestions."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                "https://suggestqueries.google.com/complete/search",
                params={"client": "firefox", "ds": ds, "hl": "th", "gl": "TH", "q": seed},
            )
        if r.status_code < 300:
            import json
            data = json.loads(r.text)
            return [str(x) for x in (data[1] if len(data) > 1 else []) if str(x).strip()]
    except Exception:
        pass
    return []


async def _rss_titles(url: str, n: int) -> list[str]:
    def _do() -> list[str]:
        import feedparser
        f = feedparser.parse(url)
        return [str(e.get("title", "")).strip() for e in (f.entries or [])[:n] if e.get("title")]
    try:
        return await asyncio.to_thread(_do)
    except Exception:
        return []


async def google_news_th(query: str, n: int = 6) -> list[str]:
    from urllib.parse import quote
    return await _rss_titles(
        f"https://news.google.com/rss/search?q={quote(query)}&hl=th&gl=TH&ceid=TH:th", n)


async def daily_trends_th(n: int = 20) -> list[str]:
    return await _rss_titles(
        "https://trends.google.com/trends/trendingsearches/daily/rss?geo=TH", n)


async def collect_signals(seeds: list[str], news_queries: list[str]) -> dict:
    """Gather autocomplete (YouTube + web) for each seed + news + today's trending searches."""
    ac_tasks = []
    for s in seeds[:8]:
        ac_tasks += [_autocomplete(s, "yt"), _autocomplete(s, "")]
    news_tasks = [google_news_th(q, 5) for q in news_queries[:4]]
    results = await asyncio.gather(*ac_tasks, *news_tasks, daily_trends_th(), return_exceptions=True)
    ac = results[:len(ac_tasks)]
    news = results[len(ac_tasks):len(ac_tasks) + len(news_tasks)]
    daily = results[-1] if not isinstance(results[-1], Exception) else []

    def _flat(xs):
        out = []
        for x in xs:
            if isinstance(x, list):
                out += x
        # dedupe, keep order
        seen, uniq = set(), []
        for s in out:
            s = str(s).strip()
            if s and s.lower() not in seen:
                seen.add(s.lower()); uniq.append(s)
        return uniq

    return {
        "autocomplete": _flat(ac)[:60],
        "news": _flat(news)[:20],
        "daily_trending": [str(s) for s in (daily or [])][:20],
    }
