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


_BAD_FILE = ("logo", "icon", "wheel", "commons", "wikidata", "ambox", "question",
             "flag of", "map of", "edit", "disambig", "symbol", ".svg")


def _good_file(name: str) -> bool:
    n = name.lower()
    if not n.split("?")[0].endswith(_IMG_EXT):
        return False
    return not any(b in n for b in _BAD_FILE)


async def _page_images_list(c: httpx.AsyncClient, api: str, title: str, lang: str,
                            subject: str) -> dict | None:
    """When PageImages has no designated lead image, pick a real photo from the article's
    file list (prefer a filename that contains the subject, e.g. 'พระสมเด็จ 14.1.jpg')."""
    r = await c.get(api, params={"action": "query", "prop": "images", "titles": title,
                                 "imlimit": 30, "redirects": 1, "format": "json", "origin": "*"})
    if r.status_code >= 300:
        return None
    files = []
    for p in ((r.json().get("query") or {}).get("pages") or {}).values():
        if "missing" in p:
            return None
        files = [im.get("title", "") for im in (p.get("images") or []) if _good_file(im.get("title", ""))]
    if not files:
        return None
    keys = [w for w in subject.replace("วัด", " ").split() if len(w) >= 3]
    files.sort(key=lambda f: 0 if any(k in f for k in keys) else 1)  # subject-matching file first
    info = await c.get(api, params={"action": "query", "titles": files[0], "prop": "imageinfo",
                                    "iiprop": "url", "iiurlwidth": 1080, "format": "json", "origin": "*"})
    if info.status_code >= 300:
        return None
    for p in ((info.json().get("query") or {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        url = ii.get("thumburl") or ii.get("url")
        if url:
            return {"url": url, "source": f"https://{lang}.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    "credit": f"Wikipedia ({lang}): {title}"}
    return None


async def _wiki_page_image(subject: str, lang: str) -> dict | None:
    """Image of the Wikipedia article. EXACT title first (accurate — 'พระสมเด็จวัดระฆัง'
    returns the amulet, not the temple): try the designated lead image, then any real photo
    in the article, then fall back to a relevance search."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    base = {"action": "query", "prop": "pageimages|info", "piprop": "original",
            "inprop": "url", "redirects": 1, "format": "json", "origin": "*"}
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
        r = await c.get(api, params={**base, "titles": subject})  # 1) exact lead image
        if r.status_code < 300:
            hit = _pick_page_image(r.json(), lang, subject)
            if hit:
                return hit
        hit = await _page_images_list(c, api, subject, lang, subject)  # 2) any photo in the article
        if hit:
            return hit
        r = await c.get(api, params={**base, "generator": "search",  # 3) search fallback
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


def _pick_extract(resp_json: dict, lang: str, subject: str) -> dict | None:
    for p in sorted(((resp_json.get("query") or {}).get("pages") or {}).values(),
                    key=lambda x: x.get("index", 99)):
        if "missing" in p:
            continue
        txt = (p.get("extract") or "").strip()
        if len(txt) >= 80:
            return {"text": txt[:3000], "source": p.get("fullurl", ""),
                    "title": p.get("title", subject)}
    return None


async def fetch_article(subject: str, lang: str = "th") -> dict | None:
    """Plain-text intro of the Wikipedia article for `subject` + its URL — used to GROUND the
    script in real data and to cite the source. EXACT title first, then search. None if absent."""
    subject = (subject or "").strip()
    if not subject:
        return None
    api = f"https://{lang}.wikipedia.org/w/api.php"
    base = {"action": "query", "prop": "extracts|info", "explaintext": 1, "exintro": 1,
            "inprop": "url", "redirects": 1, "format": "json", "origin": "*"}
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
            r = await c.get(api, params={**base, "titles": subject})
            if r.status_code < 300:
                hit = _pick_extract(r.json(), lang, subject)
                if hit:
                    return hit
            r = await c.get(api, params={**base, "generator": "search",
                                         "gsrsearch": subject, "gsrlimit": 1})
            if r.status_code < 300:
                return _pick_extract(r.json(), lang, subject)
    except Exception:
        return None
    return None


async def image_ok(url: str) -> bool:
    """Verify an image URL ACTUALLY returns valid image bytes (so we never list/use a dead
    link). Small range probe = fast."""
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA},
                                     follow_redirects=True) as c:
            r = await c.get(url, headers={"Range": "bytes=0-4096"})
        if r.status_code not in (200, 206):
            return False
        return r.headers.get("content-type", "").startswith("image/") and len(r.content) > 500
    except Exception:
        return False


async def category_members(category: str, lang: str = "th", limit: int = 200) -> list[str]:
    """List the article titles in a Wikipedia category (main namespace only). This gives a
    real, verified catalog of subjects (e.g. หมวดหมู่:พระเครื่อง) — each guaranteed to have a
    Wikipedia page → real data + usually an image."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    out: list[str] = []
    cont = None
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": _UA}) as c:
            for _ in range(4):
                p = {"action": "query", "list": "categorymembers", "cmtitle": category,
                     "cmlimit": 200, "cmnamespace": 0, "cmtype": "page", "format": "json", "origin": "*"}
                if cont:
                    p["cmcontinue"] = cont
                r = await c.get(api, params=p)
                if r.status_code >= 300:
                    break
                j = r.json()
                out += [m.get("title", "") for m in (j.get("query") or {}).get("categorymembers", [])]
                cont = (j.get("continue") or {}).get("cmcontinue")
                if not cont or len(out) >= limit:
                    break
    except Exception:
        return out
    return [t for t in out if t][:limit]


async def catalog(categories: list[str], lang: str = "th") -> list[str]:
    """Merge several categories into one deduped subject list."""
    seen, out = set(), []
    for cat in categories:
        for t in await category_members(cat, lang):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


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
