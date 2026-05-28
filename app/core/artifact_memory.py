"""Store external gateway events as structured project artifacts."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiosqlite

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


def _parse_tags_json(tags_raw: Any) -> list[str]:
    try:
        parsed = json.loads(tags_raw or "[]")
        return [str(t) for t in parsed] if isinstance(parsed, list) else []
    except Exception:
        return []


def _parse_context_json(context_raw: Any) -> dict:
    if isinstance(context_raw, dict):
        return context_raw
    try:
        parsed = json.loads(context_raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def _find_artifact_id_by_event_id(event_id: int) -> int | None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM project_artifacts WHERE event_id = ? LIMIT 1",
            (event_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    try:
        return int(row["id"])
    except (TypeError, ValueError):
        return None


def reconstruct_agent_event_payload(row: dict) -> dict:
    """Build store_external_event_artifact input from an agent_events row."""
    ctx = _parse_context_json(row.get("context"))
    tags = _parse_tags_json(row.get("tags"))
    source = str(ctx.get("source") or row.get("triggered_by") or "external").strip() or "external"
    project_slug = str(ctx.get("project_slug") or "").strip()
    if not project_slug:
        for tag in tags:
            low = str(tag).lower()
            if low in {"ener-scan", "ener_scan"}:
                project_slug = "ener-scan"
                break
    if not project_slug:
        project_slug = "external"
    payload = ctx.get("payload")
    if not isinstance(payload, (dict, list)):
        payload = {}
    return {
        "event_id": row.get("id"),
        "event_type": row.get("event_type"),
        "source": source,
        "project_slug": project_slug,
        "summary": row.get("summary"),
        "external_user_id": ctx.get("external_user_id"),
        "external_object_id": ctx.get("external_object_id"),
        "payload": payload,
        "context": ctx,
    }


def _event_matches_filters(
    row: dict,
    source: str | None,
    project_slug: str | None,
) -> bool:
    if not source and not project_slug:
        return True
    tags = [str(t).lower() for t in _parse_tags_json(row.get("tags"))]
    ctx = _parse_context_json(row.get("context"))
    triggered = str(row.get("triggered_by") or "").lower()
    ctx_source = str(ctx.get("source") or "").lower()
    ctx_slug = str(ctx.get("project_slug") or "").lower()
    context_text = str(row.get("context") or "").lower()

    if source:
        src = str(source).strip().lower()
        if triggered != src and ctx_source != src and src not in tags:
            return False
    if project_slug:
        slug = str(project_slug).strip().lower()
        if ctx_slug != slug and slug not in tags and slug not in context_text:
            return False
    return True


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

        if event_id is not None:
            existing_id = await _find_artifact_id_by_event_id(event_id)
            if existing_id:
                return {"ok": True, "artifact_id": existing_id, "existing": True}

        async with get_db() as db:
            try:
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
            except aiosqlite.IntegrityError:
                if event_id is not None:
                    existing_id = await _find_artifact_id_by_event_id(event_id)
                    if existing_id:
                        return {
                            "ok": True,
                            "artifact_id": existing_id,
                            "existing": True,
                        }
                raise

        return {"ok": True, "artifact_id": artifact_id, "existing": False}
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


async def backfill_external_event_artifacts(
    source: str | None = None,
    project_slug: str | None = None,
    limit: int = 500,
) -> dict:
    """Backfill project_artifacts from AIGatewayEvent agent_events rows."""
    safe_limit = max(1, min(int(limit), 2000))
    scanned = created = skipped = failed = 0
    errors: list[str] = []

    conditions = ["agent_name = 'AIGatewayEvent'"]
    params: list[Any] = []
    if source:
        src = str(source).strip().lower()
        conditions.append("(LOWER(triggered_by) = ? OR tags LIKE ?)")
        params.extend([src, f'%"{src}"%'])
    if project_slug:
        slug = str(project_slug).strip().lower()
        conditions.append("(tags LIKE ? OR LOWER(context) LIKE ?)")
        params.extend([f'%"{slug}"%', f"%{slug}%"])
    where = " AND ".join(conditions)
    params.append(safe_limit)

    async with get_db() as db:
        cursor = await db.execute(
            f"""
            SELECT id, event_type, triggered_by, summary, tags, context
            FROM agent_events
            WHERE {where}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        cur_existing = await db.execute(
            "SELECT event_id FROM project_artifacts WHERE event_id IS NOT NULL"
        )
        existing_event_ids = {
            int(r["event_id"])
            for r in await cur_existing.fetchall()
            if r["event_id"] is not None
        }

    for row in rows:
        if not _event_matches_filters(row, source, project_slug):
            continue
        scanned += 1
        event_id = int(row["id"])
        if event_id in existing_event_ids:
            skipped += 1
            continue
        try:
            payload = reconstruct_agent_event_payload(row)
            result = await store_external_event_artifact(payload)
            if result.get("ok") and result.get("artifact_id"):
                if result.get("existing"):
                    skipped += 1
                else:
                    created += 1
                existing_event_ids.add(event_id)
            else:
                failed += 1
                errors.append(
                    f"event {event_id}: {str(result.get('error', 'unknown'))[:120]}"
                )
        except Exception as exc:
            failed += 1
            errors.append(f"event {event_id}: {str(exc)[:120]}")

    return {
        "ok": True,
        "scanned": scanned,
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "errors": errors[:20],
    }


