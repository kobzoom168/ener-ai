"""TikTok connection + Shorts upload via the TikTok Content Posting API v2.

Posting to TikTok needs OAuth2 (client_key/secret + a user-granted token that carries a
refresh_token). Connect ONCE from /admin/tiktok (one click → TikTok consent screen); after
that every upload mints a fresh access token from the refresh token automatically, like
youtube_client does. Token is stored at /app/data/tiktok_token.json.

Flow for uploading (FILE_UPLOAD, "Direct Post"):
  1. POST /v2/post/publish/video/init/   → {publish_id, upload_url}
  2. PUT the mp4 bytes to upload_url      (single chunk)
  3. POST /v2/post/publish/status/fetch/  → poll until PUBLISH_COMPLETE

⚠️ IMPORTANT (pre-audit): until the TikTok app passes audit for the `video.publish` scope,
direct posts must be PRIVATE (privacy_level = SELF_ONLY) — TikTok rejects public posts from
unaudited apps. Set tiktok_privacy = PUBLIC_TO_EVERYONE only after the app is approved.

Config (set on /admin/tiktok, stored in DB via set_config — env wins if present):
  tiktok_client_key      - app Client key      (TikTok for Developers → your app)
  tiktok_client_secret   - app Client secret
  tiktok_redirect_uri    - EXACT redirect URI registered in the app, e.g.
                           https://my-ener.uk/admin/tiktok/callback
  tiktok_privacy         - SELF_ONLY | PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | FOLLOWER_OF_CREATOR
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

# Need video.publish for Direct Post; video.upload alone only drafts to the user's inbox.
SCOPES = "user.info.basic,video.publish"
_TOKEN_PATH = Path("/app/data/tiktok_token.json")
_AUTH_URI = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN_URI = "https://open.tiktokapis.com/v2/oauth/token/"
_API = "https://open.tiktokapis.com/v2"


async def _cfg() -> tuple[str, str, str, str]:
    """(client_key, client_secret, redirect_uri, privacy). Env overrides DB config."""
    from app.core.database import get_config
    ck = os.environ.get("TIKTOK_CLIENT_KEY", "").strip() or (await get_config("tiktok_client_key", "")).strip()
    cs = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip() or (await get_config("tiktok_client_secret", "")).strip()
    redir = os.environ.get("TIKTOK_REDIRECT_URI", "").strip() or (await get_config("tiktok_redirect_uri", "")).strip()
    priv = (os.environ.get("TIKTOK_PRIVACY", "").strip()
            or (await get_config("tiktok_privacy", "")).strip() or "SELF_ONLY")
    return ck, cs, redir, priv


def enabled() -> bool:
    """Connected = a saved token with a refresh_token exists."""
    if not _TOKEN_PATH.exists():
        return False
    try:
        return bool(json.loads(_TOKEN_PATH.read_text(encoding="utf-8")).get("refresh_token"))
    except Exception:
        return False


async def configured() -> bool:
    """True once client key+secret are set (so the Connect flow can run)."""
    ck, cs, _, _ = await _cfg()
    return bool(ck and cs)


async def auth_url(redirect_uri: str, state: str = "") -> str:
    """Build the TikTok consent URL the user clicks to grant posting access."""
    ck, cs, _, _ = await _cfg()
    if not ck or not cs:
        raise RuntimeError("ยังไม่ได้ตั้ง TikTok client key/secret")
    from urllib.parse import urlencode
    q = urlencode({
        "client_key": ck,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state or "ener",
    })
    return f"{_AUTH_URI}?{q}"


def _save_token(tok: dict) -> None:
    _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_PATH.write_text(json.dumps(tok), encoding="utf-8")


async def exchange_code(code: str, redirect_uri: str) -> None:
    """Exchange the consent code for tokens and persist them (with the refresh_token)."""
    ck, cs, _, _ = await _cfg()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN_URI, headers={"Content-Type": "application/x-www-form-urlencoded"},
                         data={"client_key": ck, "client_secret": cs, "code": code,
                               "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    data = r.json()
    if r.status_code >= 300 or not data.get("access_token"):
        raise RuntimeError(f"แลก token ไม่สำเร็จ: {str(data)[:200]}")
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 0) or 0) - 60
    _save_token(data)


async def _valid_token() -> str:
    """Return a fresh access token, refreshing from the refresh_token when expired."""
    if not _TOKEN_PATH.exists():
        raise RuntimeError("TikTok ยังไม่เชื่อม")
    tok = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
    if int(time.time()) < int(tok.get("expires_at", 0)) and tok.get("access_token"):
        return tok["access_token"]
    ck, cs, _, _ = await _cfg()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_TOKEN_URI, headers={"Content-Type": "application/x-www-form-urlencoded"},
                         data={"client_key": ck, "client_secret": cs,
                               "grant_type": "refresh_token", "refresh_token": tok.get("refresh_token", "")})
    data = r.json()
    if r.status_code >= 300 or not data.get("access_token"):
        raise RuntimeError(f"refresh token ไม่สำเร็จ: {str(data)[:200]}")
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 0) or 0) - 60
    if not data.get("refresh_token"):
        data["refresh_token"] = tok.get("refresh_token", "")
    _save_token(data)
    return data["access_token"]


async def check() -> tuple[bool, str]:
    """Verify the connection by querying the creator info (used by the Test button)."""
    if not enabled():
        return False, "ยังไม่ได้เชื่อม TikTok — กดปุ่ม Connect ก่อน"
    try:
        token = await _valid_token()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{_API}/post/publish/creator_info/query/",
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=UTF-8"})
        data = r.json()
        if r.status_code >= 300 or (data.get("error", {}).get("code") not in (None, "ok")):
            return False, f"เชื่อมแล้วแต่เช็คไม่ผ่าน: {str(data.get('error') or data)[:200]}"
        d = data.get("data", {})
        nick = d.get("creator_nickname") or d.get("creator_username") or "TikTok"
        return True, f"{nick} (โพสต์ได้สูงสุด {d.get('max_video_post_duration_sec', '?')} วิ)"
    except Exception as exc:
        return False, str(exc)[:240]


async def upload_video(mp4_path: str, title: str, privacy: str | None = None) -> tuple[bool, str]:
    """Direct-post an mp4 to TikTok via FILE_UPLOAD (init → PUT bytes → poll status)."""
    if not enabled():
        return False, "TikTok ยังไม่เชื่อม"
    if not os.path.exists(mp4_path):
        return False, "ไม่พบไฟล์วิดีโอ"
    _, _, _, default_priv = await _cfg()
    priv = (privacy or default_priv or "SELF_ONLY").strip().upper()
    if priv not in {"SELF_ONLY", "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"}:
        priv = "SELF_ONLY"
    size = os.path.getsize(mp4_path)
    safe_title = (title or "").strip()[:150]
    try:
        token = await _valid_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        init_body = {
            "post_info": {"title": safe_title, "privacy_level": priv,
                          "disable_comment": False, "disable_duet": False, "disable_stitch": False},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": size, "total_chunk_count": 1},
        }
        async with httpx.AsyncClient(timeout=300) as c:
            ir = await c.post(f"{_API}/post/publish/video/init/", headers=headers, json=init_body)
            idata = ir.json()
            if ir.status_code >= 300 or (idata.get("error", {}).get("code") not in (None, "ok")):
                return False, f"init ไม่สำเร็จ: {str(idata.get('error') or idata)[:200]}"
            d = idata.get("data", {})
            publish_id, upload_url = d.get("publish_id"), d.get("upload_url")
            if not publish_id or not upload_url:
                return False, f"init ไม่ได้ upload_url: {str(idata)[:160]}"
            # 2) upload the bytes (single chunk)
            with open(mp4_path, "rb") as fh:
                blob = fh.read()
            pr = await c.put(upload_url, content=blob,
                             headers={"Content-Type": "video/mp4",
                                      "Content-Range": f"bytes 0-{size - 1}/{size}"})
            if pr.status_code >= 300:
                return False, f"อัปไฟล์ไม่สำเร็จ (HTTP {pr.status_code})"
            # 3) poll publish status a few times
            for _ in range(20):
                sr = await c.post(f"{_API}/post/publish/status/fetch/", headers=headers,
                                  json={"publish_id": publish_id})
                sdata = sr.json().get("data", {})
                status = sdata.get("status", "")
                if status == "PUBLISH_COMPLETE":
                    note = " (โพสต์เป็นส่วนตัว — แอปยังไม่ผ่าน audit)" if priv == "SELF_ONLY" else ""
                    return True, f"โพสต์ขึ้น TikTok แล้ว{note}"
                if status in ("FAILED", "PUBLISH_FAILED"):
                    return False, f"TikTok ปฏิเสธ: {str(sdata.get('fail_reason') or sdata)[:180]}"
                import asyncio
                await asyncio.sleep(3)
            return True, "ส่งขึ้น TikTok แล้ว (กำลังประมวลผล — เช็คในแอป)"
    except Exception as exc:
        return False, f"โพสต์ TikTok ไม่สำเร็จ: {str(exc)[:220]}"
