"""Maintenance (CMMS) module — in-house replacement for Opmaint, inside the QMS app.

Covers: an asset register, per-asset checklists (each checkpoint tagged with a
category — Hydraulic / Mechanical / Electrical), preventive-maintenance schedules
triggered by date interval AND/OR running-hours, category→staff assignment
(reusing the employees login, with Electrical varying by location), completion
logging (machine history), and WhatsApp reminders (due-today to the assignee,
overdue escalation to a supervisor) via WATI.

All tables are Neon and self-create on first use.
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse

from .. import db, wati
from ..auth import require_employee
from ..config import (WATI_MAINT_DUE_TEMPLATE, WATI_MAINT_OVERDUE_TEMPLATE,
                      MAINT_SUPERVISOR_MOBILE, AGENT_TOKEN)

router = APIRouter(prefix="/maint", tags=["maintenance"])
IST = ZoneInfo("Asia/Kolkata")
CATEGORIES = ["Hydraulic", "Mechanical", "Electrical"]
_ensured = False


async def ensure_tables():
    global _ensured
    if _ensured:
        return
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_assets (
        id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, atype TEXT, location TEXT,
        make TEXT, model TEXT, serial_no TEXT, running_hours NUMERIC DEFAULT 0,
        hours_updated_at TIMESTAMPTZ, active BOOLEAN DEFAULT true, notes TEXT,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_checklists (
        id SERIAL PRIMARY KEY, asset_id INT, name TEXT, active BOOLEAN DEFAULT true,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_checkpoints (
        id SERIAL PRIMARY KEY, checklist_id INT, seq INT, text TEXT, category TEXT,
        input_type TEXT DEFAULT 'check', unit TEXT, spec TEXT)""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_schedules (
        id SERIAL PRIMARY KEY, asset_id INT, title TEXT, checklist_id INT,
        freq_days INT, hours_interval NUMERIC, category TEXT, location TEXT,
        assignee_emp_id INT, start_date DATE, last_done DATE, last_done_hours NUMERIC,
        active BOOLEAN DEFAULT true, created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_assignments (
        id SERIAL PRIMARY KEY, category TEXT, location TEXT DEFAULT '', emp_id INT,
        created_at TIMESTAMPTZ DEFAULT now())""")
    await db.execute("""CREATE TABLE IF NOT EXISTS maint_logs (
        id SERIAL PRIMARY KEY, asset_id INT, schedule_id INT, checklist_id INT,
        done_by TEXT, done_by_emp_id INT, done_at TIMESTAMPTZ DEFAULT now(),
        running_hours NUMERIC, status TEXT, results JSONB, remarks TEXT, next_due DATE)""")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_cp ON maint_checkpoints (checklist_id, seq)")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_m_log ON maint_logs (asset_id, done_at DESC)")
    _ensured = True


def _today() -> date:
    return datetime.now(IST).date()


def _num(v, default=None):
    try:
        if v is None or v == "":
            return default
        f = float(v)
        return int(f) if f == int(f) else f
    except Exception:
        return default


def _d(v):
    """Parse a YYYY-MM-DD string to date, else None."""
    if not v:
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ---------- assignee resolution ----------
async def resolve_assignee(category: str, location: str) -> Optional[int]:
    cat = (category or "").strip()
    loc = (location or "").strip()
    if not cat:
        return None
    if loc:
        row = await db.fetchrow(
            "SELECT emp_id FROM maint_assignments WHERE lower(category)=lower($1) "
            "AND lower(COALESCE(location,''))=lower($2) LIMIT 1", cat, loc)
        if row:
            return row["emp_id"]
    row = await db.fetchrow(
        "SELECT emp_id FROM maint_assignments WHERE lower(category)=lower($1) "
        "AND COALESCE(location,'')='' LIMIT 1", cat)
    return row["emp_id"] if row else None


