"""D-ID talking head: user's face photo + narration audio -> lip-synced video, used as a
corner PIP over the clip background.

Fail-open: every entry point returns None when the key/face is missing or the API errors,
so the video pipeline still ships a normal clip. The face photo lives at FACE_PATH and is
served publicly (D-ID fetches it by URL); the narration mp3 is served from /vdo/audio.
"""
from __future__ import annotations

import asyncio
import os

import httpx

FACE_DIR = "/app/data/avatar"
FACE_PATH = os.path.join(FACE_DIR, "face.jpg")
PUBLIC_BASE = (os.environ.get("PUBLIC_BASE_URL") or "https://my-ener.uk").rstrip("/")


def _auth() -> str | None:
    key = os.environ.get("DID_API_KEY", "").strip()
    if not key:
        return None
    return key if key.lower().startswith("basic ") else f"Basic {key}"


def face_exists() -> bool:
    return os.path.exists(FACE_PATH) and os.path.getsize(FACE_PATH) > 1000


def enabled() -> bool:
    """Talking head is usable only when both the D-ID key and a face photo are present."""
    if os.environ.get("VDO_FACE_PIP", "1") == "0":
        return False
    return bool(_auth()) and face_exists()


async def generate_talking_head(audio_path: str, out_path: str) -> str | None:
    """Create a D-ID talk (face + narration), poll until done, download the mp4. Fail-open.

    The narration mp3 is uploaded to D-ID's /audios first (D-ID won't validate an external
    audio URL); the face is referenced by its public /avatar/face.jpg URL.
    """
    auth = _auth()
    if not auth or not face_exists() or not audio_path or not os.path.exists(audio_path):
        return None
    json_headers = {"Authorization": auth, "Content-Type": "application/json",
                    "accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            with open(audio_path, "rb") as fh:
                ur = await c.post(
                    "https://api.d-id.com/audios",
                    headers={"Authorization": auth, "accept": "application/json"},
                    files={"audio": (os.path.basename(audio_path), fh, "audio/mpeg")},
                )
            if ur.status_code >= 300:
                return None
            audio_url = ur.json().get("url")
            if not audio_url:
                return None
            body = {
                "source_url": f"{PUBLIC_BASE}/avatar/face.jpg",
                "script": {"type": "audio", "audio_url": audio_url},
                "config": {"stitch": True},
            }
            r = await c.post("https://api.d-id.com/talks", headers=json_headers, json=body)
            if r.status_code >= 300:
                return None
            tid = r.json().get("id")
            if not tid:
                return None
            headers = json_headers
            result_url = None
            for _ in range(75):  # ~150s max
                await asyncio.sleep(2)
                pr = await c.get(f"https://api.d-id.com/talks/{tid}", headers=headers)
                if pr.status_code >= 300:
                    continue
                pj = pr.json()
                st = pj.get("status")
                if st == "done":
                    result_url = pj.get("result_url")
                    break
                if st in ("error", "rejected"):
                    return None
            if not result_url:
                return None
            dr = await c.get(result_url)
            if dr.status_code >= 300 or not dr.content:
                return None
            with open(out_path, "wb") as fh:
                fh.write(dr.content)
        return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 10000 else None
    except Exception:
        return None
