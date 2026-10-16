"""Diesel Generator Log API. Reuses the `dg_log` table (same schema as the Node
build), writes a CSV log to OneDrive and renders the monthly PDF with ReportLab."""
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Request, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph, odsync
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_dg_pdf, dg_minutes, fmt_min

router = APIRouter(prefix="/dg", tags=["dg"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS dg_log (
            id SERIAL PRIMARY KEY, dg_set TEXT DEFAULT 'DG-320 kVA', log_date DATE,
            start_at TEXT, stop_at TEXT, running_minutes INTEGER,
            fuel_added NUMERIC, diesel_level NUMERIC, kwh_start NUMERIC, kwh_end NUMERIC, units NUMERIC,
            remarks TEXT, sign TEXT, created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ)
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_dg_log_date ON dg_log (log_date)")
    _ensured = True


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class DgIn(BaseModel):
    id: Optional[int] = None
    dg_set: Optional[str] = "DG-320 kVA"
    log_date: Optional[str] = None
    start_at: Optional[str] = ""
    stop_at: Optional[str] = ""
    running_minutes: Optional[int] = None
    fuel_added: Optional[float] = None
    diesel_level: Optional[float] = None
    kwh_start: Optional[float] = None
    kwh_end: Optional[float] = None
    units: Optional[float] = None
    remarks: Optional[str] = ""
    sign: Optional[str] = ""


def _normalize(b: DgIn):
    if not b.log_date or not b.start_at:
        raise HTTPException(400, "log_date and start_at required")
    try:
        d = date.fromisoformat(str(b.log_date)[:10])
    except ValueError:
        raise HTTPException(400, "bad log_date")
    mins = b.running_minutes
    if mins is None:
        mins = dg_minutes(b.start_at, b.stop_at)
    ks, ke = _num(b.kwh_start), _num(b.kwh_end)
    units = b.units
    if units is None and ks is not None and ke is not None:
        units = round(ke - ks, 2)
    return {
        "dg_set": (b.dg_set or "DG-320 kVA")[:60],
        "log_date": d,
        "start_at": (b.start_at or "")[:8],
        "stop_at": (b.stop_at or "")[:8],
        "running_minutes": int(mins) if mins is not None else None,
        "fuel_added": _num(b.fuel_added),
        "diesel_level": _num(b.diesel_level),
        "kwh_start": ks, "kwh_end": ke, "units": _num(units),
        "remarks": (b.remarks or "")[:500],
        "sign": (b.sign or "")[:120],
    }


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


async def _append_csv(token, row: dict):
    path = f"{ONEDRIVE_BASE}/DG Log/DG_Log.csv"
    ex = await graph.read_text(token, path) or (
        "DG Set,Date,Start Time,End Time,Running Hours,Fuel Added (L),Diesel Level (L),"
        "Start Load (kWh),End Load (kWh),Units (kWh),Remarks,Sign,Entered By,Entered At\n")
    key = ",".join(odsync.csv_cell(x) for x in (row["dg_set"], row["log_date"], row["start_at"])) + ","
    if ex.startswith(key) or ("\n" + key) in ex:
        return "exists"
    ca = row.get("created_at")
    line = odsync.csv_line([
        row["dg_set"], row["log_date"], row["start_at"], row["stop_at"],
        fmt_min(row["running_minutes"]), row["fuel_added"], row["diesel_level"],
        row["kwh_start"], row["kwh_end"], row["units"], row["remarks"], row["sign"],
        row.get("created_by") or "",
        (ca.astimezone(IST) if ca else datetime.now(IST)).strftime("%d-%b-%Y %H:%M")])
    if not ex.endswith("\n"):
        ex += "\n"
    await graph.upload_file(token, path, (ex + line).encode("utf-8"), "text/csv")


async def _month_pdf(token, dg_set: str, log_date):
    """Refresh that DG set's monthly report PDF on OneDrive (DG Log/PDF/)."""
    if not log_date:
        return "none"
    rows = [dict(r) for r in await db.fetch(
        "SELECT * FROM dg_log WHERE dg_set=$1 AND date_trunc('month',log_date)=date_trunc('month',$2::date) "
        "ORDER BY log_date ASC NULLS LAST, start_at ASC", dg_set, log_date)]
    month = log_date.strftime("%Y-%m")
    pdf = build_dg_pdf(rows, dg_set, month)
    name = f"DG_Log_{dg_set.replace(' ', '_')}_{month}.pdf"
    await graph.upload_file(token, f"{ONEDRIVE_BASE}/DG Log/PDF/{name}", pdf, "application/pdf")


async def od_sync(rec_id: int, csv: bool = True, dedupe: bool = False) -> dict:
    """DG_Log.csv line + refreshed monthly PDF (awaited inside the request)."""
    await ensure_table()
    st = odsync.Steps("dg", rec_id)
    row = await db.fetchrow("SELECT * FROM dg_log WHERE id=$1", rec_id)
    if not row:
        st.fail("record", "not found in database")
        return await odsync.record(st)
    r = dict(row)
    token = await st.run("OneDrive sign-in", graph.get_token())
    if token:
        if csv:
            await st.run("Log CSV (DG_Log.csv)", _append_csv(token, r))
        await st.run("Monthly PDF report", _month_pdf(token, r["dg_set"], r["log_date"]))
    return await odsync.record(st)


@router.get("/list")
async def dg_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    rows = await db.fetch(
        "SELECT id,dg_set,log_date,start_at,stop_at,running_minutes,fuel_added,diesel_level,"
        "kwh_start,kwh_end,units,remarks,sign,created_by,created_at FROM dg_log "
        "ORDER BY log_date DESC NULLS LAST, start_at DESC LIMIT 2000")
    out = jsonable_encoder([dict(r) for r in rows])
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.post("/submit")
async def dg_submit(body: DgIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    r = _normalize(body)
    by = _emp_name(emp)
    new_id = await db.fetchval(
        "INSERT INTO dg_log (dg_set,log_date,start_at,stop_at,running_minutes,fuel_added,"
        "diesel_level,kwh_start,kwh_end,units,remarks,sign,created_by) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id",
        r["dg_set"], r["log_date"], r["start_at"], r["stop_at"], r["running_minutes"],
        r["fuel_added"], r["diesel_level"], r["kwh_start"], r["kwh_end"], r["units"],
        r["remarks"], r["sign"], by)
    od = await od_sync(new_id)           # awaited: Vercel kills post-response work
    return {"ok": True, "id": new_id, "onedrive": od}


@router.post("/update")
async def dg_update(body: DgIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.id:
        raise HTTPException(400, "id required")
    r = _normalize(body)
    await db.execute(
        "UPDATE dg_log SET dg_set=$1,log_date=$2,start_at=$3,stop_at=$4,running_minutes=$5,"
        "fuel_added=$6,diesel_level=$7,kwh_start=$8,kwh_end=$9,units=$10,remarks=$11,sign=$12,"
        "updated_at=now() WHERE id=$13",
        r["dg_set"], r["log_date"], r["start_at"], r["stop_at"], r["running_minutes"],
        r["fuel_added"], r["diesel_level"], r["kwh_start"], r["kwh_end"], r["units"],
        r["remarks"], r["sign"], body.id)
    od = await od_sync(body.id, csv=False)   # refresh the monthly PDF
    return {"ok": True, "id": body.id, "onedrive": od}


class DelIn(BaseModel):
    id: int


@router.post("/delete")
async def dg_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM dg_log WHERE id=$1", body.id)
    return {"ok": True}


@router.get("/pdf")
async def dg_pdf(month: str = Query(""), dg: str = Query(""),
                 emp: dict = Depends(require_employee)):
    await ensure_table()
    where, args = [], []
    if dg:
        args.append(dg[:60]); where.append(f"dg_set=${len(args)}")
    if len(month) == 7 and month[4] == "-":
        try:
            args.append(date.fromisoformat(month + "-01"))
            where.append(f"date_trunc('month',log_date)=date_trunc('month',${len(args)}::date)")
        except ValueError:
            pass
    sql = "SELECT * FROM dg_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_date ASC NULLS LAST, start_at ASC"
    rows = [dict(r) for r in await db.fetch(sql, *args)]
    pdf = build_dg_pdf(rows, dg or "All DG sets", month or "All months")
    fn = f"DG_Log_{(dg or 'all').replace(' ', '_')}_{month or 'all'}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fn}"',
                             "Cache-Control": "no-store"})
