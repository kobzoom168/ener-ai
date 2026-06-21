"""fal.ai AI video: generate ONE 'hero' background clip per short (e.g. a naga, a glowing
amulet, swirling smoke) for scenes that don't exist in stock footage. Everything else stays
free Thai stock video. Fail-open: returns None when the key is missing or the API errors.

Model is env-configurable (FAL_VIDEO_MODEL); default is a cheap text-to-video.
"""
from __future__ import annotations

import asyncio
import os

import httpx

_DEFAULT_MODEL = "fal-ai/ltx-video"

# Models offered in the UI dropdown (fal.ai). `ups` = rough $/sec (for the live cost estimate).
# LTX first = the default/cheap pick. Veo flagged ⚠️ because it burned ~$6 for 2 shots once.
MODELS = [
    {"id": "fal-ai/ltx-video", "label": "LTX Video — ถูกสุด เร็ว (แนะนำ)", "cost": "~$0.02/วิ", "ups": 0.02},
    {"id": "fal-ai/wan-25-preview/text-to-video", "label": "Wan 2.5 — กลางๆ", "cost": "~$0.05/วิ", "ups": 0.05},
    {"id": "fal-ai/minimax/hailuo-02/standard/text-to-video", "label": "Hailuo 02 — ลื่น", "cost": "~$0.05/วิ", "ups": 0.05},
    {"id": "fal-ai/kling-video/v2.5-turbo/pro/text-to-video", "label": "Kling 2.5 — cinematic คุ้ม", "cost": "~$0.07/วิ", "ups": 0.07},
    {"id": "fal-ai/veo3", "label": "⚠️ Veo 3 — แพงมาก! ($0.40/วิ ~$3+/ช็อต)", "cost": "~$0.40/วิ", "ups": 0.40},
]


def _key() -> str:
    return os.environ.get("FAL_KEY", "").strip()


def current_model() -> str:
    return (os.environ.get("FAL_VIDEO_MODEL", "") or _DEFAULT_MODEL).strip()


def enabled() -> bool:
    if os.environ.get("VDO_AI_VIDEO", "1") == "0":
        return False
    return bool(_key())


async def generate_image(prompt: str, out_path: str, model: str = "", seed: int | None = None,
                         size: dict | None = None) -> str | None:
    """9:16 background image via fal. Default Flux DEV — far better prompt adherence than schnell
    (so the picture matches the script) at ~$0.025/img. Override with FAL_IMAGE_MODEL. A shared
    `seed` across a clip keeps the look cohesive. `size` overrides the default 9:16 (e.g. 16:9 for
    Story Studio). Fail-open."""
    key = _key()
    prompt = (prompt or "").strip()
    if not key or not prompt:
        return None
    mdl = (model or os.environ.get("FAL_IMAGE_MODEL", "") or "fal-ai/flux-pro/v1.1").strip()
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = {"prompt": prompt, "num_images": 1,
            "image_size": size or {"width": 768, "height": 1344}}  # default ~9:16
    if seed is not None:
        body["seed"] = int(seed)
    if "/dev" in mdl:  # flux dev benefits from a few more steps; pro tunes itself
        body["num_inference_steps"] = 30
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"https://fal.run/{mdl}", headers=headers, json=body)
            if r.status_code >= 300:
                return None
            imgs = (r.json().get("images") or [])
            url = (imgs[0].get("url") if imgs else "") or ""
            if not url:
                return None
            dr = await c.get(url)
            if dr.status_code >= 300 or not dr.content:
                return None
        with open(out_path, "wb") as fh:
            fh.write(dr.content)
        return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 2000 else None
    except Exception:
        return None


async def generate_image_edit(prompt: str, ref_paths: list[str], out_path: str,
                              seed: int | None = None, aspect: str = "9:16") -> str | None:
    """Character-consistent 9:16 image via fal Nano Banana 2 edit: generate a NEW scene from
    `prompt` while keeping the SAME character/subject shown in the reference image(s). Unlike
    Redux this is a SEMANTIC edit (it re-poses the character into a new scene instead of cloning
    the whole frame). `ref_paths` = local anchor images sent as base64 data URIs. Queue API.
    Fail-open → None (caller falls back to plain text2img)."""
    import asyncio
    import base64
    key = _key()
    prompt = (prompt or "").strip()
    refs = [p for p in (ref_paths or []) if p and os.path.exists(p)]
    if not key or not prompt or not refs:
        return None
    mdl = (os.environ.get("FAL_EDIT_MODEL", "") or "fal-ai/nano-banana-2/edit").strip()
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    try:
        urls = []
        for p in refs[:6]:
            with open(p, "rb") as f:
                urls.append("data:image/jpeg;base64," + base64.b64encode(f.read()).decode())
        body = {"prompt": prompt, "image_urls": urls, "num_images": 1,
                "aspect_ratio": aspect, "resolution": "1K", "output_format": "jpeg"}
        if seed is not None:
            body["seed"] = int(seed)
        async with httpx.AsyncClient(timeout=180) as c:
            sub = await c.post(f"https://queue.fal.run/{mdl}", headers=headers, json=body)
            if sub.status_code >= 300:
                return None
            sj = sub.json()
            status_url = sj.get("status_url")
            response_url = sj.get("response_url")
            if not status_url or not response_url:
                return None
            result = None
            for _ in range(60):  # ~180s max
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
            imgs = (result.get("images") or [])
            url = (imgs[0].get("url") if imgs else "") or ""
            if not url:
                return None
            dr = await c.get(url)
            if dr.status_code >= 300 or not dr.content:
                return None
        with open(out_path, "wb") as fh:
            fh.write(dr.content)
        return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 2000 else None
    except Exception:
        return None


async def generate_ai_video(prompt: str, out_path: str, model: str = "") -> str | None:
    """Generate a short hero clip from `prompt` via fal.ai's queue API. Fail-open.
    `model` overrides the configured fal model for this call."""
    key = _key()
    prompt = (prompt or "").strip()
    if not key or not prompt:
        return None
    fal_model = (model or current_model()).strip() or _DEFAULT_MODEL
    headers = {"Authorization": f"Key {key}", "Content-Type": "application/json"}
    body = {"prompt": (f"{prompt}. Vertical 9:16, cinematic, dark and mysterious mood, "
                       "atmospheric, high quality. No text, no words, no captions.")}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            sub = await c.post(f"https://queue.fal.run/{fal_model}", headers=headers, json=body)
            if sub.status_code >= 300:
                return None
            req = sub.json().get("request_id")
            if not req:
                return None
            base = f"https://queue.fal.run/{fal_model}/requests/{req}"
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
