"""Live status of the ener-vdo pipeline so the Auto Post UI can show a progress light
(เขียนบท → สร้างภาพ → พากย์/เรนเดอร์ → โพสต์). Stored in app_config so the background
render task and the polling UI share it.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.database import get_config, set_config

_BKK = ZoneInfo("Asia/Bangkok")
_KEY = "vdo_pipeline_status"

STAGES = {
    "idle": "พร้อม",
    "script": "✍️ กำลังเขียนบท…",
    "media": "🎨 กำลังสร้างภาพ + หน้าพูด…",
    "render": "🎬 กำลังพากย์ + ตัดต่อ…",
    "posting": "📤 กำลังส่ง/โพสต์…",
    "done": "✅ เสร็จแล้ว",
    "error": "❌ ผิดพลาด",
}
STAGE_PCT = {"idle": 0, "script": 15, "media": 45, "render": 75, "posting": 92, "done": 100, "error": 100}


async def set_status(stage: str, detail: str = "", title: str = "") -> None:
    payload = {
        "stage": stage,
        "detail": detail or STAGES.get(stage, ""),
        "title": title,
        "pct": STAGE_PCT.get(stage, 0),
        "at": datetime.now(_BKK).strftime("%H:%M:%S"),
    }
    try:
        await set_config(_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def get_status() -> dict:
    raw = await get_config(_KEY, "")
    try:
        return json.loads(raw) if raw else {"stage": "idle", "detail": STAGES["idle"]}
    except Exception:
        return {"stage": "idle", "detail": STAGES["idle"]}
