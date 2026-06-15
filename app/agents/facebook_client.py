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
