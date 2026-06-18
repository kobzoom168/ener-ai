"""Find a REAL, legally-usable image for a subject from Wikipedia / Wikimedia Commons.

Used by the image-first pipeline: we only write a script for a subject when a real image
actually exists. Everything here is CC / public-domain with attribution, so clips stay legal
(no scraping collector sites). Fail-open: returns None when nothing suitable is found.

Returns: {"url": <direct image url>, "source": <human page url>, "credit": <attribution>}.
"""
from __future__ import annotations

import os

import httpx

# Wikimedia requires a descriptive User-Agent or it may block the request.
_UA = "EnerScanBot/1.0 (https://my-ener.uk; ener auto-content)"
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _pick_page_image(resp_json: dict, lang: str, subject: str) -> dict | None:
    pages = (resp_json.get("query") or {}).get("pages") or {}
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        if "missing" in p:
            continue
        src = (p.get("original") or {}).get("source")
        if src and src.lower().split("?")[0].endswith(_IMG_EXT):
            return {"url": src, "source": p.get("fullurl", ""),
                    "credit": f"Wikipedia ({lang}): {p.get('title', subject)}"}
    return None


async def _wiki_page_image(subject: str, lang: str) -> dict | None:
    """Lead image of the Wikipedia article. Tries the EXACT title first (accurate — e.g.
    'พระสมเด็จวัดระฆัง' returns the amulet, not the temple), then falls back to search."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    base = {"action": "query", "prop": "pageimages|info", "piprop": "original",
            "inprop": "url", "redirects": 1, "format": "json", "origin": "*"}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
        # 1) exact-title match (most accurate)
        r = await c.get(api, params={**base, "titles": subject})
        if r.status_code < 300:
            hit = _pick_page_image(r.json(), lang, subject)
            if hit:
                return hit
        # 2) fall back to a relevance search
        r = await c.get(api, params={**base, "generator": "search",
                                     "gsrsearch": subject, "gsrlimit": 3})
        if r.status_code < 300:
            return _pick_page_image(r.json(), lang, subject)
    return None


async def _commons_search(subject: str) -> dict | None:
    """Search Wikimedia Commons (the media repo) directly for a file matching `subject`."""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "generator": "search", "gsrsearch": subject,
        "gsrnamespace": 6, "gsrlimit": 8, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": 1080, "format": "json", "origin": "*",
    }
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
        r = await c.get(api, params=params)
    if r.status_code >= 300:
        return None
    pages = (r.json().get("query") or {}).get("pages") or {}
    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        info = (p.get("imageinfo") or [{}])[0]
        url = info.get("url") or ""
        if not url or not url.lower().split("?")[0].endswith(_IMG_EXT):
            continue
        meta = info.get("extmetadata") or {}
        artist = (meta.get("Artist") or {}).get("value", "")
        # strip any html the artist field may carry
        import re
        artist = re.sub("<[^>]+>", "", artist).strip()[:80] or "Wikimedia Commons"
        return {"url": info.get("thumburl") or url,
                "source": info.get("descriptionurl", ""),
                "credit": f"Wikimedia Commons: {artist}"}
    return None


async def find_image(subject: str) -> dict | None:
    """Best real image for `subject`: Thai Wikipedia → English Wikipedia → Commons. None if
    nothing legal/usable exists (the caller then SKIPS this subject — image-first rule)."""
    subject = (subject or "").strip()
    if not subject:
        return None
    for finder in (lambda: _wiki_page_image(subject, "th"),
                   lambda: _wiki_page_image(subject, "en"),
                   lambda: _commons_search(subject)):
        try:
            hit = await finder()
        except Exception:
            hit = None
        if hit and hit.get("url"):
            return hit
    return None


async def download(url: str, out_path: str) -> str | None:
    """Download a found image to out_path. Fail-open → None."""
    if not url:
        return None
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        async with httpx.AsyncClient(timeout=60, headers={"User-Agent": _UA},
                                     follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code >= 300 or not r.content:
                return None
        with open(out_path, "wb") as fh:
            fh.write(r.content)
        return out_path if os.path.getsize(out_path) > 3000 else None
    except Exception:
        return None
