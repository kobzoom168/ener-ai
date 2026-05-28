"""Ener Scan business metrics from project_artifacts."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from app.core.database import get_db

_PROJECT_SLUG = "ener-scan"
_AMOUNT_KEYS = ("amount", "price", "total", "paid_amount", "package_price")
_USER_KEYS = (
    "external_user_id",
    "lineUserId",
    "line_user_id",
    "userId",
    "user_id",
)
_BASE64_IMAGE_RE = re.compile(r"data:image/[^;]+;base64,", re.IGNORECASE)


def normalize_range(range_value: str | None) -> str:
    key = str(range_value or "7d").strip().lower()
    if key in {"today", "7d", "30d"}:
        return key
    return "7d"


def range_created_at_sql(range_key: str) -> str:
    """SQLite filter on created_at using Bangkok (+7 hours)."""
    if range_key == "today":
        return "date(datetime(created_at, '+7 hours')) = date(datetime('now', '+7 hours'))"
    if range_key == "30d":
        return "datetime(created_at, '+7 hours') >= datetime('now', '+7 hours', '-30 days')"
    return "datetime(created_at, '+7 hours') >= datetime('now', '+7 hours', '-7 days')"


def _parse_payload_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _event_type_from_row(artifact_type: str, payload_root: dict) -> str:
    et = str(payload_root.get("event_type") or "").strip().lower()
    if et:
        return et
    inner = payload_root.get("payload")
    if isinstance(inner, dict):
        et2 = str(inner.get("event_type") or "").strip().lower()
        if et2:
            return et2
    tags = payload_root.get("_tags")
    if isinstance(tags, list):
        for tag in tags:
            t = str(tag).lower()
            if t in {
                "report_created",
                "scan_completed",
                "payment_approved",
                "birthdate_saved",
                "user_profile_updated",
            }:
                return t
    return ""


def is_scan_completed(artifact_type: str, event_type: str) -> bool:
    at = str(artifact_type or "").strip().lower()
    et = str(event_type or "").strip().lower()
    return at == "scan_activity" or et == "scan_completed"


def is_report_created(artifact_type: str, event_type: str) -> bool:
    at = str(artifact_type or "").strip().lower()
    et = str(event_type or "").strip().lower()
    return at == "scan_report" or et == "report_created"


def is_payment_approved(artifact_type: str, event_type: str) -> bool:
    at = str(artifact_type or "").strip().lower()
    et = str(event_type or "").strip().lower()
    return at == "payment_event" or et == "payment_approved"


def safe_conversion_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 2)


def cap_conversion_rate(raw_rate: float) -> float:
    try:
        value = float(raw_rate)
    except (TypeError, ValueError):
        return 0.0
    return round(min(value, 100.0), 2)


def build_event_coverage(
    scan_completed: int,
    report_created: int,
    payment_approved: int,
) -> dict:
    has_scan = scan_completed > 0
    has_report = report_created > 0
    has_payment = payment_approved > 0

    if scan_completed == 0 and report_created == 0:
        scan_report_balance = "empty"
    elif scan_completed == 0 and report_created > 0:
        scan_report_balance = "no_scan_events"
    elif report_created > scan_completed:
        scan_report_balance = "report_gt_scan"
    else:
        scan_report_balance = "ok"

    if report_created == 0 and payment_approved == 0:
        payment_report_balance = "empty"
    elif report_created == 0 and payment_approved > 0:
        payment_report_balance = "no_report_events"
    elif payment_approved > report_created:
        payment_report_balance = "payment_gt_report"
    else:
        payment_report_balance = "ok"

    return {
        "has_scan_events": has_scan,
        "has_report_events": has_report,
        "has_payment_events": has_payment,
        "scan_report_balance": scan_report_balance,
        "payment_report_balance": payment_report_balance,
    }


def build_data_quality(
    scan_completed: int,
    report_created: int,
    payment_approved: int,
) -> dict:
    warnings: list[str] = []
    notes: list[str] = []

    if report_created > scan_completed and scan_completed > 0:
        warnings.append(
            "report_created exceeds scan_completed; funnel rate may be inflated "
            "because scan_completed events may be incomplete."
        )
    if payment_approved > report_created and report_created > 0:
        warnings.append(
            "payment_approved exceeds report_created; report events may be incomplete."
        )
    if scan_completed == 0 and report_created > 0:
        warnings.append(
            "No scan_completed events found but report_created exists; "
            "scan event coverage may be incomplete."
        )
    if report_created == 0 and payment_approved > 0:
        warnings.append(
            "No report_created events found but payment_approved exists; "
            "report event coverage may be incomplete."
        )

    if warnings:
        notes.append(
            "Funnel rates use raw event counts; conversion may exceed 100% when "
            "upstream events are missing or backfilled unevenly."
        )

    status = "warning" if warnings else "ok"
    return {"status": status, "warnings": warnings, "notes": notes}


def parse_amount_from_payload(payload_root: dict) -> float:
    """Extract numeric amount from payload; return 0 if missing/invalid."""
    candidates: list[Any] = []
    for key in _AMOUNT_KEYS:
        if key in payload_root:
            candidates.append(payload_root.get(key))
    inner = payload_root.get("payload")
    if isinstance(inner, dict):
        for key in _AMOUNT_KEYS:
            if key in inner:
                candidates.append(inner.get(key))
    for val in candidates:
        if val is None:
            continue
        try:
            num = float(val)
            if num >= 0:
                return round(num, 2)
        except (TypeError, ValueError):
            continue
    return 0.0


def extract_external_user_id(payload_root: dict) -> str | None:
    for key in _USER_KEYS:
        val = payload_root.get(key)
        if val:
            return str(val).strip() or None
    inner = payload_root.get("payload")
    if isinstance(inner, dict):
        for key in _USER_KEYS:
            val = inner.get(key)
            if val:
                return str(val).strip() or None
    return None


def _sanitize_recent_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").strip()
    if _BASE64_IMAGE_RE.search(text):
        return "[redacted]"
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def build_recent_item(row: dict) -> dict:
    payload_root = _parse_payload_json(row.get("payload_json"))
    event_type = _event_type_from_row(str(row.get("artifact_type") or ""), payload_root)
    amount = parse_amount_from_payload(payload_root)
    user_id = extract_external_user_id(payload_root)
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "artifact_type": row.get("artifact_type"),
        "event_type": event_type or None,
        "title": _sanitize_recent_text(row.get("title"), 200),
        "summary": _sanitize_recent_text(row.get("summary"), 300),
        "external_id": _sanitize_recent_text(row.get("external_id"), 120) or None,
        "external_user_id": _sanitize_recent_text(user_id, 80) if user_id else None,
        "amount": amount,
    }


def aggregate_artifacts(rows: list[dict], range_key: str) -> dict:
    scan_completed = report_created = payment_approved = 0
    unique_users: set[str] = set()
    estimated_revenue = 0.0
    by_artifact_type: dict[str, int] = {}
    by_event_type: dict[str, int] = {}
    trend_buckets: dict[str, dict[str, int]] = {}

    for row in rows:
        artifact_type = str(row.get("artifact_type") or "")
        payload_root = _parse_payload_json(row.get("payload_json"))
        event_type = _event_type_from_row(artifact_type, payload_root)

        if is_scan_completed(artifact_type, event_type):
            scan_completed += 1
        if is_report_created(artifact_type, event_type):
            report_created += 1
        if is_payment_approved(artifact_type, event_type):
            payment_approved += 1
            estimated_revenue += parse_amount_from_payload(payload_root)

        user_id = extract_external_user_id(payload_root)
        if user_id:
            unique_users.add(user_id)

        at_key = artifact_type or "unknown"
        by_artifact_type[at_key] = by_artifact_type.get(at_key, 0) + 1
        et_key = event_type or at_key
        by_event_type[et_key] = by_event_type.get(et_key, 0) + 1

        day = str(row.get("created_at") or "")[:10]
        if day:
            bucket = trend_buckets.setdefault(
                day,
                {
                    "scan_completed": 0,
                    "report_created": 0,
                    "payment_approved": 0,
                    "total": 0,
                },
            )
            bucket["total"] += 1
            if is_scan_completed(artifact_type, event_type):
                bucket["scan_completed"] += 1
            if is_report_created(artifact_type, event_type):
                bucket["report_created"] += 1
            if is_payment_approved(artifact_type, event_type):
                bucket["payment_approved"] += 1

    trend = _build_trend_series(trend_buckets, range_key)
    by_artifact_list = [
        {"artifact_type": k, "count": v}
        for k, v in sorted(by_artifact_type.items(), key=lambda x: (-x[1], x[0]))
    ]
    by_event_list = [
        {"event_type": k, "count": v}
        for k, v in sorted(by_event_type.items(), key=lambda x: (-x[1], x[0]))
    ]

    total_artifacts = len(rows)

    scan_to_report_raw = safe_conversion_rate(report_created, scan_completed)
    report_to_payment_raw = safe_conversion_rate(payment_approved, report_created)
    data_quality = build_data_quality(scan_completed, report_created, payment_approved)
    coverage = build_event_coverage(scan_completed, report_created, payment_approved)

    return {
        "summary": {
            "total_artifacts": total_artifacts,
            "scan_completed": scan_completed,
            "report_created": report_created,
            "payment_approved": payment_approved,
            "unique_users": len(unique_users),
            "estimated_revenue": round(estimated_revenue, 2),
            "scan_to_report_rate": scan_to_report_raw,
            "report_to_payment_rate": report_to_payment_raw,
            "scan_to_report_rate_raw": scan_to_report_raw,
            "report_to_payment_rate_raw": report_to_payment_raw,
            "scan_to_report_rate_capped": cap_conversion_rate(scan_to_report_raw),
            "report_to_payment_rate_capped": cap_conversion_rate(report_to_payment_raw),
            "data_quality": data_quality,
        },
        "coverage": coverage,
        "trend": trend,
        "by_artifact_type": by_artifact_list,
        "by_event_type": by_event_list,
    }


def _build_trend_series(trend_buckets: dict[str, dict[str, int]], range_key: str) -> list[dict]:
    days = _trend_day_count(range_key)
    end = datetime.utcnow() + timedelta(hours=7)
    start = end - timedelta(days=days - 1)
    out: list[dict] = []
    cur = start.date()
    end_date = end.date()
    while cur <= end_date:
        key = cur.isoformat()
        bucket = trend_buckets.get(
            key,
            {
                "scan_completed": 0,
                "report_created": 0,
                "payment_approved": 0,
                "total": 0,
            },
        )
        out.append({"date": key, **bucket})
        cur += timedelta(days=1)
    return out


def _trend_day_count(range_key: str) -> int:
    if range_key == "today":
        return 1
    if range_key == "30d":
        return 30
    return 7


async def get_ener_scan_business_summary(range_value: str | None = "7d") -> dict:
    range_key = normalize_range(range_value)
    range_sql = range_created_at_sql(range_key)

    async with get_db() as db:
        cursor = await db.execute(
            f"""
            SELECT
                id,
                project_slug,
                source,
                external_id,
                artifact_type,
                title,
                summary,
                payload_json,
                tags,
                datetime(created_at, '+7 hours') AS created_at
            FROM project_artifacts
            WHERE LOWER(project_slug) = ?
              AND {range_sql}
            ORDER BY id DESC
            """,
            (_PROJECT_SLUG,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    agg = aggregate_artifacts(rows, range_key)
    recent = [build_recent_item(r) for r in rows[:20]]

    return {
        "ok": True,
        "project_slug": _PROJECT_SLUG,
        "range": range_key,
        **agg,
        "recent": recent,
    }