async def get_artifact_coverage(project_slug: str | None = None) -> dict:
    slug = str(project_slug or "").strip().lower() or "ener-scan"
    slug_pattern = f'%"{slug}"%'
    context_pattern = f"%{slug}%"

    async with get_db() as db:
        cur_events_total = await db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM agent_events
            WHERE agent_name = 'AIGatewayEvent'
              AND (tags LIKE ? OR LOWER(context) LIKE ?)
            """,
            (slug_pattern, context_pattern),
        )
        events_total = int((await cur_events_total.fetchone())["cnt"])

        cur_events_by_type = await db.execute(
            """
            SELECT event_type, COUNT(*) AS cnt
            FROM agent_events
            WHERE agent_name = 'AIGatewayEvent'
              AND (tags LIKE ? OR LOWER(context) LIKE ?)
            GROUP BY event_type
            ORDER BY cnt DESC, event_type ASC
            """,
            (slug_pattern, context_pattern),
        )
        events_by_type = [
            {"event_type": r["event_type"], "count": int(r["cnt"])}
            for r in await cur_events_by_type.fetchall()
        ]

        cur_last_event = await db.execute(
            """
            SELECT datetime(created_at, '+7 hours') AS created_at
            FROM agent_events
            WHERE agent_name = 'AIGatewayEvent'
              AND (tags LIKE ? OR LOWER(context) LIKE ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (slug_pattern, context_pattern),
        )
        last_event_row = await cur_last_event.fetchone()
        last_event_at = last_event_row["created_at"] if last_event_row else None

        cur_artifacts_total = await db.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM project_artifacts
            WHERE LOWER(project_slug) = ?
            """,
            (slug,),
        )
        artifacts_total = int((await cur_artifacts_total.fetchone())["cnt"])

        cur_artifacts_by_type = await db.execute(
            """
            SELECT artifact_type, COUNT(*) AS cnt
            FROM project_artifacts
            WHERE LOWER(project_slug) = ?
            GROUP BY artifact_type
            ORDER BY cnt DESC, artifact_type ASC
            """,
            (slug,),
        )
        artifacts_by_type = [
            {"artifact_type": r["artifact_type"], "count": int(r["cnt"])}
            for r in await cur_artifacts_by_type.fetchall()
        ]

        cur_last_artifact = await db.execute(
            """
            SELECT datetime(created_at, '+7 hours') AS created_at
            FROM project_artifacts
            WHERE LOWER(project_slug) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (slug,),
        )
        last_artifact_row = await cur_last_artifact.fetchone()
        last_artifact_at = last_artifact_row["created_at"] if last_artifact_row else None

    return {
        "ok": True,
        "project_slug": slug,
        "events": {"total": events_total, "by_type": events_by_type},
        "artifacts": {"total": artifacts_total, "by_type": artifacts_by_type},
        "last_event_at": last_event_at,
        "last_artifact_at": last_artifact_at,
    }