# ---------- due computation ----------
def compute_due(sch: dict, asset_hours, today: date):
    base = sch.get("last_done") or sch.get("start_date")
    freq = sch.get("freq_days")
    next_due = None
    if freq:
        next_due = (base + timedelta(days=int(freq))) if base else today
    date_due = bool(next_due and next_due <= today)
    hours_due = False
    hrs_rem = None
    if sch.get("hours_interval"):
        last_h = float(sch.get("last_done_hours") or 0)
        target = last_h + float(sch["hours_interval"])
        cur = float(asset_hours or 0)
        hours_due = cur >= target
        hrs_rem = round(target - cur, 1)
    due = bool(date_due or hours_due)
    overdue = bool(next_due and next_due < today)
    return {"next_due": next_due.isoformat() if next_due else None,
            "due": due, "overdue": overdue, "hours_due": hours_due, "hours_remaining": hrs_rem}


# =======================================================================
#  ASSETS
# =======================================================================
@router.get("/meta")
async def meta(emp: dict = Depends(require_employee)):
    await ensure_tables()
    return JSONResponse({"categories": CATEGORIES}, headers={"Cache-Control": "no-store"})


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)), headers={"Cache-Control": "no-store"})


@router.get("/assets/list")
async def assets_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    rows = await db.fetch("SELECT * FROM maint_assets ORDER BY active DESC, lower(name) NULLS LAST, code")
    return _json([dict(r) for r in rows])


