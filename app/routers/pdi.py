"""Pre-Delivery Inspection (PDI) API — per-SO reports with spec vs observed
dimensions, surface checks and auto out-of-spec flags. Same contract as the Node
app: Neon table `pdi_reports`, auto report number BSCQMS-PRD-009-<DDMMYY>-<NN>,
a CSV register on OneDrive and a per-report PDF."""
import json
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph, odsync
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_pdi_pdf

router = APIRouter(prefix="/pdi", tags=["pdi"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

PDI_TOL = {"thickness": 0.5, "width": 5, "length": 10}
CSV_PATH = f"{ONEDRIVE_BASE}/Pre-Delivery/PDI_Register.csv"


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS pdi_reports (
            id SERIAL PRIMARY KEY, report_no TEXT UNIQUE, report_date DATE, customer TEXT,
            items JSONB, inspected_by TEXT, reviewed_by TEXT, approved_by TEXT,
            pdf_path TEXT, created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ)
    """)
    # Self-heal: older copies of this table may be missing newer columns, which
    # makes the ON CONFLICT ... DO UPDATE (e.g. updated_at=now()) fail. Add any gaps.
    for col, typ in (("report_date", "DATE"), ("customer", "TEXT"), ("items", "JSONB"),
                     ("inspected_by", "TEXT"), ("reviewed_by", "TEXT"), ("approved_by", "TEXT"),
                     ("pdf_path", "TEXT"), ("created_by", "TEXT"),
                     ("created_at", "TIMESTAMPTZ"), ("updated_at", "TIMESTAMPTZ")):
        try:
            await db.execute(f"ALTER TABLE pdi_reports ADD COLUMN IF NOT EXISTS {col} {typ}")
        except Exception as e:
            print("[pdi-ensure]", col, e)
    _ensured = True


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


async def _next_number() -> str:
    d = datetime.now(IST)
    ddmmyy = f"{d.day:02d}{d.month:02d}{str(d.year)[-2:]}"
    prefix = f"BSCQMS-PRD-009-{ddmmyy}-"
    n = await db.fetchval(
        "SELECT COALESCE(MAX((split_part(report_no,'-',5))::int),0) FROM pdi_reports "
        "WHERE report_no LIKE $1", prefix + "%")
    return prefix + f"{int(n or 0) + 1:02d}"


@router.get("/next-number")
async def pdi_next(emp: dict = Depends(require_employee)):
    await ensure_table()
    return JSONResponse({"report_no": await _next_number()}, headers={"Cache-Control": "no-store"})


async def _append_csv(token, row: dict):
    ex = await graph.read_text(token, CSV_PATH) or (
        "Report No,Date,Customer,Items,Inspected By,Reviewed By,Approved By,Created By,Created At\n")
    key = odsync.csv_cell(row["report_no"]) + ","
    if ex.startswith(key) or ("\n" + key) in ex:
        return "exists"
    line = odsync.csv_line([
        row["report_no"], row["report_date"], row["customer"],
        f"{len(row.get('items') or [])} item(s)", row["inspected_by"],
        row.get("reviewed_by", ""), row.get("approved_by", ""), row.get("created_by", ""),
        row.get("created_at", "")])
    if not ex.endswith("\n"):
        ex += "\n"
    await graph.upload_file(token, CSV_PATH, (ex + line).encode("utf-8"), "text/csv")


async def od_sync(rno: str, dedupe: bool = False) -> dict:
    """PDF -> Pre-Delivery/<report no>.pdf and a line in PDI_Register.csv (awaited)."""
    await ensure_table()
    st = odsync.Steps("pdi", rno)
    row = await db.fetchrow("SELECT * FROM pdi_reports WHERE report_no=$1", rno)
    if not row:
        st.fail("record", "not found in database")
        return await odsync.record(st)
    d = dict(row)
    ca = d.get("created_at")
    rep = {"report_no": d["report_no"], "report_date": str(d.get("report_date") or ""),
           "customer": d.get("customer") or "", "items": odsync.as_obj(d.get("items"), []) or [],
           "inspected_by": d.get("inspected_by") or "", "reviewed_by": d.get("reviewed_by") or "",
           "approved_by": d.get("approved_by") or "", "created_by": d.get("created_by") or "",
           "created_at": ca.astimezone(IST).strftime("%d-%b-%Y %H:%M") if ca else ""}
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in rno)
    path = f"{ONEDRIVE_BASE}/Pre-Delivery/{safe}.pdf"
    token = await st.run("OneDrive sign-in", graph.get_token())
    if token:
        await st.run("PDF report", graph.upload_file(token, path, build_pdi_pdf(rep, PDI_TOL),
                                                     "application/pdf"))
        if "PDF report" in st.saved:
            await db.execute("UPDATE pdi_reports SET pdf_path=$1 WHERE report_no=$2", path, rno)
        await st.run("Register CSV (PDI_Register.csv)", _append_csv(token, rep))
    return await odsync.record(st)


class PdiIn(BaseModel):
    report_no: str
    report_date: Optional[str] = None
    customer: Optional[str] = ""
    items: list = []
    inspected_by: Optional[str] = ""
    reviewed_by: Optional[str] = ""
    approved_by: Optional[str] = ""


@router.get("/list")
async def pdi_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    rows = await db.fetch(
        "SELECT report_no,report_date,customer,inspected_by,created_by,created_at,pdf_path,"
        "CASE WHEN jsonb_typeof(items)='array' THEN jsonb_array_length(items) ELSE 0 END "
        "AS item_count FROM pdi_reports "
        "ORDER BY created_at DESC LIMIT 3000")
    return JSONResponse(jsonable_encoder([dict(r) for r in rows]),
                        headers={"Cache-Control": "no-store"})


@router.get("/record")
async def pdi_record(report_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM pdi_reports WHERE report_no=$1", report_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    items = d.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    return JSONResponse(jsonable_encoder({
        "report_no": d["report_no"], "report_date": d.get("report_date"),
        "customer": d.get("customer"), "inspected_by": d.get("inspected_by"),
        "items": items or []}), headers={"Cache-Control": "no-store"})


def _rep(body: PdiIn):
    return {"report_no": body.report_no.strip(), "report_date": body.report_date or "",
            "customer": body.customer or "", "items": body.items or [],
            "inspected_by": body.inspected_by or "", "reviewed_by": body.reviewed_by or "",
            "approved_by": body.approved_by or ""}


@router.post("/submit")
async def pdi_submit(body: PdiIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    rno = (body.report_no or "").strip()
    if not rno:                       # auto-assign if the form didn't have a number yet
        rno = await _next_number()
    by = _emp_name(emp)
    try:
        rdate = date.fromisoformat(str(body.report_date)[:10]) if body.report_date else None
    except ValueError:
        rdate = None
    try:
        await db.execute(
            "INSERT INTO pdi_reports (report_no,report_date,customer,items,inspected_by,reviewed_by,"
            "approved_by,created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT (report_no) DO UPDATE SET report_date=EXCLUDED.report_date,"
            "customer=EXCLUDED.customer,items=EXCLUDED.items,inspected_by=EXCLUDED.inspected_by,"
            "reviewed_by=EXCLUDED.reviewed_by,approved_by=EXCLUDED.approved_by,updated_at=now()",
            rno, rdate, body.customer or "", (body.items or []),
            body.inspected_by or "", body.reviewed_by or "", body.approved_by or "", by)
    except Exception as e:
        print("[pdi-submit]", repr(e))
        raise HTTPException(500, f"Save failed: {e}")
    od = await od_sync(rno)              # awaited: Vercel kills post-response work
    return {"ok": True, "report_no": rno, "onedrive": od}


class DelIn(BaseModel):
    report_no: str


@router.post("/delete")
async def pdi_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM pdi_reports WHERE report_no=$1", body.report_no)
    return {"ok": True}


@router.get("/pdf")
async def pdi_pdf(report_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM pdi_reports WHERE report_no=$1", report_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    items = d.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    rep = {"report_no": d["report_no"], "report_date": str(d.get("report_date") or ""),
           "customer": d.get("customer") or "", "items": items or [],
           "inspected_by": d.get("inspected_by") or ""}
    pdf = build_pdi_pdf(rep, PDI_TOL)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(report_no))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"',
                             "Cache-Control": "no-store"})
