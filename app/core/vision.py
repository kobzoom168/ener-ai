"""Vision helpers for multimodal workspace chat."""
from __future__ import annotations

import base64
import mimetypes
from typing import Any


def guess_media_type(filename: str = "", content_type: str = "") -> str:
    ct = str(content_type or "").strip().lower()
    if ct.startswith("image/"):
        return ct.split(";")[0]
    guessed, _ = mimetypes.guess_type(str(filename or ""))
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/jpeg"


def build_user_content(
    text: str,
    *,
    image_base64: str | None = None,
    image_media_type: str = "image/jpeg",
) -> str | list[dict[str, Any]]:
    if not image_base64:
        return str(text or "")
    parts: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_media_type or "image/jpeg",
                "data": image_base64,
            },
        },
        {"type": "text", "text": str(text or "").strip() or "วิเคราะห์รูป screenshot นี้"},
    ]
    return parts


def vision_route() -> dict:
    return {
        "complexity": "complex",
        "domain": "vision",
        "model": "haiku",
        "tools": [
            "get_project_structure",
            "read_code_file",
            "run_shell_command",
            "get_server_overview",
        ],
        "needs_check": False,
        "reason": "vision screenshot / UI",
    }
