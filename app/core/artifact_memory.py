"""Store external gateway events as structured project artifacts."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core.database import get_db

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_JSON_CHARS = 8000
_MAX_SUMMARY_CHARS = 500
_MAX_TITLE_CHARS = 200

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|password|passwd|secret|token|authorization|bearer)",
    re.IGNORECASE,
)
_BASE64_IMAGE_RE = re.compile(
    r"data:image/[^;]+;base64,",
    re.IGNORECASE,
)

_EVENT_TYPE_TO_ARTIFACT = {
    "report_created": "scan_report",
    "scan_completed": "scan_activity",
    "payment_approved": "payment_event",
    "birthdate_saved": "user_profile_event",
    "user_profile_updated": "user_profile_event",
}

_TITLE_BY_ARTIFACT = {
    "scan_report": "Ener Scan report created",
    "scan_activity": "Ener Scan scan completed",
    "payment_event": "Ener Scan payment approved",
    "user_profile_event": "Ener Scan profile updated",
    "external_event": "External event",
}


def _clip(text: str, limit: int) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3].rstrip() + "..."


def map_event_type_to_artifact_type(event_type: str) -> str:
    key = str(event_type or "").strip().lower()
    return _EVENT_TYPE_TO_ARTIFACT.get(key, "external_event")


def _human_title(artifact_type: str, event_type: str, summary: str) -> str:
    base = _TITLE_BY_ARTIFACT.get(artifact_type) or f"External: {event_type or 'event'}"
    short = _clip(summary, 80)
    if short and short.lower() not in base.lower():
        return _clip(f"{base} — {short}", _MAX_TITLE_CHARS)
    return _clip(base, _MAX_TITLE_CHARS)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key or "")))


def _sanitize_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[k] = "[redacted]"
                continue
            key_lower = str(k).lower()
            if "slip" in key_lower and ("image" in key_lower or "url" in key_lower):
                out[k] = "[redacted]"
                continue
            if key_lower in {"image", "image_base64", "base64", "slip_image", "slip_url"}:
                out[k] = "[redacted]"
                continue
            out[k] = _sanitize_value(v, depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize_value(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        if _BASE64_IMAGE_RE.search(value) or (
            len(value) > 500 and value.startswith("data:")
        ):
            return "[redacted:image]"
        if len(value) > 2000:
            return _clip(value, 2000)
        return value
    return value


def _safe_payload_json(payload: Any) -> str:
    cleaned = _sanitize_value(payload if payload is not None else {})
    try:
        text = json.dumps(cleaned, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps({"raw": str(cleaned)[:500]}, ensure_ascii=False)
    return text[:_MAX_PAYLOAD_JSON_CHARS]


def _normalize_tags(
    source: str,
    project_slug: str,
    event_type: str,
    artifact_type: str,
    extra: list[str] | None = None,
) -> str:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in [source, project_slug, event_type, artifact_type, *(extra or [])]:
        clean = str(tag or "").strip().lower()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        tags.append(clean)
    return json.dumps(tags, ensure_ascii=False)


async def store_external_event_artifact(event_row_or_payload: dict) -> dict:
    """
    Persist external /ai/event data into project_artifacts.
    Never raises — caller should treat failures as non-fatal.
    """
    data = event_row_or_payload if isinstance(event_row_or_payload, dict) else {}
    try:
        event_type = str(data.get("event_type", "external_event") or "external_event").strip()
        source = str(data.get("source", "external") or "external").strip() or "external"
        project_slug = str(data.get("project_slug", "") or "").strip() or "external"
        summary = _clip(
            str(data.get("summary", "") or "").strip() or event_type,
            _MAX_SUMMARY_CHARS,
        )
        external_id = str(
            data.get("external_object_id") or data.get("external_id") or ""
        ).strip() or None
        event_id = data.get("event_id")
        if event_id is not None:
            try:
                event_id = int(event_id)
            except (TypeError, ValueError):
                event_id = None

        artifact_type = map_event_type_to_artifact_type(event_type)
        title = _human_title(artifact_type, event_type, summary)
        payload = data.get("payload")
        if payload is None and isinstance(data.get("context"), dict):
            payload = data.get("context", {}).get("payload")
        payload_json = _safe_payload_json(
            {
                "event_type": event_type,
                "external_user_id": data.get("external_user_id"),
                "payload": payload if isinstance(payload, (dict, list)) else {},
            }
        )
        tags_json = _normalize_tags(source, project_slug, event_type, artifact_type)

        project_id = data.get("project_id")
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                project_id = None

        async with get_db() as db:
            cursor = await db.execute(
                """
                INSERT INTO project_artifacts (
                    project_id, project_slug, source, external_id,
                    artifact_type, title, summary, payload_json, tags, event_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    project_slug,
                    source,
                    external_id,
                    artifact_type,
                    title,
                    summary,
                    payload_json,
                    tags_json,
                    event_id,
                ),
            )
            await db.commit()
            artifact_id = int(cursor.lastrowid or 0)

        return {"ok": True, "artifact_id": artifact_id}
    except Exception as exc:
        logger.warning(
            "store_external_event_artifact failed: %s",
            str(exc)[:240],
            exc_info=False,
        )
        return {"ok": False, "artifact_id": None, "error": str(exc)[:200]}


async def get_recent_project_artifacts(
    project_slug: str | None = None,
    limit: int = 50,
) -> list[dict]:
    safe_limit = max(1, min(int(limit), 200))
    slug = str(project_slug or "").strip().lower()

    async with get_db() as db:
        if slug:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    project_id,
                    project_slug,
                    source,
                    external_id,
                    artifact_type,
                    title,
                    summary,
                    payload_json,
                    tags,
                    event_id,
                    datetime(created_at, '+7 hours') AS created_at
                FROM project_artifacts
                WHERE LOWER(project_slug) = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (slug, safe_limit),
            )
        else:
            cursor = await db.execute(
                """
                SELECT
                    id,
                    project_id,
                    project_slug,
                    source,
                    external_id,
                    artifact_type,
                    title,
                    summary,
                    payload_json,
                    tags,
                    event_id,
                    datetime(created_at, '+7 hours') AS created_at
                FROM project_artifacts
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            )
        rows = await cursor.fetchall()

    artifacts: list[dict] = []
    for row in rows:
        item = dict(row)
        raw_payload = str(item.pop("payload_json", "") or "")
        preview = raw_payload
        if len(preview) > 600:
            preview = preview[:597].rstrip() + "..."
        item["payload_preview"] = preview
        try:
            item["payload"] = json.loads(raw_payload) if raw_payload else {}
        except Exception:
            item["payload"] = {}
        try:
            parsed_tags = json.loads(item.get("tags") or "[]")
            item["tags"] = parsed_tags if isinstance(parsed_tags, list) else []
        except Exception:
            item["tags"] = []
        artifacts.append(item)
    return artifacts
