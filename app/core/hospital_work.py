"""Hospital Work Dashboard — Phase 1: DB access, daily report preview, CRUD helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any

import aiosqlite

from app.core.database import get_db

_TZ_BKK = timezone(timedelta(hours=7))


def _row(d: aiosqlite.Row | None) -> dict[str, Any] | None:
    return dict(d) if d else None


def _slug_code(raw: str) -> str:
    s = (raw or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:64] or "project"


async def list_projects(*, include_inactive: bool = False) -> list[dict[str, Any]]:
    async with get_db() as db:
        if include_inactive:
            cur = await db.execute(
                """
                SELECT * FROM hospital_projects
                ORDER BY sort_order ASC, id ASC
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
    return [_row(r) for r in rows if r]


async def get_project(project_id: int) -> dict[str, Any] | None:
    async with get_db() as db:
        cur = await db.execute(
            "SELECT * FROM hospital_projects WHERE id = ?",
            (project_id,),
        )
        return _row(await cur.fetchone())


async def create_project(
    name: str,
    code: str | None = None,
    status: str = "In Progress",
    percent_complete: int = 0,
    current_status: str = "",
    sort_order: int = 0,
) -> dict[str, Any]:
    nm = (name or "").strip()
    if not nm:
        raise ValueError("name required")
    cd = _slug_code(code or nm)
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_projects
            (name, code, status, percent_complete, current_status, sort_order)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (nm, cd, status or "In Progress", int(percent_complete or 0), current_status or "", int(sort_order or 0)),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        rid = int((await cur.fetchone())["id"])
    row = await get_project(rid)
    if not row:
        raise RuntimeError("insert failed")
    return row


async def update_project(project_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "name",
        "code",
        "status",
        "percent_complete",
        "current_status",
        "sort_order",
        "is_active",
    }
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        if k == "code" and v is not None:
            v = _slug_code(str(v))
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


