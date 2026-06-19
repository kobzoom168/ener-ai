"""AI animated images (SEPARATE add-on — does NOT touch the locked still pipeline).

Turns each ACCURATE Flux-dev still into a short moving clip via fal's Kling image-to-video, so
the motion stays on-content (we animate the picture we already trust, not a fresh text-to-video
that drifts). Gated by the `vdo_animate` config flag — when off, nothing here runs and clips
render exactly as the locked still pipeline does. Fail-open everywhere: any failure falls back
to the original still, so a clip always ships.
"""
from __future__ import annotations

import asyncio
import base64
import os
import time

import httpx

# Kling 2.5 turbo pro image-to-video — the "สวยสุด" tier the creator chose. Override via env.
_DEFAULT_MODEL = "fal-ai/kling-video/v2.5-turbo/pro/image-to-video"
_ANIM_PROMPT = ("subtle natural cinematic motion, gentle parallax and slow camera move, "
                "living scene, keep the subject and composition unchanged")


def _key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def current_model() -> str:
    return (os.environ.get("FAL_ANIMATE_MODEL", "") or _DEFAULT_MODEL).strip()


async def is_on() -> bool:
    """True only when the user turned the animate toggle on AND a fal key exists."""
    if not _key():
        return False
    try:
        from app.core.database import get_config
        return (await get_config("vdo_animate", "")).strip().lower() in ("1", "true", "on", "yes")
    except Exception:
        return False


async def animate_image(image_path: str, out_path: str, model: str = "") -> str | None:
    """Animate ONE still → mp4 via fal Kling i2v (queue API). Fail-open → None."""
    key = _key()
    if not key or not image_path or not os.path.exists(image_path):
        return None
    mdl = (model or current_model()).strip()
    try:
        with open(image_path, "rb") as f:
            data_uri = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
        body = {"image_url": data_uri, "prompt": _ANIM_PROMPT, "duration": "5"}
        async with httpx.AsyncClient(timeout=240) as c:
            sub = await c.post(f"https://queue.fal.run/{mdl}", headers=headers, json=body)
            if sub.status_code >= 300:
                return None
            sj = sub.json()
            # IMPORTANT: use the URLs fal returns — sub-pathed models (kling .../pro/image-to-video)
            # expose their queue under the APP id only, so building the URL by hand 404s.
            status_url = sj.get("status_url")
            response_url = sj.get("response_url")
            if not status_url or not response_url:
                return None
            result = None
            for _ in range(120):  # ~360s max (i2v can be slow)
                await asyncio.sleep(3)
                st = await c.get(status_url, headers=headers)
                if st.status_code >= 300:
                    continue
                try:
                    status = st.json().get("status")
                except Exception:
                    continue
                if status == "COMPLETED":
                    rr = await c.get(response_url, headers=headers)
                    if rr.status_code < 300:
                        result = rr.json()
                    break
                if status in ("FAILED", "ERROR"):
                    return None
            if not result:
                return None
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


async def animate_items(items: list[tuple[str, str]], vdo_dir: str) -> list[tuple[str, str]]:
    """Animate every still bg_item in parallel → (video, 'video'); keep the still on any failure."""
    stamp = int(time.time())

    async def _one(i: int, path: str, kind: str) -> tuple[str, str]:
        if kind != "image" or not path:
            return (path, kind)
        out = os.path.join(vdo_dir, f"anim_{stamp}_{i}.mp4")
        vid = await animate_image(path, out)
        return (vid, "video") if vid else (path, kind)

    return list(await asyncio.gather(*[_one(i, p, k) for i, (p, k) in enumerate(items)]))