@router.post("/assets/save")
async def assets_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    if not code:
        raise HTTPException(400, "Asset code is required")
    if not name:
        raise HTTPException(400, "Asset name is required")
    vals = (code, name, (body.get("atype") or "").strip(), (body.get("location") or "").strip(),
            (body.get("make") or "").strip(), (body.get("model") or "").strip(),
            (body.get("serial_no") or "").strip(), _num(body.get("running_hours"), 0) or 0,
            (body.get("notes") or "").strip())
    aid = body.get("id")
    try:
        if aid:
            await db.execute(
                "UPDATE maint_assets SET code=$1,name=$2,atype=$3,location=$4,make=$5,model=$6,"
                "serial_no=$7,running_hours=$8,notes=$9 WHERE id=$10", *vals, aid)
            return {"ok": True, "id": aid}
        dup = await db.fetchval("SELECT id FROM maint_assets WHERE lower(code)=lower($1)", code)
        if dup:
            raise HTTPException(400, "An asset with that code already exists")
        new_id = await db.fetchval(
            "INSERT INTO maint_assets (code,name,atype,location,make,model,serial_no,running_hours,notes,"
            "hours_updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now()) RETURNING id", *vals)
        return {"ok": True, "id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


@router.post("/assets/hours")
async def assets_hours(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    aid = body.get("id")
    hrs = _num(body.get("running_hours"))
    if not aid or hrs is None:
        raise HTTPException(400, "Asset and running hours required")
    await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2", hrs, aid)
    return {"ok": True}


@router.post("/assets/toggle")
async def assets_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("UPDATE maint_assets SET active=$1 WHERE id=$2", bool(body.get("active")), body.get("id"))
    return {"ok": True}


# =======================================================================
#  CHECKLISTS + CHECKPOINTS
# =======================================================================
@router.get("/checklists/list")
async def checklists_list(asset: int = Query(default=0), emp: dict = Depends(require_employee)):
    await ensure_tables()
    if asset:
        rows = await db.fetch(
            "SELECT c.*, (SELECT COUNT(*) FROM maint_checkpoints p WHERE p.checklist_id=c.id) AS points "
            "FROM maint_checklists c WHERE asset_id=$1 ORDER BY lower(name)", asset)
    else:
        rows = await db.fetch(
            "SELECT c.*, a.name AS asset_name, a.code AS asset_code, "
            "(SELECT COUNT(*) FROM maint_checkpoints p WHERE p.checklist_id=c.id) AS points "
            "FROM maint_checklists c LEFT JOIN maint_assets a ON a.id=c.asset_id "
            "ORDER BY a.name NULLS LAST, lower(c.name)")
    return _json([dict(r) for r in rows])


@router.get("/checklist/get")
async def checklist_get(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_tables()
    c = await db.fetchrow("SELECT * FROM maint_checklists WHERE id=$1", id)
    if not c:
        raise HTTPException(404, "Checklist not found")
    pts = await db.fetch("SELECT * FROM maint_checkpoints WHERE checklist_id=$1 ORDER BY seq, id", id)
    return _json({"checklist": dict(c), "checkpoints": [dict(p) for p in pts]})


@router.post("/checklist/save")
async def checklist_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    asset_id = body.get("asset_id")
    name = (body.get("name") or "").strip()
    if not asset_id:
        raise HTTPException(400, "Choose an asset")
    if not name:
        raise HTTPException(400, "Checklist name is required")
    cid = body.get("id")
    if cid:
        await db.execute("UPDATE maint_checklists SET name=$1, asset_id=$2 WHERE id=$3", name, asset_id, cid)
    else:
        cid = await db.fetchval(
            "INSERT INTO maint_checklists (asset_id,name) VALUES ($1,$2) RETURNING id", asset_id, name)
    # replace checkpoints
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    for i, p in enumerate(body.get("checkpoints") or []):
        txt = (p.get("text") or "").strip()
        if not txt:
            continue
        await db.execute(
            "INSERT INTO maint_checkpoints (checklist_id,seq,text,category,input_type,unit,spec) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7)",
            cid, i + 1, txt, (p.get("category") or "").strip(),
            (p.get("input_type") or "check").strip(), (p.get("unit") or "").strip(),
            (p.get("spec") or "").strip())
    return {"ok": True, "id": cid}


@router.post("/checklist/delete")
async def checklist_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    cid = body.get("id")
    await db.execute("DELETE FROM maint_checkpoints WHERE checklist_id=$1", cid)
    await db.execute("DELETE FROM maint_checklists WHERE id=$1", cid)
    return {"ok": True}


# =======================================================================
#  SCHEDULES
# =======================================================================
async def _schedule_rows():
    rows = await db.fetch("""
        SELECT s.*, a.name AS asset_name, a.code AS asset_code, a.location AS asset_location,
               a.running_hours AS asset_hours, c.name AS checklist_name,
               e.name AS assignee_name, e.emp_no AS assignee_code, COALESCE(e.mobile,'') AS assignee_mobile
        FROM maint_schedules s
        LEFT JOIN maint_assets a ON a.id=s.asset_id
        LEFT JOIN maint_checklists c ON c.id=s.checklist_id
        LEFT JOIN employees e ON e.id=s.assignee_emp_id
        ORDER BY s.active DESC, a.name NULLS LAST, s.title""")
    return [dict(r) for r in rows]


@router.get("/schedules/list")
async def schedules_list(emp: dict = Depends(require_employee)):
    await ensure_tables()
    today = _today()
    out = []
    for s in await _schedule_rows():
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        out.append(s)
    return _json(out)


@router.post("/schedule/save")
async def schedule_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    asset_id = body.get("asset_id")
    title = (body.get("title") or "").strip()
    if not asset_id:
        raise HTTPException(400, "Choose an asset")
    if not title:
        raise HTTPException(400, "Schedule title is required")
    category = (body.get("category") or "").strip()
    location = (body.get("location") or "").strip()
    assignee = body.get("assignee_emp_id") or None
    if not assignee and category:
        assignee = await resolve_assignee(category, location)
    vals = (asset_id, title, body.get("checklist_id") or None,
            _num(body.get("freq_days")), _num(body.get("hours_interval")),
            category, location, assignee, _d(body.get("start_date")),
            _d(body.get("last_done")), _num(body.get("last_done_hours")))
    sid = body.get("id")
    if sid:
        await db.execute(
            "UPDATE maint_schedules SET asset_id=$1,title=$2,checklist_id=$3,freq_days=$4,"
            "hours_interval=$5,category=$6,location=$7,assignee_emp_id=$8,start_date=$9,"
            "last_done=$10,last_done_hours=$11 WHERE id=$12", *vals, sid)
        return {"ok": True, "id": sid}
    sid = await db.fetchval(
        "INSERT INTO maint_schedules (asset_id,title,checklist_id,freq_days,hours_interval,"
        "category,location,assignee_emp_id,start_date,last_done,last_done_hours) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id", *vals)
    return {"ok": True, "id": sid}


@router.post("/schedule/toggle")
async def schedule_toggle(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("UPDATE maint_schedules SET active=$1 WHERE id=$2", bool(body.get("active")), body.get("id"))
    return {"ok": True}


@router.post("/schedule/delete")
async def schedule_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("DELETE FROM maint_schedules WHERE id=$1", body.get("id"))
    return {"ok": True}


# =======================================================================
#  TEAM / ASSIGNMENTS (reuse employees as technicians)
# =======================================================================
@router.get("/team/data")
async def team(emp: dict = Depends(require_employee)):
    await ensure_tables()
    staff = await db.fetch(
        "SELECT id, emp_no, name, COALESCE(mobile,'') AS mobile, COALESCE(active,true) AS active "
        "FROM employees WHERE COALESCE(active,true)=true ORDER BY lower(name) NULLS LAST, emp_no")
    rows = await db.fetch(
        "SELECT m.*, e.name AS emp_name, e.emp_no AS emp_code, COALESCE(e.mobile,'') AS emp_mobile "
        "FROM maint_assignments m LEFT JOIN employees e ON e.id=m.emp_id "
        "ORDER BY m.category, m.location")
    return _json({"staff": [dict(r) for r in staff], "assignments": [dict(r) for r in rows],
                  "categories": CATEGORIES})


@router.post("/assignment/save")
async def assignment_save(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    cat = (body.get("category") or "").strip()
    loc = (body.get("location") or "").strip()
    eid = body.get("emp_id")
    if not cat or not eid:
        raise HTTPException(400, "Category and staff member are required")
    existing = await db.fetchval(
        "SELECT id FROM maint_assignments WHERE lower(category)=lower($1) AND lower(COALESCE(location,''))=lower($2)",
        cat, loc)
    if existing:
        await db.execute("UPDATE maint_assignments SET emp_id=$1 WHERE id=$2", eid, existing)
        return {"ok": True, "id": existing}
    nid = await db.fetchval(
        "INSERT INTO maint_assignments (category,location,emp_id) VALUES ($1,$2,$3) RETURNING id", cat, loc, eid)
    return {"ok": True, "id": nid}


@router.post("/assignment/delete")
async def assignment_delete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    await db.execute("DELETE FROM maint_assignments WHERE id=$1", body.get("id"))
    return {"ok": True}


# =======================================================================
#  DO / COMPLETE + HISTORY
# =======================================================================
@router.get("/do/get")
async def do_get(schedule: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_tables()
    s = await db.fetchrow("""
        SELECT s.*, a.name AS asset_name, a.code AS asset_code, a.running_hours AS asset_hours,
               c.name AS checklist_name
        FROM maint_schedules s LEFT JOIN maint_assets a ON a.id=s.asset_id
        LEFT JOIN maint_checklists c ON c.id=s.checklist_id WHERE s.id=$1""", schedule)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s = dict(s)
    pts = []
    if s.get("checklist_id"):
        rows = await db.fetch("SELECT * FROM maint_checkpoints WHERE checklist_id=$1 ORDER BY seq, id", s["checklist_id"])
        pts = [dict(r) for r in rows]
    return _json({"schedule": s, "checkpoints": pts})


@router.post("/complete")
async def complete(body: dict, emp: dict = Depends(require_employee)):
    await ensure_tables()
    sid = body.get("schedule_id")
    s = await db.fetchrow("SELECT * FROM maint_schedules WHERE id=$1", sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s = dict(s)
    results = body.get("results") or []
    status = "issues" if any((not r.get("ok", True)) for r in results) else "done"
    hrs = _num(body.get("running_hours"))
    today = _today()
    next_due = None
    if s.get("freq_days"):
        next_due = today + timedelta(days=int(s["freq_days"]))
    await db.execute(
        "INSERT INTO maint_logs (asset_id,schedule_id,checklist_id,done_by,done_by_emp_id,"
        "running_hours,status,results,remarks,next_due) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
        s["asset_id"], sid, s.get("checklist_id"),
        emp.get("name") or emp.get("emp_no"), emp.get("id"),
        hrs, status, json.dumps(results), (body.get("remarks") or "").strip(), next_due)
    await db.execute("UPDATE maint_schedules SET last_done=$1, last_done_hours=$2 WHERE id=$3",
                     today, hrs, sid)
    if hrs is not None:
        await db.execute("UPDATE maint_assets SET running_hours=$1, hours_updated_at=now() WHERE id=$2",
                         hrs, s["asset_id"])
    return {"ok": True, "status": status, "next_due": next_due.isoformat() if next_due else None}


@router.get("/history/list")
async def history_list(asset: int = Query(default=0), limit: int = Query(default=100),
                       emp: dict = Depends(require_employee)):
    await ensure_tables()
    if asset:
        rows = await db.fetch(
            "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
            "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
            "LEFT JOIN maint_schedules s ON s.id=l.schedule_id "
            "WHERE l.asset_id=$1 ORDER BY l.done_at DESC LIMIT $2", asset, limit)
    else:
        rows = await db.fetch(
            "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
            "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
            "LEFT JOIN maint_schedules s ON s.id=l.schedule_id "
            "ORDER BY l.done_at DESC LIMIT $1", limit)
    return _json([dict(r) for r in rows])


# =======================================================================
#  BOARD (due / overdue / upcoming)
# =======================================================================
@router.get("/board")
async def board(emp: dict = Depends(require_employee)):
    await ensure_tables()
    today = _today()
    due, overdue, upcoming = [], [], []
    for s in await _schedule_rows():
        if not s.get("active"):
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        s.update(info)
        if info["overdue"]:
            overdue.append(s)
        elif info["due"]:
            due.append(s)
        elif info["next_due"]:
            nd = _d(info["next_due"])
            if nd and nd <= today + timedelta(days=7):
                upcoming.append(s)
    recent = await db.fetch(
        "SELECT l.*, a.name AS asset_name, a.code AS asset_code, s.title AS sched_title "
        "FROM maint_logs l LEFT JOIN maint_assets a ON a.id=l.asset_id "
        "LEFT JOIN maint_schedules s ON s.id=l.schedule_id ORDER BY l.done_at DESC LIMIT 12")
    return _json({"today": today.isoformat(), "overdue": overdue, "due": due,
                  "upcoming": upcoming, "recent": [dict(r) for r in recent]})


# =======================================================================
#  WHATSAPP REMINDERS (cron / agent endpoint)
# =======================================================================
async def _run_reminders(dry: bool = False) -> dict:
    await ensure_tables()
    today = _today()
    sent, skipped = [], []
    for s in await _schedule_rows():
        if not s.get("active"):
            continue
        info = compute_due(s, s.get("asset_hours"), today)
        if not info["due"] and not info["overdue"]:
            continue
        assignee_mobile = s.get("assignee_mobile") or ""
        asset = f'{s.get("asset_name") or ""} ({s.get("asset_code") or ""})'.strip()
        when = "OVERDUE" if info["overdue"] else "due today"
        if info["overdue"]:
            tmpl, target = WATI_MAINT_OVERDUE_TEMPLATE, (MAINT_SUPERVISOR_MOBILE or assignee_mobile)
            params = [asset, s.get("title") or "", info.get("next_due") or "", s.get("assignee_name") or "unassigned"]
        else:
            tmpl, target = WATI_MAINT_DUE_TEMPLATE, assignee_mobile
            params = [s.get("assignee_name") or "", asset, s.get("title") or "", info.get("next_due") or "today"]
        rec = {"schedule_id": s["id"], "asset": asset, "title": s.get("title"),
               "state": when, "to": target, "assignee": s.get("assignee_name")}
        if not target:
            rec["result"] = "skipped: no mobile"; skipped.append(rec); continue
        if not tmpl:
            rec["result"] = "skipped: template not set"; skipped.append(rec); continue
        if dry:
            rec["result"] = "dry-run"; sent.append(rec); continue
        ok, detail = await wati.send_template(tmpl, target, params)
        rec["result"] = "sent" if ok else f"failed: {detail}"
        (sent if ok else skipped).append(rec)
    return {"today": today.isoformat(), "sent": sent, "skipped": skipped,
            "counts": {"sent": len(sent), "skipped": len(skipped)}}


@router.get("/run-reminders")
async def run_reminders(token: str = Query(default=""), dry: int = Query(default=0)):
    """Protected cron endpoint — call daily. ?token=<AGENT_TOKEN>  (&dry=1 to preview)."""
    if not AGENT_TOKEN or token != AGENT_TOKEN:
        raise HTTPException(401, "bad token")
    return _json(await _run_reminders(dry=bool(dry)))


@router.get("/reminders/preview")
async def reminders_preview(emp: dict = Depends(require_employee)):
    """Staff-only preview of what the reminder run would send right now."""
    return _json(await _run_reminders(dry=True))