async def list_tasks(project_id: int) -> list[dict[str, Any]]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT * FROM hospital_project_tasks
            WHERE project_id = ?
            ORDER BY sort_order ASC, id ASC
            """,
            (project_id,),
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_task(
    project_id: int,
    title: str,
    status: str = "open",
    due_hint: str = "",
    sort_order: int = 0,
) -> dict[str, Any]:
    t = (title or "").strip()
    if not t:
        raise ValueError("title required")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_project_tasks
            (project_id, title, status, due_hint, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, t, status or "open", due_hint or "", int(sort_order or 0)),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        tid = int((await cur.fetchone())["id"])
        cur2 = await db.execute("SELECT * FROM hospital_project_tasks WHERE id = ?", (tid,))
        return _row(await cur2.fetchone()) or {}


async def update_task(task_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"title", "status", "due_hint", "sort_order", "project_id"}
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in allowed:
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
        return _row(await cur.fetchone())


async def delete_task(task_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM hospital_project_tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cur.rowcount > 0


async def list_issues(project_id: int | None = None) -> list[dict[str, Any]]:
    async with get_db() as db:
        if project_id is None:
            cur = await db.execute(
                """
                SELECT i.*, p.name AS project_name, p.code AS project_code
                FROM hospital_issues i
                LEFT JOIN hospital_projects p ON p.id = i.project_id
                ORDER BY i.id DESC
                """
            )
        else:
            cur = await db.execute(
                """
                SELECT i.*, p.name AS project_name, p.code AS project_code
                FROM hospital_issues i
                LEFT JOIN hospital_projects p ON p.id = i.project_id
                WHERE i.project_id = ?
                ORDER BY i.id DESC
                """,
                (project_id,),
            )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_issue(
    title: str,
    project_id: int | None = None,
    severity: str = "medium",
    status: str = "open",
    details: str = "",
) -> dict[str, Any]:
    t = (title or "").strip()
    if not t:
        raise ValueError("title required")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_issues
            (project_id, title, severity, status, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, t, severity or "medium", status or "open", details or ""),
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
    allowed = {"title", "project_id", "severity", "status", "details"}
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
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
        cur = await db.execute("DELETE FROM hospital_issues WHERE id = ?", (issue_id,))
        await db.commit()
        return cur.rowcount > 0


async def list_other_tasks() -> list[dict[str, Any]]:
    async with get_db() as db:
        cur = await db.execute(
            """
            SELECT * FROM hospital_other_tasks
            ORDER BY sort_order ASC, id ASC
            """
        )
        rows = await cur.fetchall()
    return [_row(r) for r in rows if r]


async def create_other_task(
    title: str,
    status: str = "open",
    notes: str = "",
    sort_order: int = 0,
) -> dict[str, Any]:
    t = (title or "").strip()
    if not t:
        raise ValueError("title required")
    async with get_db() as db:
        await db.execute(
            """
            INSERT INTO hospital_other_tasks
            (title, status, notes, sort_order)
            VALUES (?, ?, ?, ?)
            """,
            (t, status or "open", notes or "", int(sort_order or 0)),
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid() AS id")
        oid = int((await cur.fetchone())["id"])
        cur2 = await db.execute("SELECT * FROM hospital_other_tasks WHERE id = ?", (oid,))
        return _row(await cur2.fetchone()) or {}


async def update_other_task(ot_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"title", "status", "notes", "sort_order"}
    sets: list[str] = []
    vals: list[Any] = []
    for k, v in (fields or {}).items():
        if k not in allowed:
            continue
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        async with get_db() as db:
            cur = await db.execute("SELECT * FROM hospital_other_tasks WHERE id = ?", (ot_id,))
            return _row(await cur.fetchone())
    sets.append("updated_at = datetime('now')")
    vals.append(ot_id)
    async with get_db() as db:
        await db.execute(
            f"UPDATE hospital_other_tasks SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM hospital_other_tasks WHERE id = ?", (ot_id,))
        return _row(await cur.fetchone())


async def delete_other_task(ot_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM hospital_other_tasks WHERE id = ?", (ot_id,))
        await db.commit()
        return cur.rowcount > 0


async def _count_table(db: aiosqlite.Connection, table: str) -> int:
    cur = await db.execute(f"SELECT COUNT(*) AS c FROM {table}")
    r = await cur.fetchone()
    return int(r["c"]) if r else 0


async def build_daily_report_preview() -> dict[str, Any]:
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
        projects_raw = [dict(r) for r in await proj_cur.fetchall()]

        issues_cur = await db.execute(
            """
            SELECT i.*, p.name AS project_name
            FROM hospital_issues i
            LEFT JOIN hospital_projects p ON p.id = i.project_id
            ORDER BY
                CASE i.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                i.id DESC
            """
        )
        issues_raw = [dict(r) for r in await issues_cur.fetchall()]

        ot_cur = await db.execute(
            "SELECT * FROM hospital_other_tasks ORDER BY sort_order ASC, id ASC"
        )
        other_raw = [dict(r) for r in await ot_cur.fetchall()]

        evidence = {
            "hospital_projects": await _count_table(db, "hospital_projects"),
            "hospital_project_tasks": await _count_table(db, "hospital_project_tasks"),
            "hospital_issues": await _count_table(db, "hospital_issues"),
            "hospital_other_tasks": await _count_table(db, "hospital_other_tasks"),
        }

    projects_out: list[dict[str, Any]] = []
    for p in projects_raw:
        pid = int(p["id"])
        tasks = await list_tasks(pid)
        tasks = await list_tasks(pid)
        projects_out.append(
            {
                "id": pid,
                "name": p["name"],
                "code": p["code"],
                "status": p["status"],
                "percent_complete": p["percent_complete"],
                "current_status": p["current_status"] or "",
                "tasks": [
                    {
                        "id": t["id"],
                        "title": t["title"],
                        "status": t["status"],
                        "due_hint": t.get("due_hint") or "",
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
            "project_id": r.get("project_id"),
            "project_name": r.get("project_name"),
        }
        for r in issues_raw
    ]

    other_out = list(other_raw)

    lines: list[str] = [
        f"รายงานประจำวัน (preview จาก DB) — {now.strftime('%Y-%m-%d %H:%M')} ICT+7",
        "",
        "== โครงการ ==",
    ]
    for po in projects_out:
        lines.append(
            f"- [{po['code']}] {po['name']} — {po['status']} ({po['percent_complete']}%)"
        )
        if po["current_status"]:
            lines.append(f"    สถานะ: {po['current_status']}")
        for tk in po["tasks"]:
            lines.append(f"    • [{tk['status']}] {tk['title']}" + (f" (due: {tk['due_hint']})" if tk["due_hint"] else ""))
        lines.append("")
    lines.append("== Issues / Risks ==")
    if not issues_out:
        lines.append("(ไม่มี)")
    else:
        for iss in issues_out:
            pname = iss.get("project_name") or "-"
            lines.append(
                f"- [{iss['severity']}/{iss['status']}] {iss['title']} (โครงการ: {pname})"
            )
    lines.append("")
    lines.append("== งานอื่น ๆ (นอกโครงการ) ==")
    if not other_out:
        lines.append("(ไม่มี)")
    else:
        for ot in other_out:
            lines.append(f"- [{ot['status']}] {ot['title']}")

    return {
        "generated_at": generated_at,
        "date_label": now.strftime("%Y-%m-%d"),
        "projects": projects_out,
        "issues": issues_out,
        "other_tasks": other_out,
        "report_text": "\n".join(lines),
        "ai_review": {
            "mode": "placeholder",
            "message_th": "Phase 1: ไม่มี LLM parse / auto review — แสดงเฉพาะหลักฐานจากจำนวนแถวในฐานข้อมูล",
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
  .container{max-width:1200px;margin:24px auto;padding:0 24px 48px}
  section{background:#111;border:1px solid #222;border-radius:10px;padding:16px 18px;margin-bottom:20px}
  section h2{font-size:1rem;color:#93c5fd;margin-bottom:12px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:900px){.grid2{grid-template-columns:1fr}}
  label{display:block;font-size:0.75rem;color:#9ca3af;margin-bottom:4px}
  input,select,textarea{width:100%;background:#0a0a0a;border:1px solid #333;border-radius:6px;padding:8px 10px;color:#e5e7eb;font-size:0.9rem}
  textarea{min-height:72px;font-family:ui-monospace,monospace;font-size:0.8rem}
  table{width:100%;border-collapse:collapse;font-size:0.85rem}
  th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #222}
  th{color:#9ca3af;font-weight:600}
  tr:hover td{background:#151515}
  .muted{color:#6b7280;font-size:0.8rem}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:0.72rem;background:#1e293b;color:#cbd5e1}
  .ai-box{border:1px dashed #444;background:#0d0d0d;padding:12px;border-radius:8px;color:#a3a3a3;font-size:0.85rem}
  .row-actions{display:flex;gap:6px;flex-wrap:wrap}
  .err{color:#f87171;font-size:0.85rem;margin-top:8px}
</style>
</head>
<body>
<header class="header">
  <a class="back-btn" href="/admin">← Admin</a>
  <h1>Hospital Work Dashboard <span class="muted">(Phase 1)</span></h1>
  <button type="button" class="btn btn-primary" id="btn-refresh">รีเฟรช</button>
</header>
<div class="container">
  <p class="muted" style="margin-bottom:16px">CRUD จาก DB เท่านั้น — ยังไม่มี Telegram / LLM parse / approve flow</p>

  <div class="grid2">
    <section>
      <h2>โครงการ</h2>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div><label>ชื่อ</label><input id="np-name" placeholder="ชื่อโครงการ"></div>
        <div><label>รหัส (slug)</label><input id="np-code" placeholder="เว้นว่างให้ auto"></div>
      </div>
      <button type="button" class="btn btn-primary" id="btn-add-project">เพิ่มโครงการ</button>
      <div class="err" id="err-projects"></div>
      <div style="overflow-x:auto;margin-top:12px">
        <table><thead><tr><th>รหัส</th><th>ชื่อ</th><th>%</th><th>สถานะโครงการ</th><th>สถานะปัจจุบัน</th><th></th></tr></thead>
        <tbody id="tb-projects"></tbody></table>
      </div>
    </section>

    <section>
      <h2>งานในโครงการ <span class="muted" id="task-project-label"></span></h2>
      <p class="muted" id="task-hint">เลือกโครงการจากตารางซ้าย</p>
      <div id="task-editor" style="display:none">
        <div style="margin-bottom:8px"><label>หัวข้อ</label><input id="nt-title"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
          <div><label>สถานะ</label>
            <select id="nt-status"><option>open</option><option>in_progress</option><option>done</option></select></div>
          <div><label>Due hint</label><input id="nt-due" placeholder="สัปดาห์นี้"></div>
        </div>
        <button type="button" class="btn btn-primary" id="btn-add-task">เพิ่มงาน</button>
        <div class="err" id="err-tasks"></div>
        <div style="overflow-x:auto;margin-top:12px">
          <table><thead><tr><th>งาน</th><th>สถานะ</th><th></th></tr></thead>
          <tbody id="tb-tasks"></tbody></table>
        </div>
      </div>
    </section>
  </div>

  <div class="grid2">
    <section>
      <h2>Issues</h2>
      <div style="display:grid;grid-template-columns:1fr 120px;gap:8px;margin-bottom:8px">
        <div><label>หัวข้อ</label><input id="ni-title"></div>
        <div><label>Severity</label>
          <select id="ni-sev"><option>low</option><option>medium</option><option>high</option></select>
        </div>
      </div>
      <div style="margin-bottom:8px"><label>รายละเอียด</label><textarea id="ni-details"></textarea></div>
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

    <section>
      <h2>งานอื่น (นอกโครงการ)</h2>
      <div style="margin-bottom:8px"><label>หัวข้อ</label><input id="no-title"></div>
      <div style="margin-bottom:8px"><label>หมายเหตุ</label><input id="no-notes"></div>
      <button type="button" class="btn btn-primary" id="btn-add-other">เพิ่ม</button>
      <div class="err" id="err-other"></div>
      <div style="overflow-x:auto;margin-top:12px">
        <table><thead><tr><th>งาน</th><th>สถานะ</th><th></th></tr></thead>
        <tbody id="tb-other"></tbody></table>
      </div>
    </section>
  </div>

  <section>
    <h2>Daily Report Preview <span class="muted">(อ่านจาก DB)</span></h2>
    <button type="button" class="btn" id="btn-copy-report" style="margin-bottom:10px">คัดลอกข้อความ</button>
    <textarea id="report-preview" readonly style="min-height:220px"></textarea>
  </section>

  <section>
    <h2>AI Review <span class="pill">Phase 1 placeholder</span></h2>
    <div class="ai-box" id="ai-review-box">กำลังโหลด…</div>
  </section>
</div>
<script>
const sel = (id) => document.getElementById(id);
let selectedProjectId = null;

async function api(path, opt) {
  const r = await fetch(path, Object.assign({credentials:'same-origin',headers:{'Content-Type':'application/json'}}, opt||{}));
  const j = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(j.detail || j.error || r.statusText);
  return j;
}

function esc(s){ const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; }
function attr(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;'); }

async function loadAll() {
  const [projects, issues, other, preview] = await Promise.all([
    api('/admin/api/hospital-work/projects'),
    api('/admin/api/hospital-work/issues'),
    api('/admin/api/hospital-work/other-tasks'),
    api('/admin/api/hospital-work/daily-report-preview'),
  ]);
  const tb = sel('tb-projects');
  tb.innerHTML = projects.map(p => `<tr data-id="${p.id}">
    <td><code>${esc(p.code)}</code></td>
    <td>${esc(p.name)}</td>
    <td><input type="number" class="p-pct" min="0" max="100" style="width:56px" value="${p.percent_complete}"></td>
    <td><input type="text" class="p-st" style="width:110px" value="${attr(p.status)}"></td>
    <td><input type="text" class="p-cs" style="width:160px" placeholder="รายละเอียด" value="${attr(p.current_status||'')}"></td>
    <td class="row-actions">
      <button type="button" class="btn btn-primary save-proj" data-id="${p.id}">บันทึก</button>
      <button type="button" class="btn select-proj" data-id="${p.id}">งาน</button>
      <button type="button" class="btn btn-danger del-proj" data-id="${p.id}">ปิด</button>
    </td></tr>`).join('');

  const niProj = sel('ni-project');
  niProj.innerHTML = '<option value="">—</option>' + projects.map(p =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join('');

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

  sel('report-preview').value = preview.report_text || '';
  const ar = preview.ai_review || {};
  const ev = ar.evidence_row_counts || {};
  sel('ai-review-box').innerHTML =
    '<p><strong>'+esc(ar.message_th||'')+'</strong></p>' +
    '<p class="muted" style="margin-top:8px">หลักฐาน (จำนวนแถว): '+
    'projects='+ (ev.hospital_projects||0) +', tasks='+ (ev.hospital_project_tasks||0) +
    ', issues='+ (ev.hospital_issues||0) +', other='+ (ev.hospital_other_tasks||0) +'</p>';

  document.querySelectorAll('.save-proj').forEach(b => b.addEventListener('click', async () => {
    const tr = b.closest('tr');
    const pct = parseInt(tr.querySelector('.p-pct').value,10)||0;
    const st = tr.querySelector('.p-st').value;
    const cs = tr.querySelector('.p-cs').value;
    await api('/admin/api/hospital-work/projects/'+b.dataset.id, {method:'PUT', body: JSON.stringify({
      percent_complete: pct, status: st, current_status: cs
    })});
    loadAll();
  }));
  document.querySelectorAll('.select-proj').forEach(b => b.addEventListener('click', () => {
    selectedProjectId = parseInt(b.dataset.id,10);
    sel('task-project-label').textContent = '(#'+selectedProjectId+')';
    sel('task-hint').style.display='none';
    sel('task-editor').style.display='block';
    loadTasks();
  }));

  document.querySelectorAll('.del-proj').forEach(b => b.addEventListener('click', async () => {
    if (!confirm('ปิดโครงการนี้ (soft delete)?')) return;
    await api('/admin/api/hospital-work/projects/'+b.dataset.id, {method:'DELETE'});
    if (selectedProjectId === parseInt(b.dataset.id,10)) { selectedProjectId=null; sel('task-editor').style.display='none'; sel('task-hint').style.display='block'; }
    loadAll();
  }));
  document.querySelectorAll('.del-issue').forEach(b => b.addEventListener('click', async () => {
    await api('/admin/api/hospital-work/issues/'+b.dataset.id, {method:'DELETE'});
    loadAll();
  }));
  document.querySelectorAll('.del-other').forEach(b => b.addEventListener('click', async () => {
    await api('/admin/api/hospital-work/other-tasks/'+b.dataset.id, {method:'DELETE'});
    loadAll();
  }));
}

async function loadTasks() {
  if (!selectedProjectId) return;
  const tasks = await api('/admin/api/hospital-work/projects/'+selectedProjectId+'/tasks');
  sel('tb-tasks').innerHTML = tasks.map(t => `<tr data-id="${t.id}">
    <td>${esc(t.title)}</td>
    <td><select class="task-st" data-id="${t.id}">${['open','in_progress','done'].map(s =>
      `<option value="${s}" ${t.status===s?'selected':''}>${s}</option>`).join('')}</select></td>
    <td><button type="button" class="btn btn-danger del-task" data-id="${t.id}">ลบ</button></td>
    </tr>`).join('');
  document.querySelectorAll('.task-st').forEach(el => el.addEventListener('change', async () => {
    await api('/admin/api/hospital-work/tasks/'+el.dataset.id, {method:'PUT', body: JSON.stringify({status: el.value})});
  }));
  document.querySelectorAll('.del-task').forEach(b => b.addEventListener('click', async () => {
    await api('/admin/api/hospital-work/tasks/'+b.dataset.id, {method:'DELETE'});
    loadTasks();
  }));
}

sel('btn-refresh').addEventListener('click', () => { loadAll().catch(e => alert(e.message)); });
sel('btn-add-project').addEventListener('click', async () => {
  sel('err-projects').textContent='';
  try {
    await api('/admin/api/hospital-work/projects', {method:'POST', body: JSON.stringify({
      name: sel('np-name').value,
      code: sel('np-code').value || null
    })});
    sel('np-name').value=''; sel('np-code').value='';
    loadAll();
  } catch(e) { sel('err-projects').textContent = e.message; }
});
sel('btn-add-task').addEventListener('click', async () => {
  sel('err-tasks').textContent='';
  if (!selectedProjectId) return;
  try {
    await api('/admin/api/hospital-work/projects/'+selectedProjectId+'/tasks', {method:'POST', body: JSON.stringify({
      title: sel('nt-title').value,
      status: sel('nt-status').value,
      due_hint: sel('nt-due').value
    })});
    sel('nt-title').value='';
    loadTasks();
  } catch(e) { sel('err-tasks').textContent = e.message; }
});
sel('btn-add-issue').addEventListener('click', async () => {
  sel('err-issues').textContent='';
  try {
    const pid = sel('ni-project').value;
    await api('/admin/api/hospital-work/issues', {method:'POST', body: JSON.stringify({
      title: sel('ni-title').value,
      severity: sel('ni-sev').value,
      details: sel('ni-details').value,
      project_id: pid ? parseInt(pid,10) : null
    })});
    sel('ni-title').value=''; sel('ni-details').value='';
    loadAll();
  } catch(e) { sel('err-issues').textContent = e.message; }
});
sel('btn-add-other').addEventListener('click', async () => {
  sel('err-other').textContent='';
  try {
    await api('/admin/api/hospital-work/other-tasks', {method:'POST', body: JSON.stringify({
      title: sel('no-title').value,
      notes: sel('no-notes').value
    })});
    sel('no-title').value=''; sel('no-notes').value='';
    loadAll();
  } catch(e) { sel('err-other').textContent = e.message; }
});
sel('btn-copy-report').addEventListener('click', () => {
  const t = sel('report-preview');
  t.select();
  document.execCommand('copy');
});
loadAll().catch(e => alert(e.message));
</script>
</body>
</html>"""

