"""Minimal Postiz public API client: upload media + create/publish a post.

Used by the ener-vdo pipeline to push a rendered short to a Postiz-connected
channel (e.g. the Ener Scan Facebook page). Auth is the Postiz API key, read from
POSTIZ_API_KEY env (fallback to the DB config 'postiz_api_key').
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

POSTIZ_URL = (os.environ.get("POSTIZ_URL") or "https://postiz.my-ener.uk").rstrip("/")
# Ener Scan FB page integration (connected in Postiz). Override via env if needed.
FB_INTEGRATION_ID = os.environ.get("POSTIZ_FB_INTEGRATION_ID") or "cmqaobcuh0001ms7h18uvalgu"


async def get_api_key() -> str:
    key = (os.environ.get("POSTIZ_API_KEY") or "").strip()
    if not key:
        try:
            from app.core.database import get_config
            key = str(await get_config("postiz_api_key", "") or "").strip()
        except Exception:
            pass
    return key


async def list_integrations() -> tuple[list | None, str]:
    key = await get_api_key()
    if not key:
        return None, "ยังไม่ได้ตั้ง POSTIZ_API_KEY"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{POSTIZ_URL}/api/public/v1/integrations",
                            headers={"Authorization": key})
        if r.status_code >= 300:
            return None, f"{r.status_code}: {r.text[:200]}"
        return r.json(), ""
    except Exception as exc:
        return None, str(exc)[:200]


async def upload_media(file_path: str) -> tuple[dict | None, str]:
    key = await get_api_key()
    if not key:
        return None, "ยังไม่ได้ตั้ง POSTIZ_API_KEY"
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            with open(file_path, "rb") as fh:
                r = await c.post(
                    f"{POSTIZ_URL}/api/public/v1/upload",
                    headers={"Authorization": key},
                    files={"file": (os.path.basename(file_path), fh, "video/mp4")},
                )
        if r.status_code >= 300:
            return None, f"upload {r.status_code}: {r.text[:200]}"
        return r.json(), ""
    except Exception as exc:
        return None, str(exc)[:200]


async def create_post(integration_id: str, content: str, media: dict | None,
                      when: str = "now") -> tuple[object, str]:
    """when: 'now' = publish immediately, 'draft' = save as a Postiz draft."""
    key = await get_api_key()
    if not key:
        return None, "ยังไม่ได้ตั้ง POSTIZ_API_KEY"
    value_item: dict = {"content": content}
    if media:
        value_item["image"] = [{"id": media.get("id"), "path": media.get("path")}]
    payload = {
        "type": when,
        "shortLink": False,
        "date": datetime.now(timezone.utc).isoformat(),
        "tags": [],
        "posts": [{"integration": {"id": integration_id}, "value": [value_item]}],
    }
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"{POSTIZ_URL}/api/public/v1/posts",
                headers={"Authorization": key, "Content-Type": "application/json"},
                json=payload,
            )
        if r.status_code >= 300:
            return None, f"post {r.status_code}: {r.text[:300]}"
        return r.json(), ""
    except Exception as exc:
        return None, str(exc)[:200]


async def post_video(mp4_path: str, caption: str, integration_id: str = "",
                     when: str = "now") -> tuple[bool, str]:
    """Upload the MP4 then create/publish a post on the given channel. Returns (ok, msg)."""
    media, err = await upload_media(mp4_path)
    if err:
        return False, f"อัปวิดีโอเข้า Postiz ไม่สำเร็จ: {err}"
    _res, err = await create_post(integration_id or FB_INTEGRATION_ID, caption, media, when)
    if err:
        return False, f"สร้างโพสต์ไม่สำเร็จ: {err}"
    return True, ("โพสต์ขึ้นเพจแล้ว" if when == "now" else "บันทึกเป็น draft ใน Postiz แล้ว")
