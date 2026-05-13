"""Hospital Work Dashboard — Phase 1: DB access, daily report preview, CRUD helpers.

Manual QA checklist (mirror of database._migrate_hospital_schema docstring):
- init_db on an old DB: additive columns + indexes; failures log at WARNING, app starts.
- Legacy-only seed codes → replaced with real projects (Cloud PBX, Backup, …,
  Migration DB to AWS).
- Soft delete: task/issue/other rows with is_active=0 disappear from lists and report;
  projects: ปิดโครงการ (is_active=0) hidden from active list & report; restore via API/UI.
- Daily report: standup_mention (e.g. @Noom); body mentions Cloud / PBX / Backup /
  Migration DB when seeded data present.
- Date fields (incl. implementation_date): UI uses input type=date; DB stores YYYY-MM-DD;
  report shows 13-May-2026; legacy 13-May-2026 loads via toDateInputValue (Thai month text
  in DB leaves picker empty until user picks a calendar day).
- Summary tab: GET /admin/api/hospital-work/dashboard — cards, projects overview,
  all tasks table with filters, issues & other tasks (active data only).
- Task order: hospital_project_tasks.sort_order (ลำดับ) — lower first; new task auto-appends.
- Project % complete: auto from tasks — done counts full, in_progress half, others zero; no tasks → 0%.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

import aiosqlite

from app.core.database import get_config, get_db

_TZ_BKK = timezone(timedelta(hours=7))

PROJECT_WRITE_KEYS = frozenset(
    {
        "name",
        "code",
        "status",
        "percent_complete",
        "current_status",
        "sort_order",
        "is_active",
        "description",
        "start_date",
        "end_date",
        "due_date",
        "implementation_date",
        "next_step",
        "notes",
        "vendor",
        "owner",
        "priority",
    }
)

TASK_WRITE_KEYS = frozenset(
    {
        "title",
        "status",
        "due_hint",
        "sort_order",
        "project_id",
        "details",
        "start_date",
        "end_date",
        "due_date",
        "notes",
        "is_active",
    }
)

ISSUE_WRITE_KEYS = frozenset(
    {
        "title",
        "project_id",
        "severity",
        "status",
        "details",
        "system_name",
        "impact",
        "priority",
        "start_date",
        "end_date",
        "due_date",
        "what_done",
        "next_step",
        "notes",
        "is_active",
    }
)

OTHER_WRITE_KEYS = frozenset(
    {
        "title",
        "status",
        "notes",
        "sort_order",
        "details",
        "priority",
        "requester",
        "start_date",
        "end_date",
        "due_date",
        "related_project_id",
        "is_active",
    }
)


def _row(d: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(d) if d else None


def _slug_code(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:64] or "project"


def _s(v: Any) -> str:
    if v is None:
        return ""
    return str(v)


def _issue_open_for_report(status: str | None) -> bool:
    return str(status or "").strip().lower() not in (
        "done",
        "closed",
        "resolved",
        "cancelled",
    )


_EN_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_report_date_bkk(now: datetime) -> str:
    """e.g. 13-May-2026 (ICT+7 caller should pass now in BKK)."""
    return f"{now.day}-{_EN_MONTH_ABBR[now.month - 1]}-{now.year}"


def format_report_date_value(value: str | None) -> str:
    """Format a stored date for the daily report text (tasks, project dates, implementation).

    - YYYY-MM-DD (canonical DB from date pickers) -> 13-May-2026
    - 13-May-2026 style -> returned unchanged
    - other non-empty strings (e.g. legacy Thai month text) -> returned unchanged
    - empty -> ""
    """
    v = (str(value) if value is not None else "").strip()
    if not v:
        return ""
    m_iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            dt = datetime(y, mo, d, tzinfo=_TZ_BKK)
            return format_report_date_bkk(dt)
        except ValueError:
            return v
    if re.fullmatch(r"\d{1,2}-[A-Za-z]{3}-\d{4}", v):
        return v
    return v


def _week_range_label_bkk(now: datetime) -> str:
    wd = now.weekday()
    mon = (now - timedelta(days=wd)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sun = mon + timedelta(days=6)
    return f"{mon.strftime('%d %b %Y')} – {sun.strftime('%d %b %Y')}"


def _is_due_today(
    due_hint: str | None, due_date: str | None, now: datetime
) -> bool:
    """Primary due_date format in DB is YYYY-MM-DD; legacy free text still supported."""
    h = (due_hint or "").strip().lower()
    if "วันนี้" in (due_hint or "") or "today" in h:
        return True
    d = (due_date or "").strip()
    if not d:
        return False
    m_iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", d)
    if m_iso:
        y, mo, day = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return (now.year, now.month, now.day) == (y, mo, day)
    today_fmt = format_report_date_bkk(now)
    if today_fmt.lower() in d.lower():
        return True
    if now.strftime("%Y-%m-%d") in d:
        return True
    if now.strftime("%d/%m/%Y") in d or now.strftime("%d-%m-%Y") in d:
        return True
    return False


def _task_not_done(status: str | None) -> bool:
    return str(status or "").strip().lower() not in ("done",)


def _task_status_progress_weight(status: str | None) -> float:
    """Contribution toward project %: done=1.0, in_progress=0.5, else 0."""
    s = str(status or "").strip().lower()
    if s == "done":
        return 1.0
    if s == "in_progress":
        return 0.5
    return 0.0


def percent_complete_from_tasks(tasks: list[dict[str, Any]]) -> int:
    """0–100 from active project tasks (empty list → 0)."""
    if not tasks:
        return 0
    n = float(len(tasks))
    score = sum(_task_status_progress_weight(t.get("status")) for t in tasks)
    return int(min(100, max(0, round(score / n * 100.0))))


async def _percent_complete_by_project_ids(
    db: aiosqlite.Connection, pids: list[int]
) -> dict[int, int]:
    """Aggregate task-based % per project_id (same formula as percent_complete_from_tasks)."""
    if not pids:
        return {}
    ph = ",".join("?" * len(pids))
    cur = await db.execute(
        f"""
        SELECT project_id,
               COUNT(*) AS n,
               SUM(CASE WHEN LOWER(TRIM(COALESCE(status, ''))) = 'done' THEN 1 ELSE 0 END) AS dn,
               SUM(CASE WHEN LOWER(TRIM(COALESCE(status, ''))) = 'in_progress' THEN 1 ELSE 0 END) AS pn
        FROM hospital_project_tasks
        WHERE is_active = 1 AND project_id IN ({ph})
        GROUP BY project_id
        """,
        pids,
    )
    out: dict[int, int] = {}
    for r in await cur.fetchall():
        d = dict(r)
        total = int(d["n"] or 0)
        if total <= 0:
            continue
        dn = int(d["dn"] or 0)
        pn = int(d["pn"] or 0)
        score = float(dn) + 0.5 * float(pn)
        out[int(d["project_id"])] = int(
            min(100, max(0, round(score / float(total) * 100.0)))
        )
    return out


async def refresh_project_percent_from_tasks(project_id: int) -> int:
    """Recompute hospital_projects.percent_complete from active tasks and persist."""
    tasks = await list_tasks(project_id)
    pct = percent_complete_from_tasks(tasks)
    await update_project(project_id, {"percent_complete": pct})
    return pct


def _parse_iso_date_bkk(value: str | None) -> datetime | None:
    v = (str(value) if value is not None else "").strip()
    if not v:
        return None
    m_iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if not m_iso:
        return None
    y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
    try:
        return datetime(y, mo, d, tzinfo=_TZ_BKK)
    except ValueError:
        return None


def _is_overdue_iso(due_date: str | None, now: datetime) -> bool:
    dt = _parse_iso_date_bkk(due_date)
    if not dt:
        return False
    return dt.date() < now.date()


def _build_list_today_report_text(
    *,
    mention: str,
    now: datetime,
    projects: list[dict[str, Any]],
    issues_open: list[dict[str, Any]],
    other_tasks: list[dict[str, Any]],
) -> str:
    date_line = format_report_date_bkk(now)
    week_rng = _week_range_label_bkk(now)
    lines: list[str] = [
        mention,
        f"List Today {week_rng}",
        "##################################",
        "",
        date_line,
        "",
        "หัวข้อที่ต้องการสื่อสารมีดังนี้",
        "",
        "1.วันนี้ คุณพบงานที่มีปัญหา หรือไม่",
        "-->[Tanarit] :",
    ]
    if not issues_open:
        lines.append("->")
    else:
        for iss in issues_open:
            extra = iss.get("next_step") or iss.get("details") or ""
            line = f"- {iss.get('title', '')}"
            bits = [extra] if extra else []
            for label, key in (
                ("start", "start_date"),
                ("end", "end_date"),
                ("due", "due_date"),
            ):
                fv = format_report_date_value(iss.get(key))
                if fv:
                    bits.append(f"{label} {fv}")
            if bits:
                line += f" ({', '.join(bits)})"
            lines.append(line)

    lines += [
        "",
        "2.วันนี้ คุณมีงานที่เป็นงานหลักที่ต้องทำคืออะไร",
        "",
    ]

    for idx, po in enumerate(projects, start=1):
        lines.append(f"Project {idx} :{po.get('name', '')}")
        lines.append(f">>สถานะปัจจุบัน: {po.get('current_status') or ''}")
        lines.append(f"Current Status: {po.get('status') or ''}")
        lines.append(f"% Complete: {po.get('percent_complete', 0)}%")
        proj_dates: list[str] = []
        psd = format_report_date_value(po.get("start_date"))
        ped = format_report_date_value(po.get("end_date"))
        pdd = format_report_date_value(po.get("due_date"))
        if psd:
            proj_dates.append(f"start {psd}")
        if ped:
            proj_dates.append(f"end {ped}")
        if pdd:
            proj_dates.append(f"due {pdd}")
        if proj_dates:
            lines.append("Schedule: " + ", ".join(proj_dates))
        lines.append("--------------")
        tasks = po.get("tasks") or []
        for t_i, tk in enumerate(tasks, start=1):
            line = f">{t_i}. {tk.get('title', '')}"
            date_bits: list[str] = []
            sd = format_report_date_value(tk.get("start_date"))
            ed = format_report_date_value(tk.get("end_date"))
            dd = format_report_date_value(tk.get("due_date"))
            if sd:
                date_bits.append(f"start {sd}")
            if ed:
                date_bits.append(f"end {ed}")
            if dd:
                date_bits.append(f"due {dd}")
            if date_bits:
                line += " [" + ", ".join(date_bits) + "]"
            lines.append(line)
        impl = format_report_date_value(po.get("implementation_date"))
        lines.append("")
        lines.append(f"กำหนดการ Implementation: {impl}")
        lines.append("---------------------------------------------------------------")
        lines.append("")

    lines += [
        "3.วันนี้ คุณมีคำแนะนำหรือต้องการความช่วยเหลือจากทีม หรือไม่",
        "-->[Tanarit] :",
        "งานอื่น ๆ (Other Tasks)",
    ]
    if not other_tasks:
        lines.append("->")
    else:
        for ot in other_tasks:
            det = ot.get("details") or ot.get("notes") or ""
            line = f"- {ot.get('title', '')}"
            extras = [det] if det else []
            for label, key in (
                ("start", "start_date"),
                ("end", "end_date"),
                ("due", "due_date"),
            ):
                fv = format_report_date_value(ot.get(key))
                if fv:
                    extras.append(f"{label} {fv}")
            if extras:
                line += " — " + " | ".join(extras)
            lines.append(line)

    lines += [
        "",
        "##########################################################################",
        f"สิ่งที่ต้องทำวันนี้({date_line})",
        "",
    ]

    n = 0
    for po in projects:
        n += 1
        lines.append(f"{n}. {po.get('name', '')}")
        ns = (po.get("next_step") or "").strip()
        if ns:
            lines.append(f"> {ns}")
        tasks = po.get("tasks") or []
        for tk in tasks:
            if _is_due_today(tk.get("due_hint"), tk.get("due_date"), now):
                tln = f"> {tk.get('title', '')}"
                dd = format_report_date_value(tk.get("due_date"))
                if dd:
                    tln += f" (due: {dd})"
                lines.append(tln)
        lines.append("")

    lines.append("Issues:")
    if not issues_open:
        lines.append(">")
    else:
        for iss in issues_open:
            ins = (iss.get("next_step") or iss.get("title") or "").strip()
            lines.append(f"> {ins}")

    lines.append("")
    lines.append("Other Tasks:")
    if not other_tasks:
        lines.append(">")
    else:
        for ot in other_tasks:
            lines.append(f"> {(ot.get('title') or '').strip()}")

    return "\n".join(lines)


async def list_projects(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    async with get_db() as db:
        if include_inactive:
            cur = await db.execute(
                """
                SELECT * FROM hospital_projects
                ORDER BY is_active DESC, sort_order ASC, id ASC
                """
            )
        else:
            cur = await db.execute(
                """
                SELECT * FROM hospital_projects
                WHERE is_active = 1
                ORDER BY sort_order ASC, id ASC
                """
            )
        rows = await cur.fetchall()
        projects = [_row(r) for r in rows if r]
        pids = [int(p["id"]) for p in projects]
        pct_map = await _percent_complete_by_project_ids(db, pids)
    for p in projects:
        p["percent_complete"] = pct_map.get(int(p["id"]), 0)
    return projects


async def get_project(project_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM hospital_projects WHERE id = ?",
            (project_id,),
        )
        row = _row(await cur.fetchone())
        if not row:
            return None
        pct_map = await _percent_complete_by_project_ids(db, [project_id])
        row["percent_complete"] = pct_map.get(project_id, 0)
        return row


async def create_project(body: dict[str, Any]) -> dict[str, Any]:
    b = body or {}
    nm = str(b.get("name") or "").strip()
    if not nm:
        raise ValueError("name required")
    cd = _slug_code(b.get("code") or nm)
    status = str(b.get("status") or "In Progress")
    pct = 0  # % จากงานในโครงการเท่านั้น — โครงการใหม่เริ่มที่ 0
    curst = str(b.get("current_status") or "")
    sort_order = int(b.get("sort_order") or 0)
    description = str(b.get("description") or "")
    start_date = b.get("start_date")
    end_date = b.get("end_date")
    due_date = b.get("due_date")
    implementation_date = b.get("implementation_date")
    next_step = str(b.get("next_step") or "")
    notes = str(b.get("notes") or "")
    vendor = str(b.get("vendor") or "")
    owner = str(b.get("owner") or "")
    priority = str(b.get("priority") or "Medium")

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_projects (
                name, code, status, percent_complete, current_status, sort_order,
                description, start_date, end_date, due_date, implementation_date,
                next_step, notes, vendor, owner, priority, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                nm,
                cd,
                status,
                pct,
                curst,
                sort_order,
                description,
                _s(start_date) or None,
                _s(end_date) or None,
                _s(due_date) or None,
                _s(implementation_date) or None,
                next_step,
                notes,
                vendor,
                owner,
                priority,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        rid = int((await cur.fetchone())["id"])
    row = await get_project(rid)
    if not row:
        raise RuntimeError("insert failed")
    return row


async def update_project(project_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in PROJECT_WRITE_KEYS:
            continue
        if k == "code" and v is not None:
            v = _slug_code(str(v))
        if k in ("start_date", "end_date", "due_date", "implementation_date") and v == "":
            v = None
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return await get_project(project_id)
    sets.append("updated_at = datetime('now')")
    vals.append(project_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE hospital_projects SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()
    return await get_project(project_id)


async def delete_project_soft(project_id: int) -> bool:
    r = await update_project(project_id, {"is_active": 0})
    return r is not None


async def restore_project(project_id: int) -> dict[str, Any] | None:
    """Set hospital_projects.is_active = 1 (undo soft close)."""
    existing = await get_project(project_id)
    if not existing:
        return None
    row = await update_project(project_id, {"is_active": 1})
    if row:
        await refresh_project_percent_from_tasks(project_id)
    return row


async def list_tasks(project_id: int) -> list[dict[str, Any]]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT * FROM hospital_project_tasks
            WHERE project_id = ? AND is_active = 1
            ORDER BY sort_order ASC, id ASC
            """,
            (project_id,),
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_task(project_id: int, body: dict[str, Any]) -> dict[str, Any]:
    b = body or {}
    parent = await get_project(project_id)
    if not parent or int(parent.get("is_active") or 0) != 1:
        raise ValueError("โครงการถูกปิดหรือไม่พบ ไม่สามารถเพิ่มงานได้")
    t = str(b.get("title") or "").strip()
    if not t:
        raise ValueError("title required")
    status = str(b.get("status") or "open")
    due_hint = str(b.get("due_hint") or "")
    raw_so = b.get("sort_order")
    sort_order_val: int | None = None
    if raw_so not in (None, ""):
        try:
            sort_order_val = int(raw_so)
        except (TypeError, ValueError):
            sort_order_val = None
    use_auto_sort = sort_order_val is None or sort_order_val <= 0
    details = str(b.get("details") or "")
    notes = str(b.get("notes") or "")
    start_date = b.get("start_date")
    end_date = b.get("end_date")
    due_date = b.get("due_date")

    async with get_db() as db:
        if use_auto_sort:
            curm = await db.execute(
                """
                SELECT COALESCE(MAX(sort_order), 0) AS m
                FROM hospital_project_tasks
                WHERE project_id = ? AND is_active = 1
                """,
                (project_id,),
            )
            rm = await curm.fetchone()
            sort_order = int(rm["m"] if rm and rm["m"] is not None else 0) + 1
        else:
            sort_order = int(sort_order_val or 1)

        await db.execute(
            """
            INSERT INTO hospital_project_tasks (
                project_id, title, status, due_hint, sort_order,
                details, start_date, end_date, due_date, notes, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                project_id,
                t,
                status,
                due_hint,
                sort_order,
                details,
                _s(start_date) or None,
                _s(end_date) or None,
                _s(due_date) or None,
                notes,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        tid = int((await cur.fetchone())["id"])
        cur2 = await db.execute(
            "SELECT * FROM hospital_project_tasks WHERE id = ?", (tid,)
        )
        row = _row(await cur2.fetchone()) or {}
    await refresh_project_percent_from_tasks(project_id)
    return row


async def update_task(task_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT project_id FROM hospital_project_tasks WHERE id = ?",
            (task_id,),
        )
        tr = await cur.fetchone()
    if not tr:
        return None
    old_project_id = int(tr["project_id"])
    parent = await get_project(old_project_id)
    if not parent or int(parent.get("is_active") or 0) != 1:
        raise ValueError("โครงการถูกปิดแล้ว กู้คืนโครงการก่อนแก้ไขงาน")

    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in TASK_WRITE_KEYS:
            continue
        if k in ("start_date", "end_date", "due_date") and v == "":
            v = None
        if k == "sort_order":
            try:
                v = int(v) if v not in ("", None) else 0
            except (TypeError, ValueError):
                continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT * FROM hospital_project_tasks WHERE id = ?", (task_id,)
            )
            return _row(await cur.fetchone())
    sets.append("updated_at = datetime('now')")
    vals.append(task_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE hospital_project_tasks SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM hospital_project_tasks WHERE id = ?", (task_id,)
        )
        updated = _row(await cur.fetchone())
    if updated:
        new_pid = int(updated["project_id"])
        await refresh_project_percent_from_tasks(new_pid)
        if new_pid != old_project_id:
            await refresh_project_percent_from_tasks(old_project_id)
    return updated


async def delete_task(task_id: int) -> bool:
    async with get_db() as db:
        cur0 = await db.execute(
            "SELECT project_id FROM hospital_project_tasks WHERE id = ? AND is_active = 1",
            (task_id,),
        )
        tr = await cur0.fetchone()
        if not tr:
            return False
        project_id = int(tr["project_id"])
        cur = await db.execute(
            """
            UPDATE hospital_project_tasks
            SET is_active = 0, updated_at = datetime('now')
            WHERE id = ? AND is_active = 1
            """,
            (task_id,),
        )
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        await refresh_project_percent_from_tasks(project_id)
    return ok


async def list_projects_with_tasks(
    *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Projects with nested active tasks (single round-trip for admin UI)."""
    projects = await list_projects(include_inactive=include_inactive)
    if not projects:
        return []
    pids = [int(p["id"]) for p in projects]
    async with get_db() as db:
        ph = ",".join("?" * len(pids))
        cur = await db.execute(
            f"""
            SELECT * FROM hospital_project_tasks
            WHERE project_id IN ({ph}) AND is_active = 1
            ORDER BY project_id, sort_order ASC, id ASC
            """,
            pids,
        )
        rows = await cur.fetchall()
    task_map: dict[int, list[dict[str, Any]]] = {pid: [] for pid in pids}
    for r in rows:
        d = _row(r)
        if not d:
            continue
        task_map[int(d["project_id"])].append(d)
    out: list[dict[str, Any]] = []
    for p in projects:
        pid = int(p["id"])
        tlist = task_map[pid]
        out.append(
            {
                **p,
                "tasks": tlist,
                "percent_complete": percent_complete_from_tasks(tlist),
            }
        )
    return out


async def list_issues(project_id: int | None = None) -> list[dict[str, Any]]:
    async with get_db() as db:
        if project_id is None:
            cur = await db.execute(
                """
                SELECT i.*, p.name AS project_name, p.code AS project_code
                FROM hospital_issues i
                LEFT JOIN hospital_projects p ON p.id = i.project_id
                WHERE i.is_active = 1
                ORDER BY i.id DESC
                """
            )
        else:
            cur = await db.execute(
                """
                SELECT i.*, p.name AS project_name, p.code AS project_code
                FROM hospital_issues i
                LEFT JOIN hospital_projects p ON p.id = i.project_id
                WHERE i.project_id = ? AND i.is_active = 1
                ORDER BY i.id DESC
                """,
                (project_id,),
            )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_issue(body: dict[str, Any]) -> dict[str, Any]:
    b = body or {}
    t = str(b.get("title") or "").strip()
    if not t:
        raise ValueError("title required")
    raw = b.get("project_id")
    if raw in (None, ""):
        pid = None
    else:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            pid = None
    severity = str(b.get("severity") or "medium")
    status = str(b.get("status") or "open")
    details = str(b.get("details") or "")
    system_name = str(b.get("system_name") or "")
    impact = str(b.get("impact") or "")
    priority = str(b.get("priority") or "Medium")
    start_date = b.get("start_date")
    end_date = b.get("end_date")
    due_date = b.get("due_date")
    what_done = str(b.get("what_done") or "")
    next_step = str(b.get("next_step") or "")
    notes = str(b.get("notes") or "")

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_issues (
                project_id, title, severity, status, details,
                system_name, impact, priority, start_date, end_date, due_date,
                what_done, next_step, notes, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                pid,
                t,
                severity,
                status,
                details,
                system_name,
                impact,
                priority,
                _s(start_date) or None,
                _s(end_date) or None,
                _s(due_date) or None,
                what_done,
                next_step,
                notes,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        iid = int((await cur.fetchone())["id"])
        cur2 = await db.execute(
            """
            SELECT i.*, p.name AS project_name, p.code AS project_code
            FROM hospital_issues i
            LEFT JOIN hospital_projects p ON p.id = i.project_id
            WHERE i.id = ?
            """,
            (iid,),
        )
        return _row(await cur2.fetchone()) or {}


async def update_issue(issue_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in ISSUE_WRITE_KEYS:
            continue
        if k in ("start_date", "end_date", "due_date") and v == "":
            v = None
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        async with get_db() as db:
            cur = await db.execute(
                """
                SELECT i.*, p.name AS project_name, p.code AS project_code
                FROM hospital_issues i
                LEFT JOIN hospital_projects p ON p.id = i.project_id
                WHERE i.id = ?
                """,
                (issue_id,),
            )
            return _row(await cur.fetchone())
    sets.append("updated_at = datetime('now')")
    vals.append(issue_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE hospital_issues SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()
        cur = await db.execute(
            """
            SELECT i.*, p.name AS project_name, p.code AS project_code
            FROM hospital_issues i
            LEFT JOIN hospital_projects p ON p.id = i.project_id
            WHERE i.id = ?
            """,
            (issue_id,),
        )
        return _row(await cur.fetchone())


async def delete_issue(issue_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            """
            UPDATE hospital_issues
            SET is_active = 0, updated_at = datetime('now')
            WHERE id = ? AND is_active = 1
            """,
            (issue_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_other_tasks() -> list[dict[str, Any]]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT * FROM hospital_other_tasks
            WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
            """
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_other_task(body: dict[str, Any]) -> dict[str, Any]:
    b = body or {}
    t = str(b.get("title") or "").strip()
    if not t:
        raise ValueError("title required")
    status = str(b.get("status") or "open")
    notes = str(b.get("notes") or "")
    sort_order = int(b.get("sort_order") or 0)
    details = str(b.get("details") or "")
    priority = str(b.get("priority") or "Medium")
    requester = str(b.get("requester") or "")
    start_date = b.get("start_date")
    end_date = b.get("end_date")
    due_date = b.get("due_date")
    related = b.get("related_project_id")
    try:
        rid = int(related) if related not in (None, "") else None
    except (TypeError, ValueError):
        rid = None

    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_other_tasks (
                title, status, notes, sort_order, details, priority, requester,
                start_date, end_date, due_date, related_project_id, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                t,
                status,
                notes,
                sort_order,
                details,
                priority,
                requester,
                _s(start_date) or None,
                _s(end_date) or None,
                _s(due_date) or None,
                rid,
            ),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        oid = int((await cur.fetchone())["id"])
        cur2 = await db.execute(
            "SELECT * FROM hospital_other_tasks WHERE id = ?", (oid,)
        )
        return _row(await cur2.fetchone()) or {}


async def update_other_task(ot_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in OTHER_WRITE_KEYS:
            continue
        if k in ("start_date", "end_date", "due_date", "related_project_id") and v == "":
            v = None
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        async with get_db() as db:
            cur = await db.execute(
                "SELECT * FROM hospital_other_tasks WHERE id = ?", (ot_id,)
            )
            return _row(await cur.fetchone())
    sets.append("updated_at = datetime('now')")
    vals.append(ot_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE hospital_other_tasks SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()
        cur = await db.execute(
            "SELECT * FROM hospital_other_tasks WHERE id = ?", (ot_id,)
        )
        return _row(await cur.fetchone())


async def delete_other_task(ot_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            """
            UPDATE hospital_other_tasks
            SET is_active = 0, updated_at = datetime('now')
            WHERE id = ? AND is_active = 1
            """,
            (ot_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def _count_active(
    db: aiosqlite.Connection, table: str, active_col: str = "is_active"
) -> int:
    try:
        cur = await db.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {active_col} = 1"
        )
        r = await cur.fetchone()
        return int(r["c"]) if r else 0
    except Exception:
        cur = await db.execute(f"SELECT COUNT(*) AS c FROM {table}")
        r = await cur.fetchone()
        return int(r["c"]) if r else 0


def hospital_admin_sync_info() -> dict[str, str]:
    """Lightweight server clock + report date label for admin clients."""
    now = datetime.now(_TZ_BKK)
    return {
        "report_date_label": format_report_date_bkk(now),
        "server_time_ict7": now.isoformat(),
    }


async def build_hospital_dashboard_summary() -> dict[str, Any]:
    """Aggregate active projects/tasks/issues/other for Summary dashboard (is_active=1 only)."""
    now = datetime.now(_TZ_BKK)
    generated_at = now.isoformat()

    async with get_db() as db:
        proj_cur = await db.execute(
            """
            SELECT * FROM hospital_projects
            WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
            """
        )
        projects_raw = [_row(r) for r in await proj_cur.fetchall() if r]

        task_cur = await db.execute(
            """
            SELECT t.*, p.name AS project_name, p.code AS project_code
            FROM hospital_project_tasks t
            INNER JOIN hospital_projects p ON p.id = t.project_id AND p.is_active = 1
            WHERE t.is_active = 1
            ORDER BY p.sort_order ASC, p.id ASC, t.sort_order ASC, t.id ASC
            """
        )
        tasks_raw = [_row(r) for r in await task_cur.fetchall() if r]

        issues_cur = await db.execute(
            """
            SELECT i.*, p.name AS project_name
            FROM hospital_issues i
            LEFT JOIN hospital_projects p ON p.id = i.project_id AND p.is_active = 1
            WHERE i.is_active = 1
              AND (i.project_id IS NULL OR p.id IS NOT NULL)
            ORDER BY
                CASE i.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                i.id DESC
            """
        )
        issues_raw = [_row(r) for r in await issues_cur.fetchall() if r]

        ot_cur = await db.execute(
            """
            SELECT o.*, p.name AS related_project_name
            FROM hospital_other_tasks o
            LEFT JOIN hospital_projects p ON p.id = o.related_project_id AND p.is_active = 1
            WHERE o.is_active = 1
              AND (o.related_project_id IS NULL OR p.id IS NOT NULL)
            ORDER BY o.sort_order ASC, o.id ASC
            """
        )
        other_raw = [_row(r) for r in await ot_cur.fetchall() if r]

    issues_open = [
        d for d in issues_raw if d and _issue_open_for_report(d.get("status"))
    ]

    open_issue_by_pid: dict[int, int] = {}
    for iss in issues_open:
        pid = iss.get("project_id")
        if pid is None:
            continue
        try:
            ip = int(pid)
        except (TypeError, ValueError):
            continue
        open_issue_by_pid[ip] = open_issue_by_pid.get(ip, 0) + 1

    open_task_by_pid: dict[int, int] = {}
    all_tasks_out: list[dict[str, Any]] = []
    rank_by_pid: dict[int, int] = {}
    task_by_pid: dict[int, list[dict[str, Any]]] = {}
    for t in tasks_raw:
        if not t:
            continue
        pid = int(t["project_id"])
        task_by_pid.setdefault(pid, []).append(t)
        if _task_not_done(t.get("status")):
            open_task_by_pid[pid] = open_task_by_pid.get(pid, 0) + 1
        rank_by_pid[pid] = rank_by_pid.get(pid, 0) + 1
        so = int(t.get("sort_order") or 0)
        all_tasks_out.append(
            {
                "id": int(t["id"]),
                "project_id": pid,
                "project_name": t.get("project_name") or "",
                "project_code": t.get("project_code") or "",
                "title": t.get("title") or "",
                "status": t.get("status") or "",
                "due_hint": t.get("due_hint") or "",
                "due_date": t.get("due_date") or "",
                "due_date_display": format_report_date_value(t.get("due_date")),
                "start_date": t.get("start_date") or "",
                "end_date": t.get("end_date") or "",
                "details": t.get("details") or "",
                "notes": t.get("notes") or "",
                "sort_order": so,
                "rank_in_project": rank_by_pid[pid],
            }
        )

    projects_out: list[dict[str, Any]] = []
    for p in projects_raw:
        if not p:
            continue
        pid = int(p["id"])
        projects_out.append(
            {
                "id": pid,
                "name": p.get("name") or "",
                "code": p.get("code") or "",
                "status": p.get("status") or "",
                "percent_complete": percent_complete_from_tasks(
                    task_by_pid.get(pid, [])
                ),
                "next_step": p.get("next_step") or "",
                "implementation_date": p.get("implementation_date") or "",
                "implementation_date_display": format_report_date_value(
                    p.get("implementation_date")
                ),
                "due_date": p.get("due_date") or "",
                "due_date_display": format_report_date_value(p.get("due_date")),
                "open_task_count": open_task_by_pid.get(pid, 0),
                "issue_count": open_issue_by_pid.get(pid, 0),
            }
        )

    issues_out = [
        {
            "id": int(x["id"]),
            "title": x.get("title") or "",
            "system_name": x.get("system_name") or "",
            "priority": x.get("priority") or "",
            "severity": x.get("severity") or "",
            "status": x.get("status") or "",
            "next_step": x.get("next_step") or "",
            "project_id": x.get("project_id"),
            "project_name": x.get("project_name") or "",
        }
        for x in issues_open
    ]

    other_out: list[dict[str, Any]] = []
    for o in other_raw:
        if not o:
            continue
        other_out.append(
            {
                "id": int(o["id"]),
                "title": o.get("title") or "",
                "status": o.get("status") or "",
                "priority": o.get("priority") or "",
                "requester": o.get("requester") or "",
                "related_project_id": o.get("related_project_id"),
                "related_project_name": o.get("related_project_name") or "",
                "due_date": o.get("due_date") or "",
                "due_date_display": format_report_date_value(o.get("due_date")),
                "details": o.get("details") or "",
                "notes": o.get("notes") or "",
            }
        )

    due_today = 0
    for t in tasks_raw:
        if not t:
            continue
        if _task_not_done(t.get("status")) and _is_due_today(
            t.get("due_hint"), t.get("due_date"), now
        ):
            due_today += 1
    for o in other_raw:
        if not o:
            continue
        if _task_not_done(o.get("status")) and _is_due_today(
            None, o.get("due_date"), now
        ):
            due_today += 1

    need_attention = 0
    for iss in issues_open:
        if str(iss.get("severity") or "").strip().lower() == "high":
            need_attention += 1
    for t in tasks_raw:
        if not t:
            continue
        if _task_not_done(t.get("status")) and _is_overdue_iso(
            str(t.get("due_date") or ""), now
        ):
            need_attention += 1
    for o in other_raw:
        if not o:
            continue
        if _task_not_done(o.get("status")) and _is_overdue_iso(
            str(o.get("due_date") or ""), now
        ):
            need_attention += 1

    summary = {
        "active_projects": len(projects_out),
        "open_issues": len(issues_open),
        "project_tasks": len(all_tasks_out),
        "other_tasks": len(other_out),
        "due_today": due_today,
        "need_attention": need_attention,
    }

    return {
        "summary": summary,
        "generated_at": generated_at,
        "projects": projects_out,
        "all_project_tasks": all_tasks_out,
        "issues": issues_out,
        "other_tasks": other_out,
    }


async def build_daily_report_preview() -> dict[str, Any]:
    now = datetime.now(_TZ_BKK)
    generated_at = now.isoformat()
    mention = (await get_config("standup_mention", "@Noom")).strip() or "@Noom"

    async with get_db() as db:
        proj_cur = await db.execute(
            """
            SELECT * FROM hospital_projects
            WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
            """
        )
        projects_raw = [dict(r) for r in await proj_cur.fetchall()]

        issues_cur = await db.execute(
            """
            SELECT i.*, p.name AS project_name
            FROM hospital_issues i
            LEFT JOIN hospital_projects p ON p.id = i.project_id AND p.is_active = 1
            WHERE i.is_active = 1
              AND (i.project_id IS NULL OR p.id IS NOT NULL)
            ORDER BY
                CASE i.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                i.id DESC
            """
        )
        issues_raw = [dict(r) for r in await issues_cur.fetchall()]

        ot_cur = await db.execute(
            """
            SELECT o.*
            FROM hospital_other_tasks o
            LEFT JOIN hospital_projects p ON p.id = o.related_project_id AND p.is_active = 1
            WHERE o.is_active = 1
              AND (o.related_project_id IS NULL OR p.id IS NOT NULL)
            ORDER BY o.sort_order ASC, o.id ASC
            """
        )
        other_raw = [dict(r) for r in await ot_cur.fetchall()]

        pids = [int(p["id"]) for p in projects_raw]
        task_map: dict[int, list[dict[str, Any]]] = {pid: [] for pid in pids}
        if pids:
            ph = ",".join("?" * len(pids))
            tcur = await db.execute(
                f"""
                SELECT * FROM hospital_project_tasks
                WHERE project_id IN ({ph}) AND is_active = 1
                ORDER BY project_id, sort_order ASC, id ASC
                """,
                pids,
            )
            for row in await tcur.fetchall():
                d = dict(row)
                task_map.setdefault(int(d["project_id"]), []).append(d)

        evidence = {
            "hospital_projects": await _count_active(db, "hospital_projects"),
            "hospital_project_tasks": await _count_active(
                db, "hospital_project_tasks"
            ),
            "hospital_issues": await _count_active(db, "hospital_issues"),
            "hospital_other_tasks": await _count_active(db, "hospital_other_tasks"),
        }

    issues_open = [r for r in issues_raw if _issue_open_for_report(r.get("status"))]

    projects_out: list[dict[str, Any]] = []
    for p in projects_raw:
        pid = int(p["id"])
        tasks = task_map.get(pid, [])
        projects_out.append(
            {
                "id": pid,
                "name": p["name"],
                "code": p["code"],
                "status": p["status"],
                "percent_complete": percent_complete_from_tasks(tasks),
                "current_status": p.get("current_status") or "",
                "start_date": p.get("start_date") or "",
                "end_date": p.get("end_date") or "",
                "due_date": p.get("due_date") or "",
                "implementation_date": p.get("implementation_date") or "",
                "next_step": p.get("next_step") or "",
                "tasks": [
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "status": t["status"],
                        "due_hint": t.get("due_hint") or "",
                        "due_date": t.get("due_date") or "",
                        "start_date": t.get("start_date") or "",
                        "end_date": t.get("end_date") or "",
                    }
                    for t in tasks
                ],
            }
        )

    issues_out = [
        {
            "id": r["id"],
            "title": r["title"],
            "severity": r["severity"],
            "status": r["status"],
            "details": (r.get("details") or ""),
            "next_step": r.get("next_step") or "",
            "start_date": r.get("start_date") or "",
            "end_date": r.get("end_date") or "",
            "due_date": r.get("due_date") or "",
            "project_id": r.get("project_id"),
            "project_name": r.get("project_name"),
        }
        for r in issues_raw
    ]

    other_out = list(other_raw)

    report_text = _build_list_today_report_text(
        mention=mention,
        now=now,
        projects=projects_out,
        issues_open=issues_open,
        other_tasks=other_out,
    )

    return {
        "generated_at": generated_at,
        "date_label": format_report_date_bkk(now),
        "projects": projects_out,
        "issues": issues_out,
        "other_tasks": other_out,
        "report_text": report_text,
        "ai_review": {
            "mode": "placeholder",
            "message_th": "Phase 1: ไม่มี LLM parse / auto review — แสดงเฉพาะหลักฐานจากจำนวนแถว (เฉพาะ is_active=1)",
            "evidence_row_counts": evidence,
        },
    }


def build_hospital_work_html() -> str:
    return """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>Hospital Work — Ener-AI Admin</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a0a;color:#e5e7eb;font-family:system-ui,sans-serif;min-height:100vh}
  .header{background:#111;border-bottom:1px solid #222;padding:16px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .header h1{font-size:1.15rem;font-weight:700;color:#f9fafb}
  .back-btn,.btn{background:#1e293b;color:#94a3b8;border:1px solid #334;padding:6px 14px;border-radius:6px;text-decoration:none;font-size:0.85rem;cursor:pointer}
  .back-btn:hover,.btn:hover{background:#273449;color:#e2e8f0}
  .btn-primary{background:#14532d;color:#bbf7d0;border-color:#166534}
  .btn-primary:hover{background:#166534;color:#fff}
  .btn-danger{background:#450a0a;color:#fecaca;border-color:#7f1d1d}
  .container{max-width:1320px;margin:24px auto;padding:0 24px 48px}
  section{background:#111;border:1px solid #222;border-radius:10px;padding:16px 18px;margin-bottom:20px}
  section h2{font-size:1rem;color:#93c5fd;margin-bottom:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:900px){.grid2{grid-template-columns:1fr}}
  label{display:block;font-size:0.75rem;color:#9ca3af;margin-bottom:4px}
  input,select,textarea{width:100%;background:#0a0a0a;border:1px solid #333;border-radius:6px;padding:8px 10px;color:#e5e7eb;font-size:0.9rem}
  input[type="date"]{color-scheme:dark}
  textarea{min-height:72px;font-family:ui-monospace,monospace;font-size:0.8rem}
  table{width:100%;border-collapse:collapse;font-size:0.85rem}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #222}
  th{color:#9ca3af;font-weight:600}
  tr:hover td{background:#151515}
  .muted{color:#6b7280;font-size:0.8rem}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:0.72rem;background:#1e293b;color:#cbd5e1}
  .pill-archived{background:#3f3f46;color:#e4e4e7;border:1px solid #52525b}
  .ai-box{border:1px dashed #444;background:#0d0d0d;padding:12px;border-radius:8px;color:#a3a3a3;font-size:0.85rem}
  .row-actions{display:flex;gap:6px;flex-wrap:wrap}
  .err{color:#f87171;font-size:0.85rem;margin-top:8px}
  .subpanel{margin-top:12px;padding:12px;background:#0d0d0d;border:1px solid #262626;border-radius:8px}
  .subpanel h3{font-size:0.95rem;color:#93c5fd;margin-bottom:12px}
  .page-nav{display:flex;flex-wrap:wrap;gap:8px;padding:12px 0 14px;margin-bottom:4px;border-bottom:1px solid #222}
  .page-nav a{color:#93c5fd;text-decoration:none;font-size:0.82rem;padding:6px 12px;border-radius:8px;background:#1e293b;border:1px solid #334}
  .page-nav a:hover{background:#273449;color:#e2e8f0}
  section[id^="sec-"]{scroll-margin-top:72px}
  table.proj-table{table-layout:fixed;width:100%}
  .proj-table .col-code{width:10%;vertical-align:top}
  .proj-table .col-name{min-width:200px;width:34%;word-break:break-word;line-height:1.4;vertical-align:top}
  .proj-table .col-pct{width:76px;vertical-align:top}
  .proj-table .col-st{width:120px;vertical-align:top}
  .proj-table .col-cs{min-width:140px;vertical-align:top}
  .proj-table .col-actions{width:1%;white-space:nowrap;vertical-align:top}
  .proj-table input[type=text],.proj-table input[type=number]{min-width:0;max-width:100%}
  tr.proj-main.proj-archived td{background:#12121a}
  tr.proj-tasks-archived .proj-task-wrap{opacity:0.95}
  .proj-task-wrap{padding:12px 12px 4px}
  .proj-task-scroll{max-height:min(360px,50vh);overflow-y:auto;overflow-x:auto;margin-bottom:8px;padding-right:4px}
  .task-card-edit{border:1px solid #262626;border-radius:8px;padding:10px;margin-bottom:10px;background:#111}
  .task-grid{display:grid;grid-template-columns:84px minmax(120px,2fr) 100px minmax(80px,1fr) 130px 130px 130px;gap:8px;align-items:end}
  @media(max-width:1100px){.task-grid{grid-template-columns:1fr 1fr}}
  .toggle-proj-tasks{margin-bottom:8px}
  .add-task-section{margin-top:4px;padding-top:12px;border-top:1px dashed #333}
  .field-group{margin-top:14px;padding-top:14px;border-top:1px solid #333}
  .field-group:first-of-type{margin-top:0;padding-top:0;border-top:none}
  .field-group h4{font-size:0.72rem;color:#94a3b8;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:10px;font-weight:600}
  #report-preview{font-family:ui-monospace,monospace;font-size:0.8rem;line-height:1.5;min-height:360px;max-height:min(70vh,520px);resize:vertical;overflow-y:auto}
  .report-meta{display:flex;flex-wrap:wrap;gap:12px 20px;margin-bottom:12px;font-size:0.85rem;color:#9ca3af}
  .report-toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;align-items:center}
  .summary-section{margin-bottom:22px}
  .summary-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));gap:12px;margin-bottom:8px}
  .sum-card{background:#151515;border:1px solid #2a2a2a;border-radius:10px;padding:12px 14px}
  .sum-card b{display:block;font-size:1.28rem;color:#f9fafb;line-height:1.2}
  .sum-card span{display:block;font-size:0.72rem;color:#9ca3af;margin-top:4px;line-height:1.35}
  .sum-h3{font-size:0.92rem;color:#93c5fd;margin:20px 0 8px;font-weight:600}
  .sum-table-wrap{overflow-x:auto;margin-bottom:4px;border-radius:8px;border:1px solid #222}
  .sum-table{font-size:0.8rem}
  .sum-filters{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin-bottom:10px}
  .sum-filters label{display:flex;flex-direction:column;gap:4px;font-size:0.72rem;color:#9ca3af;margin:0}
  .sum-filters select,.sum-filters input[type="checkbox"]{width:auto;min-width:0}
  .sum-filters .chk-inline{flex-direction:row;align-items:center;gap:6px}
</style>
</head>
<body>
<header class="header">
  <a class="back-btn" href="/admin">← Admin</a>
  <h1>Hospital Work Dashboard <span class="muted">(Phase 1)</span></h1>
  <span id="sync-status" class="muted" style="font-size:0.8rem;margin-left:auto;min-width:12rem;text-align:right" aria-live="polite"></span>
  <button type="button" class="btn btn-primary" id="btn-refresh">รีเฟรช</button>
</header>
<div class="container">
  <nav class="page-nav" aria-label="Hospital Work sections">
    <a href="#sec-summary">Summary</a>
    <a href="#sec-overview">ภาพรวม</a>
    <a href="#sec-projects">โครงการ</a>
    <a href="#sec-issues">Issues</a>
    <a href="#sec-other">Other Tasks</a>
    <a href="#sec-report">Daily Report</a>
    <a href="#sec-ai">AI Review</a>
  </nav>

  <section id="sec-summary" class="summary-section">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:14px">
      <h2 style="margin:0;font-size:1.05rem;color:#f9fafb">Summary Dashboard</h2>
      <div class="row-actions" style="align-items:center">
        <button type="button" class="btn btn-primary" id="btn-refresh-summary">รีเฟรช Summary</button>
        <span class="muted" id="sum-generated" style="font-size:0.78rem;max-width:20rem"></span>
      </div>
    </div>
    <div class="summary-cards" id="sum-cards"></div>

    <h3 class="sum-h3">Overview — โครงการที่เปิดอยู่</h3>
    <div class="sum-table-wrap">
      <table class="sum-table"><thead><tr><th>โครงการ</th><th>สถานะ</th><th>%</th><th>Next step</th><th>Impl / Due</th><th>งานเปิด</th><th>Issues</th><th></th></tr></thead>
      <tbody id="tb-sum-projects"></tbody></table>
    </div>

    <h3 class="sum-h3">งานในโครงการทั้งหมด</h3>
    <div class="sum-filters">
      <label>สถานะ
        <select id="sum-f-status"><option value="">ทั้งหมด</option><option>open</option><option>in_progress</option><option>done</option></select>
      </label>
      <label>โครงการ
        <select id="sum-f-proj"><option value="">ทั้งหมด</option></select>
      </label>
      <label class="chk-inline"><input type="checkbox" id="sum-f-due-today"> due วันนี้</label>
      <label class="chk-inline"><input type="checkbox" id="sum-f-due-week"> due สัปดาห์นี้</label>
    </div>
    <div class="sum-table-wrap">
      <table class="sum-table"><thead><tr><th>#</th><th>โครงการ</th><th>งาน</th><th>สถานะ</th><th>due_hint</th><th>due</th><th>รายละเอียด</th><th></th></tr></thead>
      <tbody id="tb-sum-all-tasks"></tbody></table>
    </div>

    <h3 class="sum-h3">Issues ที่ยังไม่ปิด</h3>
    <div class="sum-table-wrap">
      <table class="sum-table"><thead><tr><th>หัวข้อ</th><th>system</th><th>Pri / Sev</th><th>สถานะ</th><th>next_step</th><th>โครงการ</th></tr></thead>
      <tbody id="tb-sum-issues"></tbody></table>
    </div>

    <h3 class="sum-h3">Other Tasks (active)</h3>
    <div class="sum-table-wrap">
      <table class="sum-table"><thead><tr><th>หัวข้อ</th><th>สถานะ</th><th>priority</th><th>requester</th><th>โครงการ</th><th>due</th><th></th></tr></thead>
      <tbody id="tb-sum-other"></tbody></table>
    </div>
  </section>

  <div id="sec-overview">
    <p class="muted" style="margin-bottom:12px">CRUD จาก DB — ลบงาน/Issue/Other = soft delete (is_active=0) • ปิดโครงการ = soft delete โครงการ (กู้คืนได้ ไม่มี hard delete ใน Phase นี้)</p>
    <p class="muted" style="margin-bottom:8px;font-size:0.8rem">สรุปภาพรวมอยู่ที่แท็บ <strong>Summary</strong> ด้านบน (การ์ด + ตารางงานรวม + Issues / Other) — จากนั้นเป็น CRUD รายละเอียดด้านล่าง • งานในโครงการแสดงใต้แต่ละโครงการในตาราง • วันที่ใช้ปฏิทิน เก็บ YYYY-MM-DD แสดงในรายงานเป็น 13-May-2026</p>
  </div>

  <section id="sec-projects">
      <h2>โครงการ</h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div><label>ชื่อ</label><input id="np-name" placeholder="ชื่อโครงการ"></div>
        <div><label>รหัส (slug)</label><input id="np-code" placeholder="เว้นว่างให้ auto"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div><label>Priority</label><select id="np-priority"><option>High</option><option selected>Medium</option><option>Low</option></select></div>
        <div><label>Implementation date</label><input id="np-impl" type="date"></div>
      </div>
      <div style="margin-bottom:10px"><label>Next step</label><input id="np-next" placeholder="ขั้นตอนถัดไป"></div>
      <div style="margin-bottom:10px"><label>description</label><textarea id="np-desc" placeholder="รายละเอียดโครงการ"></textarea></div>
      <button type="button" class="btn btn-primary" id="btn-add-project">เพิ่มโครงการ</button>
      <div class="err" id="err-projects"></div>
      <div style="display:flex;align-items:center;gap:12px;margin:14px 0 10px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:0.85rem;color:#d1d5db;margin:0">
          <input type="checkbox" id="cb-show-archived" style="width:auto;margin:0">
          แสดงโครงการที่ปิดแล้ว (Archived)
        </label>
        <span class="muted" style="font-size:0.78rem">ใช้ soft delete — กู้คืนได้จากมุมมองนี้</span>
      </div>
      <div class="proj-wrap" style="overflow-x:auto;margin-top:14px">
        <table class="proj-table"><thead><tr><th class="col-code">รหัส</th><th class="col-name">ชื่อโครงการ</th><th class="col-pct" title="คำนวณจากงานในโครงการ: done=100%, in_progress=50% ต่อหัว, เฉลี่ยทั้งหมด">% <span class="muted" style="font-weight:400;font-size:0.72rem">auto</span></th><th class="col-st">สถานะ</th><th class="col-cs">สถานะปัจจุบัน</th><th class="col-actions"></th></tr></thead>
        <tbody id="tb-projects"></tbody></table>
      </div>
      <div id="project-extra" class="subpanel" style="display:none;margin-top:16px">
        <h3>รายละเอียดโครงการ #<span id="pe-id"></span></h3>
        <div class="field-group">
          <h4>Basic</h4>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div><label>Implementation date</label><input id="pe-impl" type="date"></div>
            <div><label>priority</label><select id="pe-priority"><option>High</option><option>Medium</option><option>Low</option></select></div>
          </div>
          <div style="margin-top:8px"><label>next_step</label><input id="pe-next"></div>
          <div style="margin-top:8px"><label>description</label><textarea id="pe-desc"></textarea></div>
        </div>
        <div class="field-group">
          <h4>Dates</h4>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px">
            <div><label>Start date</label><input id="pe-sd" type="date"></div>
            <div><label>End date</label><input id="pe-ed" type="date"></div>
            <div><label>Due date</label><input id="pe-dd" type="date"></div>
          </div>
        </div>
        <div class="field-group">
          <h4>Vendor</h4>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div><label>vendor</label><input id="pe-vendor"></div>
            <div><label>owner</label><input id="pe-owner"></div>
          </div>
        </div>
        <div class="field-group">
          <h4>Notes</h4>
          <div><label>notes</label><textarea id="pe-notes"></textarea></div>
        </div>
        <button type="button" class="btn btn-primary" style="margin-top:14px" id="btn-save-project-extra">บันทึกรายละเอียด</button>
        <div class="err" id="err-pe"></div>
      </div>
  </section>

  <div class="grid2">
    <section id="sec-issues">
      <h2>Issues</h2>
      <div style="display:grid;grid-template-columns:1fr 120px;gap:8px;margin-bottom:8px">
        <div><label>หัวข้อ</label><input id="ni-title"></div>
        <div><label>Severity</label>
          <select id="ni-sev"><option>low</option><option>medium</option><option>high</option></select>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div><label>priority</label><select id="ni-priority"><option>High</option><option selected>Medium</option><option>Low</option></select></div>
        <div><label>system_name</label><input id="ni-sys"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div><label>impact</label><input id="ni-impact"></div>
        <div><label>what_done</label><input id="ni-what"></div>
      </div>
      <div style="margin-bottom:8px"><label>next_step</label><input id="ni-next"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px">
        <div><label>Start date</label><input id="ni-sd" type="date"></div>
        <div><label>End date</label><input id="ni-ed" type="date"></div>
        <div><label>Due date</label><input id="ni-dd" type="date"></div>
      </div>
      <div style="margin-bottom:8px"><label>รายละเอียด (details)</label><textarea id="ni-details"></textarea></div>
      <div style="margin-bottom:8px"><label>notes</label><textarea id="ni-notes"></textarea></div>
      <div style="margin-bottom:8px"><label>โครงการ (optional)</label>
        <select id="ni-project"><option value="">—</option></select>
      </div>
      <button type="button" class="btn btn-primary" id="btn-add-issue">เพิ่ม Issue</button>
      <div class="err" id="err-issues"></div>
      <div style="overflow-x:auto;margin-top:12px">
        <table><thead><tr><th>Issue</th><th>Sev</th><th>โครงการ</th><th></th></tr></thead>
        <tbody id="tb-issues"></tbody></table>
      </div>
    </section>

    <section id="sec-other">
      <h2>งานอื่น (นอกโครงการ)</h2>
      <div style="margin-bottom:8px"><label>หัวข้อ</label><input id="no-title"></div>
      <div style="margin-bottom:8px"><label>details</label><textarea id="no-details"></textarea></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
        <div><label>priority</label><select id="no-priority"><option>High</option><option selected>Medium</option><option>Low</option></select></div>
        <div><label>requester</label><input id="no-req"></div>
      </div>
      <div style="margin-bottom:8px"><label>related project</label><select id="no-rel"><option value="">—</option></select></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px">
        <div><label>Start date</label><input id="no-sd" type="date"></div>
        <div><label>End date</label><input id="no-ed" type="date"></div>
        <div><label>Due date</label><input id="no-dd" type="date"></div>
      </div>
      <div style="margin-bottom:8px"><label>notes</label><input id="no-notes"></div>
      <button type="button" class="btn btn-primary" id="btn-add-other">เพิ่ม</button>
      <div class="err" id="err-other"></div>
      <div style="overflow-x:auto;margin-top:12px">
        <table><thead><tr><th>งาน</th><th>สถานะ</th><th></th></tr></thead>
        <tbody id="tb-other"></tbody></table>
      </div>
    </section>
  </div>

  <section id="sec-report">
    <h2>Daily Report Preview <span class="muted">(อ่านจาก DB)</span></h2>
    <p class="muted" style="font-size:0.78rem;margin-bottom:10px">ระบบเก็บวันที่เป็น YYYY-MM-DD — ในรายงานจะแสดงเป็นรูปแบบ 13-May-2026</p>
    <div class="report-meta">
      <span id="report-date-label"></span>
      <span id="report-generated-at"></span>
    </div>
    <div class="report-toolbar">
      <button type="button" class="btn btn-primary" id="btn-copy-report">คัดลอกทั้งหมด</button>
      <button type="button" class="btn" id="btn-report-scroll-top">เลื่อนขึ้นบนสุด</button>
    </div>
    <textarea id="report-preview" readonly spellcheck="false"></textarea>
  </section>

  <section id="sec-ai">
    <h2>AI Review <span class="pill">Phase 1 placeholder</span></h2>
    <div class="ai-box" id="ai-review-box">กำลังโหลด…</div>
  </section>
</div>
<script>
const sel = (id) => document.getElementById(id);
let extraProjectId = null;
let _projectsCache = [];

async function api(path, opt) {
  const r = await fetch(path, Object.assign({credentials:'same-origin',headers:{'Content-Type':'application/json'}}, opt||{}));
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
  return j;
}

function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function attr(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }

const _DATE_INPUT_MONTHS = { Jan:0, Feb:1, Mar:2, Apr:3, May:4, Jun:5, Jul:6, Aug:7, Sep:8, Oct:9, Nov:10, Dec:11 };
/** Return YYYY-MM-DD for input[type=date], or '' — supports legacy 13-May-2026 */
function toDateInputValue(v) {
  if (v == null) return '';
  const s = String(v).trim();
  if (!s) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
  const m = s.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/);
  if (m) {
    const day = parseInt(m[1], 10);
    const monStr = m[2].charAt(0).toUpperCase() + m[2].slice(1).toLowerCase();
    const mi = _DATE_INPUT_MONTHS[monStr];
    if (mi === undefined) return '';
    const y = parseInt(m[3], 10);
    const dt = new Date(y, mi, day);
    if (isNaN(dt.getTime()) || dt.getFullYear() !== y || dt.getMonth() !== mi || dt.getDate() !== day) return '';
    const mm = String(mi + 1).padStart(2, '0');
    const dd = String(day).padStart(2, '0');
    return y + '-' + mm + '-' + dd;
  }
  return '';
}

function setSyncStatus(d) {
  const el = sel('sync-status');
  if (!el) return;
  const t = (d instanceof Date) ? d : new Date();
  el.textContent = 'โหลดล่าสุด: ' + t.toLocaleTimeString('th-TH', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fillProjectSelects(projects) {
  const active = projects.filter(p => Number(p.is_active) === 1);
  const ni = sel('ni-project');
  ni.innerHTML = '<option value="">—</option>' + active.map(p =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  const nr = sel('no-rel');
  nr.innerHTML = '<option value="">—</option>' + active.map(p =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join('');
}

function projectsWithTasksUrl() {
  const cb = sel('cb-show-archived');
  return '/admin/api/hospital-work/projects-with-tasks' + ((cb && cb.checked) ? '?include_inactive=1' : '');
}

function taskStatusOpts(cur) {
  const c = cur || 'open';
  return ['open','in_progress','done'].map(s =>
    `<option value="${s}"${s===c?' selected':''}>${s}</option>`).join('');
}

function renderTaskCard(pid, t) {
  const st = t.status || 'open';
  const so = Number(t.sort_order != null ? t.sort_order : 0);
  return `<div class="task-card-edit" id="task-card-${t.id}" data-task-id="${t.id}" data-project-id="${pid}">
    <div class="task-grid">
      <div><label>ลำดับ</label><input class="t-sort" type="number" min="0" step="1" title="เลขน้อยมาก่อน (1,2,3…)" value="${so}"></div>
      <div><label>หัวข้อ</label><input class="t-title" value="${attr(t.title||'')}"></div>
      <div><label>สถานะ</label><select class="t-status">${taskStatusOpts(st)}</select></div>
      <div><label>due_hint</label><input class="t-due-hint" value="${attr(t.due_hint||'')}"></div>
      <div><label>Due date</label><input class="t-due-date" type="date" value="${attr(toDateInputValue(t.due_date))}"></div>
      <div><label>Start date</label><input class="t-sd" type="date" value="${attr(toDateInputValue(t.start_date))}"></div>
      <div><label>End date</label><input class="t-ed" type="date" value="${attr(toDateInputValue(t.end_date))}"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
      <div><label>details</label><input class="t-details" value="${attr(t.details||'')}"></div>
      <div><label>notes</label><textarea class="t-notes" rows="2">${esc(t.notes||'')}</textarea></div>
    </div>
    <div class="row-actions" style="margin-top:8px">
      <button type="button" class="btn btn-primary save-inline-task" data-task-id="${t.id}">บันทึก</button>
      <button type="button" class="btn btn-danger del-inline-task" data-task-id="${t.id}">ลบ</button>
    </div>
  </div>`;
}

function renderAddTaskBlock(pid) {
  return `<div class="add-task-section" data-pid="${pid}">
    <div class="muted" style="font-size:0.78rem;margin-bottom:6px">เพิ่มงานใหม่ <span style="opacity:0.85">• ลำดับ: เลขน้อยขึ้นก่อน — เว้นว่าง = ต่อท้ายคิวอัตโนมัติ</span></div>
    <div class="task-grid">
      <div><label>ลำดับ</label><input class="at-sort" type="number" min="0" step="1" placeholder="อัตโนมัติ" title="เว้นว่างหรือ 0 = ต่อท้าย"></div>
      <div><label>หัวข้อ</label><input class="at-title" placeholder="ชื่องาน"></div>
      <div><label>สถานะ</label><select class="at-status"><option selected>open</option><option>in_progress</option><option>done</option></select></div>
      <div><label>due_hint</label><input class="at-due-hint" placeholder="วันนี้ / สัปดาห์นี้"></div>
      <div><label>Due date</label><input class="at-due-date" type="date"></div>
      <div><label>Start date</label><input class="at-sd" type="date"></div>
      <div><label>End date</label><input class="at-ed" type="date"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px">
      <div><label>details</label><input class="at-details"></div>
      <div><label>notes</label><textarea class="at-notes" rows="2"></textarea></div>
    </div>
    <button type="button" class="btn btn-primary at-add" style="margin-top:8px" data-pid="${pid}">เพิ่มงาน</button>
    <div class="err err-inline-task" data-pid="${pid}" style="margin-top:6px"></div>
  </div>`;
}

function renderProjectWithTasksRows(p) {
  const archived = Number(p.is_active) === 0;
  const tasks = p.tasks || [];
  const n = tasks.length;
  const dis = archived ? ' disabled' : '';
  const badge = archived ? ' <span class="pill pill-archived">Archived</span>' : '';
  const actions = archived
    ? `<button type="button" class="btn extra-proj" data-id="${p.id}">รายละเอียด</button>
      <button type="button" class="btn btn-primary restore-proj" data-id="${p.id}">กู้คืน</button>`
    : `<button type="button" class="btn btn-primary save-proj" data-id="${p.id}">บันทึก</button>
      <button type="button" class="btn extra-proj" data-id="${p.id}">รายละเอียด</button>
      <button type="button" class="btn btn-danger del-proj" data-id="${p.id}">ปิดโครงการ</button>`;
  const mainRow = `<tr class="proj-main${archived ? ' proj-archived' : ''}" id="proj-row-${p.id}" data-id="${p.id}">
    <td class="col-code"><code>${esc(p.code)}</code></td>
    <td class="col-name">${esc(p.name)}${badge}</td>
    <td class="col-pct"><span class="p-pct-val">${p.percent_complete ?? 0}</span><span class="muted" style="font-size:0.72rem">%</span></td>
    <td class="col-st"><input type="text" class="p-st" style="width:100%" value="${attr(p.status)}"${dis}></td>
    <td class="col-cs"><input type="text" class="p-cs" style="width:100%" placeholder="สถานะปัจจุบัน" value="${attr(p.current_status||'')}"${dis}></td>
    <td class="col-actions row-actions">${actions}</td></tr>`;
  let tasksBlock;
  if (archived) {
    tasksBlock = `<tr class="proj-tasks-row proj-tasks-archived" id="proj-tasks-anchor-${p.id}" data-pid="${p.id}">
    <td colspan="6" class="proj-tasks-cell">
      <div class="proj-task-wrap">
        <div class="muted" style="font-size:0.8rem;margin-bottom:6px">งานในโครงการ</div>
        <div class="muted" style="padding:6px 0;line-height:1.55">โครงการถูกปิดแล้ว — งานไม่เข้า Daily Report • มี ${n} รายการในระบบ • กด <strong>กู้คืน</strong> เพื่อแก้ไขงานอีกครั้ง</div>
      </div>
    </td></tr>`;
  } else {
    const taskHtml = tasks.map(t => renderTaskCard(p.id, t)).join('');
    tasksBlock = `<tr class="proj-tasks-row" id="proj-tasks-anchor-${p.id}" data-pid="${p.id}">
    <td colspan="6" class="proj-tasks-cell">
      <div class="proj-task-wrap">
        <div class="muted" style="font-size:0.8rem;margin-bottom:6px">งานในโครงการ</div>
        <button type="button" class="btn toggle-proj-tasks" data-pid="${p.id}" aria-expanded="true">งาน (${n}) — พับ/ขยาย</button>
        <div class="proj-tasks-body" data-pid="${p.id}">
          <div class="proj-task-scroll">${taskHtml || '<p class="muted" style="padding:4px 0">ยังไม่มีงาน — เพิ่มด้านล่าง</p>'}</div>
          ${renderAddTaskBlock(p.id)}
        </div>
      </div>
    </td></tr>`;
  }
  return mainRow + tasksBlock;
}

let _dashboardCache = null;

function todayYMDLocal() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}

function isoToLocalMidnight(iso) {
  if (!iso || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(String(iso).trim())) return null;
  const p = String(iso).trim().split('-');
  return new Date(parseInt(p[0],10), parseInt(p[1],10)-1, parseInt(p[2],10));
}

function isoInThisCalendarWeek(iso) {
  const dt = isoToLocalMidnight(iso);
  if (!dt || isNaN(dt.getTime())) return false;
  const now = new Date();
  const wd = (now.getDay() + 6) % 7;
  const mon = new Date(now.getFullYear(), now.getMonth(), now.getDate() - wd);
  mon.setHours(0,0,0,0);
  const sun = new Date(mon);
  sun.setDate(mon.getDate() + 6);
  sun.setHours(23,59,59,999);
  return dt >= mon && dt <= sun;
}

function taskDueTodayClient(t) {
  const y = todayYMDLocal();
  if ((t.due_date || '').trim() === y) return true;
  const h = (t.due_hint || '').toLowerCase();
  if (h.includes('today') || (t.due_hint || '').indexOf('วันนี้') >= 0) return true;
  return false;
}

function taskMatchesSummaryFilters(t) {
  const st = sel('sum-f-status').value;
  if (st && (t.status || '') !== st) return false;
  const pid = sel('sum-f-proj').value;
  if (pid && String(t.project_id) !== pid) return false;
  if (sel('sum-f-due-today').checked && !taskDueTodayClient(t)) return false;
  if (sel('sum-f-due-week').checked && !isoInThisCalendarWeek(t.due_date)) return false;
  return true;
}

function renderSummaryTaskRows() {
  const tb = sel('tb-sum-all-tasks');
  if (!tb || !_dashboardCache) return;
  const src = _dashboardCache.all_project_tasks || [];
  const rows = src.filter(taskMatchesSummaryFilters);
  tb.innerHTML = rows.map(t => {
    const det = [t.details,t.notes].filter(Boolean).join(' · ');
    return `<tr data-task-id="${t.id}" data-project-id="${t.project_id}">
      <td title="sort_order=${t.sort_order ?? 0}"><strong>${t.rank_in_project != null ? t.rank_in_project : '—'}</strong></td>
      <td>${esc(t.project_name||'')}</td>
      <td>${esc(t.title||'')}</td>
      <td>${esc(t.status||'')}</td>
      <td>${esc(t.due_hint||'')}</td>
      <td>${esc(t.due_date_display || t.due_date || '—')}</td>
      <td class="muted" style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${attr(det)}">${esc(det||'—')}</td>
      <td class="row-actions">
        <button type="button" class="btn sum-task-done" data-task-id="${t.id}">done</button>
        <button type="button" class="btn sum-task-edit" data-task-id="${t.id}" data-project-id="${t.project_id}">ไปแก้ไข</button>
        <button type="button" class="btn btn-danger sum-task-del" data-task-id="${t.id}">ลบ</button>
      </td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">ไม่มีรายการที่ตรงตัวกรอง</td></tr>';
}

function renderDashboard(d) {
  _dashboardCache = d;
  const s = d.summary || {};
  const gen = d.generated_at || '';
  const sg = sel('sum-generated');
  if (sg) {
    try { sg.textContent = gen ? ('อัปเดต Summary: ' + new Date(gen).toLocaleString('th-TH', { dateStyle: 'short', timeStyle: 'short' })) : ''; }
    catch (e) { sg.textContent = gen || ''; }
  }
  const cards = sel('sum-cards');
  if (cards) {
    const defs = [
      ['โครงการเปิด', s.active_projects ?? 0],
      ['Issues เปิด', s.open_issues ?? 0],
      ['งานในโครงการ', s.project_tasks ?? 0],
      ['Other tasks', s.other_tasks ?? 0],
      ['Due วันนี้', s.due_today ?? 0],
      ['ต้องจับตา', s.need_attention ?? 0],
    ];
    cards.innerHTML = defs.map(([lab, n]) => `<div class="sum-card"><b>${n}</b><span>${esc(lab)}</span></div>`).join('');
  }
  const tbp = sel('tb-sum-projects');
  if (tbp) {
    tbp.innerHTML = (d.projects||[]).map(p => {
      const implDue = [p.implementation_date_display||'', p.due_date_display||''].filter(Boolean).join(' · ') || '—';
      return `<tr>
        <td><strong>${esc(p.name)}</strong><div class="muted" style="font-size:0.72rem"><code>${esc(p.code||'')}</code></div></td>
        <td>${esc(p.status||'')}</td>
        <td>${p.percent_complete ?? 0}%</td>
        <td style="max-width:200px">${esc(p.next_step||'—')}</td>
        <td class="muted" style="font-size:0.78rem">${esc(implDue)}</td>
        <td>${p.open_task_count ?? 0}</td>
        <td>${p.issue_count ?? 0}</td>
        <td class="row-actions">
          <button type="button" class="btn sum-goto" data-pid="${p.id}" data-nav="row">ดู</button>
          <button type="button" class="btn sum-goto" data-pid="${p.id}" data-nav="tasks">งาน</button>
          <button type="button" class="btn sum-goto" data-pid="${p.id}" data-nav="details">รายละเอียด</button>
        </td></tr>`;
    }).join('') || '<tr><td colspan="8" class="muted">ไม่มีโครงการ</td></tr>';
  }
  const sp = sel('sum-f-proj');
  if (sp) {
    const cur = sp.value;
    sp.innerHTML = '<option value="">ทั้งหมด</option>' + (d.projects||[]).map(p =>
      `<option value="${p.id}">${esc(p.name)}</option>`).join('');
    if ([...sp.options].some(o => o.value === cur)) sp.value = cur;
  }
  renderSummaryTaskRows();
  const tbi = sel('tb-sum-issues');
  if (tbi) {
    tbi.innerHTML = (d.issues||[]).map(i => `<tr>
      <td>${esc(i.title||'')}</td>
      <td>${esc(i.system_name||'')}</td>
      <td>${esc((i.priority||'') + ' / ' + (i.severity||''))}</td>
      <td>${esc(i.status||'')}</td>
      <td>${esc(i.next_step||'—')}</td>
      <td>${esc(i.project_name||'—')}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">ไม่มี open issue</td></tr>';
  }
  const tbo = sel('tb-sum-other');
  if (tbo) {
    tbo.innerHTML = (d.other_tasks||[]).map(o => `<tr data-ot-id="${o.id}">
      <td>${esc(o.title||'')}</td>
      <td>${esc(o.status||'')}</td>
      <td>${esc(o.priority||'')}</td>
      <td>${esc(o.requester||'')}</td>
      <td>${esc(o.related_project_name||'—')}</td>
      <td>${esc(o.due_date_display||o.due_date||'—')}</td>
      <td class="row-actions">
        <button type="button" class="btn btn-danger sum-ot-del" data-id="${o.id}">ลบ</button>
      </td></tr>`).join('') || '<tr><td colspan="7" class="muted">ไม่มี other task</td></tr>';
  }
}

function bindSummarySectionOnce() {
  const sec = document.getElementById('sec-summary');
  if (!sec || sec.dataset.bound === '1') return;
  sec.dataset.bound = '1';
  ['sum-f-status','sum-f-proj'].forEach(id => { const el = sel(id); if (el) el.addEventListener('change', () => renderSummaryTaskRows()); });
  ['sum-f-due-today','sum-f-due-week'].forEach(id => { const el = sel(id); if (el) el.addEventListener('change', () => renderSummaryTaskRows()); });
  sec.addEventListener('click', async (e) => {
    const g = e.target.closest('.sum-goto');
    if (g) {
      const pid = parseInt(g.dataset.pid, 10);
      const mode = g.dataset.nav;
      if (mode === 'details') {
        extraProjectId = pid;
        openProjectExtra(pid);
        document.getElementById('project-extra')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        return;
      }
      const el = mode === 'tasks'
        ? document.getElementById('proj-tasks-anchor-' + pid)
        : document.getElementById('proj-row-' + pid);
      if (el) {
        if (mode === 'tasks') {
          const body = el.querySelector('.proj-tasks-body');
          if (body) body.style.display = 'block';
          const tb = el.querySelector('.toggle-proj-tasks');
          if (tb) tb.setAttribute('aria-expanded', 'true');
        }
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      } else {
        document.getElementById('sec-projects')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      return;
    }
    const done = e.target.closest('.sum-task-done');
    if (done) {
      try {
        await api('/admin/api/hospital-work/tasks/'+done.dataset.taskId, { method:'PUT', body: JSON.stringify({ status: 'done' }) });
        await loadAll();
      } catch (err) { alert(err.message); }
      return;
    }
    const ed = e.target.closest('.sum-task-edit');
    if (ed) {
      const pid = parseInt(ed.dataset.projectId, 10);
      const tid = parseInt(ed.dataset.taskId, 10);
      const anchor = document.getElementById('proj-tasks-anchor-' + pid);
      if (anchor) {
        const body = anchor.querySelector('.proj-tasks-body');
        if (body) body.style.display = 'block';
        anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
        setTimeout(() => {
          document.getElementById('task-card-' + tid)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }, 350);
      } else {
        document.getElementById('sec-projects')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      return;
    }
    const del = e.target.closest('.sum-task-del');
    if (del) {
      if (!confirm('ลบงานนี้ (soft delete)?')) return;
      try {
        await api('/admin/api/hospital-work/tasks/'+del.dataset.taskId, { method:'DELETE' });
        await loadAll();
      } catch (err) { alert(err.message); }
      return;
    }
    const otd = e.target.closest('.sum-ot-del');
    if (otd) {
      if (!confirm('ลบ Other task นี้?')) return;
      try {
        await api('/admin/api/hospital-work/other-tasks/'+otd.dataset.id, { method:'DELETE' });
        await loadAll();
      } catch (err) { alert(err.message); }
    }
  });
}

async function refreshSummaryOnly() {
  const d = await api('/admin/api/hospital-work/dashboard');
  renderDashboard(d);
  setSyncStatus(new Date());
}

function openProjectExtra(pid) {
  extraProjectId = pid;
  const p = _projectsCache.find(x => x.id === pid);
  if (!p) return;
  sel('project-extra').style.display = 'block';
  sel('pe-id').textContent = String(pid);
  sel('pe-impl').value = toDateInputValue(p.implementation_date);
  sel('pe-next').value = p.next_step || '';
  sel('pe-desc').value = p.description || '';
  sel('pe-vendor').value = p.vendor || '';
  sel('pe-owner').value = p.owner || '';
  sel('pe-sd').value = toDateInputValue(p.start_date);
  sel('pe-ed').value = toDateInputValue(p.end_date);
  sel('pe-dd').value = toDateInputValue(p.due_date);
  sel('pe-notes').value = p.notes || '';
  const pr = sel('pe-priority');
  pr.value = (p.priority && ['High','Medium','Low'].includes(p.priority)) ? p.priority : 'Medium';
  requestAnimationFrame(() => document.getElementById('project-extra')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
}

async function loadAll() {
  const [projects, issues, other, preview, dashboard] = await Promise.all([
    api(projectsWithTasksUrl()),
    api('/admin/api/hospital-work/issues'),
    api('/admin/api/hospital-work/other-tasks'),
    api('/admin/api/hospital-work/daily-report-preview'),
    api('/admin/api/hospital-work/dashboard'),
  ]);
  _projectsCache = projects;
  const tb = sel('tb-projects');
  tb.innerHTML = projects.map(renderProjectWithTasksRows).join('');

  fillProjectSelects(projects);
  renderDashboard(dashboard);
  bindSummarySectionOnce();

  sel('tb-issues').innerHTML = issues.map(i => `<tr data-id="${i.id}">
    <td>${esc(i.title)}</td>
    <td>${esc(i.severity)}</td>
    <td>${esc(i.project_name||'-')}</td>
    <td><button type="button" class="btn btn-danger del-issue" data-id="${i.id}">ลบ</button></td>
    </tr>`).join('');

  sel('tb-other').innerHTML = other.map(o => `<tr data-id="${o.id}">
    <td>${esc(o.title)}</td>
    <td>${esc(o.status)}</td>
    <td><button type="button" class="btn btn-danger del-other" data-id="${o.id}">ลบ</button></td>
    </tr>`).join('');

  const taRep = sel('report-preview');
  const fullReport = preview.report_text || '';
  taRep.value = fullReport;
  taRep.scrollTop = 0;
  const rdl = sel('report-date-label');
  const rga = sel('report-generated-at');
  if (rdl) rdl.textContent = preview.date_label ? ('รายงานวันที่: ' + preview.date_label) : '';
  if (rga) {
    if (preview.generated_at) {
      try {
        rga.textContent = 'สร้างเมื่อ: ' + new Date(preview.generated_at).toLocaleString('th-TH', { dateStyle: 'medium', timeStyle: 'short' });
      } catch (e) {
        rga.textContent = 'สร้างเมื่อ: ' + preview.generated_at;
      }
    } else rga.textContent = '';
  }
  const ar = preview.ai_review || {};
  const ev = ar.evidence_row_counts || {};
  sel('ai-review-box').innerHTML =
    '<p><strong>'+esc(ar.message_th||'')+'</strong></p>' +
    '<p class="muted" style="margin-top:8px">หลักฐาน (แถว is_active=1): '+
    'projects='+ (ev.hospital_projects||0) +', tasks='+ (ev.hospital_project_tasks||0) +
    ', issues='+ (ev.hospital_issues||0) +', other='+ (ev.hospital_other_tasks||0) +'</p>';

  document.querySelectorAll('.save-proj').forEach(b => b.addEventListener('click', async () => {
    const tr = b.closest('tr.proj-main');
    if (!tr) return;
    const st = tr.querySelector('.p-st').value;
    const cs = tr.querySelector('.p-cs').value;
    await api('/admin/api/hospital-work/projects/'+b.dataset.id, {method:'PUT', body: JSON.stringify({
      status: st, current_status: cs
    })});
    await loadAll();
  }));
  document.querySelectorAll('.extra-proj').forEach(b => b.addEventListener('click', () => {
    openProjectExtra(parseInt(b.dataset.id,10));
  }));
  document.querySelectorAll('.toggle-proj-tasks').forEach(btn => {
    btn.addEventListener('click', () => {
      const wrap = btn.parentElement;
      const body = wrap && wrap.querySelector('.proj-tasks-body');
      if (!body) return;
      const hidden = body.style.display === 'none';
      body.style.display = hidden ? 'block' : 'none';
      btn.setAttribute('aria-expanded', hidden ? 'true' : 'false');
    });
  });
  document.querySelectorAll('.save-inline-task').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.task-card-edit');
      if (!card) return;
      const tid = card.dataset.taskId;
      const body = {
        title: card.querySelector('.t-title').value,
        status: card.querySelector('.t-status').value,
        due_hint: card.querySelector('.t-due-hint').value,
        due_date: card.querySelector('.t-due-date').value,
        start_date: card.querySelector('.t-sd').value,
        end_date: card.querySelector('.t-ed').value,
        details: card.querySelector('.t-details').value,
        notes: card.querySelector('.t-notes').value,
        sort_order: (parseInt((card.querySelector('.t-sort') && card.querySelector('.t-sort').value) || '0', 10) || 0)
      };
      try {
        await api('/admin/api/hospital-work/tasks/'+tid, {method:'PUT', body: JSON.stringify(body)});
        await loadAll();
      } catch (e) { alert(e.message); }
    });
  });
  document.querySelectorAll('.del-inline-task').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm('ลบงานนี้?')) return;
      try {
        await api('/admin/api/hospital-work/tasks/'+btn.dataset.taskId, {method:'DELETE'});
        await loadAll();
      } catch (e) { alert(e.message); }
    });
  });
  document.querySelectorAll('.at-add').forEach(btn => {
    btn.addEventListener('click', async () => {
      const pid = parseInt(btn.dataset.pid, 10);
      const sec = btn.closest('.add-task-section');
      if (!sec) return;
      const errEl = sec.querySelector('.err-inline-task');
      if (errEl) errEl.textContent = '';
      try {
        const atSort = sec.querySelector('.at-sort');
        const soRaw = atSort && atSort.value !== '' ? parseInt(atSort.value, 10) : 0;
        await api('/admin/api/hospital-work/projects/'+pid+'/tasks', {method:'POST', body: JSON.stringify({
          title: sec.querySelector('.at-title').value,
          status: sec.querySelector('.at-status').value,
          due_hint: sec.querySelector('.at-due-hint').value,
          due_date: sec.querySelector('.at-due-date').value,
          details: sec.querySelector('.at-details').value,
          notes: sec.querySelector('.at-notes').value,
          start_date: sec.querySelector('.at-sd').value,
          end_date: sec.querySelector('.at-ed').value,
          sort_order: soRaw > 0 ? soRaw : 0
        })});
        await loadAll();
      } catch (e) { if (errEl) errEl.textContent = e.message; else alert(e.message); }
    });
  });
  document.querySelectorAll('.del-proj').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('ต้องการปิดโครงการนี้ไหม? โครงการจะไม่แสดงใน Daily Report แต่ยังสามารถกู้คืนได้')) return;
    await api('/admin/api/hospital-work/projects/'+b.dataset.id, {method:'DELETE'});
    if (extraProjectId === parseInt(b.dataset.id,10)) { extraProjectId=null; sel('project-extra').style.display='none'; }
    await loadAll();
  }));
  document.querySelectorAll('.restore-proj').forEach(b => b.addEventListener('click', async () => {
    try {
      await api('/admin/api/hospital-work/projects/'+b.dataset.id+'/restore', {method:'PUT', body:'{}'});
      await loadAll();
    } catch (e) { alert(e.message); }
  }));
  document.querySelectorAll('.del-issue').forEach(b => b.addEventListener('click', async () => {
    await api('/admin/api/hospital-work/issues/'+b.dataset.id, {method:'DELETE'});
    await loadAll();
  }));
  document.querySelectorAll('.del-other').forEach(b => b.addEventListener('click', async () => {
    await api('/admin/api/hospital-work/other-tasks/'+b.dataset.id, {method:'DELETE'});
    await loadAll();
  }));

  setSyncStatus(new Date());
  if (extraProjectId) {
    const still = _projectsCache.find(x => x.id === extraProjectId);
    if (still) openProjectExtra(extraProjectId);
    else { extraProjectId = null; sel('project-extra').style.display = 'none'; }
  }
}

sel('btn-save-project-extra').addEventListener('click', async () => {
  sel('err-pe').textContent='';
  if (!extraProjectId) return;
  try {
    await api('/admin/api/hospital-work/projects/'+extraProjectId, {method:'PUT', body: JSON.stringify({
      implementation_date: sel('pe-impl').value,
      next_step: sel('pe-next').value,
      description: sel('pe-desc').value,
      vendor: sel('pe-vendor').value,
      owner: sel('pe-owner').value,
      start_date: sel('pe-sd').value,
      end_date: sel('pe-ed').value,
      due_date: sel('pe-dd').value,
      notes: sel('pe-notes').value,
      priority: sel('pe-priority').value
    })});
    await loadAll();
  } catch(e) { sel('err-pe').textContent = e.message; }
});

sel('btn-refresh').addEventListener('click', () => { loadAll().catch(e => alert(e.message)); });
sel('btn-add-project').addEventListener('click', async () => {
  sel('err-projects').textContent='';
  try {
    await api('/admin/api/hospital-work/projects', {method:'POST', body: JSON.stringify({
      name: sel('np-name').value,
      code: sel('np-code').value || null,
      priority: sel('np-priority').value,
      implementation_date: sel('np-impl').value,
      next_step: sel('np-next').value,
      description: sel('np-desc').value
    })});
    sel('np-name').value=''; sel('np-code').value=''; sel('np-impl').value=''; sel('np-next').value=''; sel('np-desc').value='';
    await loadAll();
  } catch(e) { sel('err-projects').textContent = e.message; }
});
sel('btn-add-issue').addEventListener('click', async () => {
  sel('err-issues').textContent='';
  try {
    const pid = sel('ni-project').value;
    await api('/admin/api/hospital-work/issues', {method:'POST', body: JSON.stringify({
      title: sel('ni-title').value,
      severity: sel('ni-sev').value,
      priority: sel('ni-priority').value,
      system_name: sel('ni-sys').value,
      impact: sel('ni-impact').value,
      what_done: sel('ni-what').value,
      next_step: sel('ni-next').value,
      details: sel('ni-details').value,
      notes: sel('ni-notes').value,
      start_date: sel('ni-sd').value,
      end_date: sel('ni-ed').value,
      due_date: sel('ni-dd').value,
      project_id: pid ? parseInt(pid,10) : null
    })});
    sel('ni-title').value=''; sel('ni-details').value=''; sel('ni-notes').value='';
    sel('ni-next').value=''; sel('ni-sys').value=''; sel('ni-impact').value=''; sel('ni-what').value='';
    sel('ni-sd').value=''; sel('ni-ed').value=''; sel('ni-dd').value='';
    await loadAll();
  } catch(e) { sel('err-issues').textContent = e.message; }
});
sel('btn-add-other').addEventListener('click', async () => {
  sel('err-other').textContent='';
  try {
    const rel = sel('no-rel').value;
    await api('/admin/api/hospital-work/other-tasks', {method:'POST', body: JSON.stringify({
      title: sel('no-title').value,
      details: sel('no-details').value,
      priority: sel('no-priority').value,
      requester: sel('no-req').value,
      notes: sel('no-notes').value,
      start_date: sel('no-sd').value || '',
      end_date: sel('no-ed').value || '',
      due_date: sel('no-dd').value || '',
      related_project_id: rel ? parseInt(rel,10) : null
    })});
    sel('no-title').value=''; sel('no-details').value=''; sel('no-notes').value=''; sel('no-req').value='';
    sel('no-sd').value=''; sel('no-ed').value=''; sel('no-dd').value='';
    await loadAll();
  } catch(e) { sel('err-other').textContent = e.message; }
});
sel('btn-copy-report').addEventListener('click', async () => {
  const t = sel('report-preview');
  const full = t.value || '';
  try {
    await navigator.clipboard.writeText(full);
  } catch (e) {
    t.focus();
    t.select();
    t.setSelectionRange(0, full.length);
    document.execCommand('copy');
  }
});
sel('btn-report-scroll-top').addEventListener('click', () => {
  const t = sel('report-preview');
  t.scrollTop = 0;
  t.focus({ preventScroll: true });
});
const _cbArch = sel('cb-show-archived');
if (_cbArch) _cbArch.addEventListener('change', () => { loadAll().catch(e => alert(e.message)); });
const _btnSum = sel('btn-refresh-summary');
if (_btnSum) _btnSum.addEventListener('click', () => { refreshSummaryOnly().catch(e => alert(e.message)); });
loadAll().catch(e => alert(e.message));
</script>
</body>
</html>"""
