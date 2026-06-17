"""YouTube channel connection + Shorts upload via the YouTube Data API v3.

Uploading to YouTube needs OAuth2 — an API key alone cannot upload on behalf of a
channel. We store the channel owner's OAuth token (which carries a refresh_token) at
/app/data/youtube_token.json. Connect ONCE from /admin/youtube (one click → Google
consent screen); after that every upload mints a fresh access token from the refresh
token automatically, like gmail_agent does.

Config (set on /admin/youtube, stored in DB via set_config — env wins if present):
  youtube_client_id      - OAuth client id      (Google Cloud Console → Credentials)
  youtube_client_secret  - OAuth client secret
  youtube_redirect_uri   - the EXACT redirect URI registered in Google Cloud, e.g.
                           https://my-ener.uk/admin/youtube/callback
  youtube_privacy        - default privacyStatus for uploads: public | unlisted | private
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

# Google may hand back a superset of the scopes we asked for (include_granted_scopes);
# without this oauthlib raises a "Scope has changed" error on token exchange.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
_TOKEN_PATH = Path("/app/data/youtube_token.json")
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


async def _cfg() -> tuple[str, str, str, str]:
    """(client_id, client_secret, redirect_uri, privacy). Env overrides DB config."""
    from app.core.database import get_config
    cid = os.environ.get("YOUTUBE_CLIENT_ID", "").strip() or (await get_config("youtube_client_id", "")).strip()
    csec = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip() or (await get_config("youtube_client_secret", "")).strip()
    redir = os.environ.get("YOUTUBE_REDIRECT_URI", "").strip() or (await get_config("youtube_redirect_uri", "")).strip()
    priv = (os.environ.get("YOUTUBE_PRIVACY", "").strip()
            or (await get_config("youtube_privacy", "")).strip() or "public")
    return cid, csec, redir, priv


def _client_config(cid: str, csec: str, redirect_uri: str) -> dict:
    return {"web": {
        "client_id": cid,
        "client_secret": csec,
        "auth_uri": _AUTH_URI,
        "token_uri": _TOKEN_URI,
        "redirect_uris": [redirect_uri],
    }}


def enabled() -> bool:
    """Connected = a saved OAuth token with a refresh_token exists."""
    if not _TOKEN_PATH.exists():
        return False
    try:
        data = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        return bool(data.get("refresh_token"))
    except Exception:
        return False


async def configured() -> bool:
    """True once client id+secret are set (so the Connect flow can run)."""
    cid, csec, _, _ = await _cfg()
    return bool(cid and csec)


async def auth_url(redirect_uri: str, state: str = "") -> tuple[str, str]:
    """Build the Google consent URL. prompt=consent forces a refresh_token every time.

    Returns (url, code_verifier). The library uses PKCE: a code_verifier is generated here
    and MUST be supplied at token exchange (in a different request), so the caller persists
    it alongside the state. Returns "" for the verifier if PKCE is off.
    """
    cid, csec, _, _ = await _cfg()
    if not cid or not csec:
        raise RuntimeError("ยังไม่ได้ตั้ง client id/secret")

    def _do() -> tuple[str, str]:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            _client_config(cid, csec, redirect_uri), scopes=SCOPES, redirect_uri=redirect_uri)
        url, _ = flow.authorization_url(
            access_type="offline", include_granted_scopes="true",
            prompt="consent", state=state or "ener")
        return url, (flow.code_verifier or "")

    return await asyncio.to_thread(_do)


async def exchange_code(code: str, redirect_uri: str, code_verifier: str = "") -> None:
    """Exchange the consent code for tokens and persist them (with the refresh_token).

    `code_verifier` is the PKCE verifier captured in auth_url(); required by Google when a
    code_challenge was sent at the authorize step.
    """
    cid, csec, _, _ = await _cfg()

    def _do() -> None:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_config(
            _client_config(cid, csec, redirect_uri), scopes=SCOPES, redirect_uri=redirect_uri,
            code_verifier=code_verifier or None, autogenerate_code_verifier=False)
        flow.fetch_token(code=code)
        _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_PATH.write_text(flow.credentials.to_json(), encoding="utf-8")

    await asyncio.to_thread(_do)


def _creds_sync():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


async def check() -> tuple[bool, str]:
    """Verify the connection by fetching the linked channel (used by the Test button)."""
    if not _TOKEN_PATH.exists():
        return False, "ยังไม่ได้เชื่อม YouTube — กดปุ่ม Connect ก่อน"

    def _do() -> tuple[bool, str]:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=_creds_sync(), cache_discovery=False)
        resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
        items = resp.get("items") or []
        if not items:
            return False, "เชื่อมแล้วแต่หาช่องไม่เจอ (บัญชีนี้อาจยังไม่มีช่อง YouTube)"
        sn = items[0].get("snippet", {})
        st = items[0].get("statistics", {})
        return True, f"{sn.get('title', '?')} ({st.get('subscriberCount', '?')} subs)"

    try:
        return await asyncio.to_thread(_do)
    except Exception as exc:
        return False, str(exc)[:240]


async def video_stats(video_ids: list[str]) -> dict:
    """Fetch public stats (views/likes/comments) for uploaded videos — works with the
    youtube.readonly scope we already have. Returns {video_id: {views,likes,comments}}."""
    ids = [v for v in (video_ids or []) if v][:50]
    if not enabled() or not ids:
        return {}

    def _do() -> dict:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", credentials=_creds_sync(), cache_discovery=False)
        resp = yt.videos().list(part="statistics", id=",".join(ids)).execute()
        out = {}
        for it in (resp.get("items") or []):
            st = it.get("statistics", {})
            out[it.get("id")] = {
                "views": int(st.get("viewCount") or 0),
                "likes": int(st.get("likeCount") or 0),
                "comments": int(st.get("commentCount") or 0),
            }
        return out

    try:
        return await asyncio.to_thread(_do)
    except Exception:
        return {}


async def upload_video(mp4_path: str, title: str, description: str = "",
                       tags: list[str] | None = None, privacy: str | None = None) -> tuple[bool, str]:
    """Resumable-upload an mp4 as a YouTube video (vertical + #Shorts → shows as a Short)."""
    if not enabled():
        return False, "YouTube ยังไม่เชื่อม"
    if not os.path.exists(mp4_path):
        return False, "ไม่พบไฟล์วิดีโอ"
    _, _, _, default_priv = await _cfg()
    priv = (privacy or default_priv or "public").strip().lower()
    if priv not in {"public", "unlisted", "private"}:
        priv = "public"
    safe_title = (title or "Short").replace("<", " ").replace(">", " ").strip()[:100] or "Short"
    desc = (description or "").strip()
    if "#shorts" not in desc.lower():
        desc = (desc + "\n\n#Shorts").strip()

    def _do() -> str:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        yt = build("youtube", "v3", credentials=_creds_sync(), cache_discovery=False)
        body = {
            "snippet": {
                "title": safe_title,
                "description": desc[:4900],
                "tags": [str(t)[:60] for t in (tags or [])][:15],
                "categoryId": "22",  # People & Blogs (valid in all regions)
            },
            "status": {"privacyStatus": priv, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(mp4_path, mimetype="video/mp4", resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            _status, resp = req.next_chunk()
        return str(resp.get("id", ""))

    try:
        vid = await asyncio.to_thread(_do)
        return True, f"อัปขึ้น YouTube แล้ว (https://youtu.be/{vid})"
    except Exception as exc:
        return False, f"อัป YouTube ไม่สำเร็จ: {str(exc)[:220]}"
