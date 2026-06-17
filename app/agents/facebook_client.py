"""Direct Facebook Page video posting via the Graph API — replaces Postiz for FB so we
don't have to run the heavy Postiz stack.

Config (env):
  FB_PAGE_ID      - the Ener Scan page id
  FB_PAGE_TOKEN   - a long-lived Page Access Token (pages_manage_posts)
  FB_API_VERSION  - Graph API version (default v21.0)
"""
from __future__ import annotations

import os

import httpx


def _cfg() -> tuple[str, str, str]:
    return (
        os.environ.get("FB_PAGE_ID", "").strip(),
        os.environ.get("FB_PAGE_TOKEN", "").strip(),
        os.environ.get("FB_API_VERSION", "v21.0").strip() or "v21.0",
    )


def enabled() -> bool:
    pid, tok, _ = _cfg()
    return bool(pid and tok)


async def check_token() -> tuple[bool, str]:
    """Verify the page token + id resolve (used by setup/debug)."""
    pid, tok, ver = _cfg()
    if not pid or not tok:
        return False, "ยังไม่ได้ตั้ง FB_PAGE_ID / FB_PAGE_TOKEN"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"https://graph.facebook.com/{ver}/{pid}",
                            params={"fields": "name,fan_count", "access_token": tok})
        if r.status_code >= 300:
            return False, f"{r.status_code}: {r.text[:200]}"
        d = r.json()
        return True, f"{d.get('name')} ({d.get('fan_count', '?')} likes)"
    except Exception as exc:
        return False, str(exc)[:200]


# ───────────────────────── one-click Connect (OAuth) ─────────────────────────
# Lets the user connect a Page by clicking a button (no Graph API Explorer). We exchange
# the consent code → long-lived user token → long-lived Page token (Page tokens minted from
# a long-lived user token don't expire), then store it as FB_PAGE_ID / FB_PAGE_TOKEN.
OAUTH_SCOPES = "pages_show_list,pages_manage_posts,pages_read_engagement"


async def _oauth_cfg() -> tuple[str, str, str]:
    """(app_id, app_secret, redirect_uri). Env overrides DB config."""
    from app.core.database import get_config
    aid = os.environ.get("FACEBOOK_APP_ID", "").strip() or (await get_config("facebook_app_id", "")).strip()
    asec = os.environ.get("FACEBOOK_APP_SECRET", "").strip() or (await get_config("facebook_app_secret", "")).strip()
    redir = os.environ.get("FACEBOOK_REDIRECT_URI", "").strip() or (await get_config("facebook_redirect_uri", "")).strip()
    return aid, asec, redir


async def configured_oauth() -> bool:
    aid, asec, _ = await _oauth_cfg()
    return bool(aid and asec)


async def oauth_url(redirect_uri: str, state: str = "") -> str:
    aid, asec, _ = await _oauth_cfg()
    if not aid or not asec:
        raise RuntimeError("ยังไม่ได้ตั้ง Facebook App ID/Secret")
    ver = os.environ.get("FB_API_VERSION", "v21.0").strip() or "v21.0"
    from app.core.database import get_config
    config_id = (await get_config("facebook_config_id", "")).strip()
    params = {"client_id": aid, "redirect_uri": redirect_uri, "state": state or "ener",
              "response_type": "code"}
    if config_id:
        # "Facebook Login for Business" apps reject a raw scope= and require a Configuration
        # (a saved permission/asset set) referenced by config_id instead.
        params["config_id"] = config_id
    else:
        params["scope"] = OAUTH_SCOPES
    from urllib.parse import urlencode
    return f"https://www.facebook.com/{ver}/dialog/oauth?{urlencode(params)}"


async def fetch_pages(code: str, redirect_uri: str) -> list[dict]:
    """Exchange code → long-lived user token → the user's Pages (each with its own token).
    Returns [{id, name, access_token}]. Raises on failure."""
    aid, asec, _ = await _oauth_cfg()
    ver = os.environ.get("FB_API_VERSION", "v21.0").strip() or "v21.0"
    base = f"https://graph.facebook.com/{ver}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{base}/oauth/access_token",
                        params={"client_id": aid, "client_secret": asec,
                                "redirect_uri": redirect_uri, "code": code})
        d = r.json()
        if r.status_code >= 300 or not d.get("access_token"):
            raise RuntimeError(f"แลก code ไม่สำเร็จ: {str(d)[:200]}")
        short_user = d["access_token"]
        # short-lived → long-lived user token (~60 days)
        r = await c.get(f"{base}/oauth/access_token",
                        params={"grant_type": "fb_exchange_token", "client_id": aid,
                                "client_secret": asec, "fb_exchange_token": short_user})
        long_user = r.json().get("access_token") or short_user
        # the pages this user manages — page tokens here are long-lived
        r = await c.get(f"{base}/me/accounts",
                        params={"access_token": long_user, "fields": "id,name,access_token"})
        pd = r.json()
        if r.status_code >= 300:
            raise RuntimeError(f"ดึงเพจไม่สำเร็จ: {str(pd)[:200]}")
    return [{"id": p.get("id"), "name": p.get("name"), "access_token": p.get("access_token")}
            for p in (pd.get("data") or []) if p.get("id") and p.get("access_token")]


async def save_page(page_id: str, page_token: str) -> None:
    """Persist the chosen Page's id + (long-lived) token so post_video() uses it."""
    from app.core.database import set_config
    await set_config("FB_PAGE_ID", page_id)
    await set_config("FB_PAGE_TOKEN", page_token)
    os.environ["FB_PAGE_ID"] = page_id
    os.environ["FB_PAGE_TOKEN"] = page_token


async def post_video(mp4_path: str, caption: str) -> tuple[bool, str]:
    """Upload an mp4 to the page feed via the Graph video endpoint."""
    pid, tok, ver = _cfg()
    if not pid or not tok:
        return False, "ยังไม่ได้ตั้ง FB_PAGE_ID / FB_PAGE_TOKEN"
    if not os.path.exists(mp4_path):
        return False, "ไม่พบไฟล์วิดีโอ"
    try:
        async with httpx.AsyncClient(timeout=600) as c:
            with open(mp4_path, "rb") as fh:
                r = await c.post(
                    f"https://graph-video.facebook.com/{ver}/{pid}/videos",
                    data={"access_token": tok, "description": caption},
                    files={"source": (os.path.basename(mp4_path), fh, "video/mp4")},
                )
        if r.status_code >= 300:
            return False, f"FB {r.status_code}: {r.text[:300]}"
        vid = r.json().get("id", "")
        return True, f"โพสต์ขึ้นเพจแล้ว (video {vid})"
    except Exception as exc:
        return False, f"โพสต์ FB ไม่สำเร็จ: {str(exc)[:200]}"
