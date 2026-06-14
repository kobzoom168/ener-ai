"""fal.ai AI video: generate ONE 'hero' background clip per short (e.g. a naga, a glowing
amulet, swirling smoke) for scenes that don't exist in stock footage. Everything else stays
free Thai stock video. Fail-open: returns None when the key is missing or the API errors.

Model is env-configurable (FAL_VIDEO_MODEL); default is a cheap text-to-video.
"""
from __future__ import annotations

import asyncio
import os

import httpx

FAL_MODEL = os.environ.get("FAL_VIDEO_MODEL", "fal-ai/ltx-video")


def _key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def enabled() -> bool:
    if os.environ.get("VDO_AI_VIDEO", "1") == "0":
        return False
    return bool(_key())


async def generate_ai_video(prompt: str, out_path: str) -> str | None:
    """Generate a short hero clip from `prompt` via fal.ai's queue API. Fail-open."""
    key = _key()
    prompt = (prompt or "").strip()
    if not key or not prompt:
        return None
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = {"prompt": (f"{prompt}. Vertical 9:16, cinematic, dark and mysterious mood, "
                       "atmospheric, high quality. No text, no words, no captions.")}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            sub = await c.post(f"https://queue.fal.run/{FAL_MODEL}", headers=headers, json=body)
            if sub.status_code >= 300:
                return None
            req = sub.json().get("request_id")
            if not req:
                return None
            base = f"https://queue.fal.run/{FAL_MODEL}/requests/{req}"
            result = None
            for _ in range(100):  # ~300s max (AI video can be slow)
                await asyncio.sleep(3)
                st = await c.get(base + "/status", headers=headers)
                if st.status_code >= 300:
                    continue
                status = st.json().get("status")
                if status == "COMPLETED":
                    rr = await c.get(base, headers=headers)
                    if rr.status_code < 300:
                        result = rr.json()
                    break
                if status in ("FAILED", "ERROR"):
                    return None
            if not result:
                return None
            # most fal video models return {"video": {"url": ...}}; some {"videos":[{...}]}
            vid = ((result.get("video") or {}).get("url")
                   or (((result.get("videos") or [{}])[0]) or {}).get("url") or "")
            if not vid:
                return None
            dr = await c.get(vid)
            if dr.status_code >= 300 or not dr.content:
                return None
            with open(out_path, "wb") as fh:
                fh.write(dr.content)
        return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 10000 else None
    except Exception:
        return None
