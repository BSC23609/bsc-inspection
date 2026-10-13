"""8D Report API — the problem-solving report (D1–D8) combined with its
register. Same contract as the Node app: Neon table `eightd_reports`, auto report
number BSC-8D-<YYYY>-<NNN>, a CSV register on OneDrive and a per-report PDF.
The full D1–D8 content is stored in an added `details` JSONB column."""
import asyncio
import json
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_8d_pdf

router = APIRouter(prefix="/eightd", tags=["eightd"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

CSV_PATH = f"{ONEDRIVE_BASE}/8D reports/8D_Register.csv"


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS eightd_reports (
            id SERIAL PRIMARY KEY, report_no TEXT UNIQUE, report_date DATE, customer TEXT,
            part TEXT, ncr_ref TEXT, priority TEXT, status TEXT, pdf_path TEXT,
            created_by TEXT, created_at TIMESTAMPTZ DEFAULT now())
    """)
    # additive columns for the combined form (safe on the legacy table)
    await db.execute("ALTER TABLE eightd_reports ADD COLUMN IF NOT EXISTS details JSONB")
    await db.execute("ALTER TABLE eightd_reports ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    _ensured = True


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


async def _next_number() -> str:
    yr = datetime.now(IST).year
    n = await db.fetchval(
        "SELECT COALESCE(MAX((split_part(report_no,'-',4))::int),0) FROM eightd_reports "
        "WHERE report_no LIKE $1", f"BSC-8D-{yr}-%")
    return f"BSC-8D-{yr}-{int(n or 0) + 1:03d}"


@router.get("/next-number")
async def eightd_next(emp: dict = Depends(require_employee)):
    await ensure_table()
    return JSONResponse({"report_no": await _next_number()}, headers={"Cache-Control": "no-store"})


async def _append_csv(row: dict, by: str):
    try:
        token = await graph.get_token()
        ex = await graph.read_text(token, CSV_PATH) or (
            "Report No,Date,Customer,Part,NCR Ref,Priority,Status,PDF,Created By,Created At\n")

        def c(v):
            v = "" if v is None else str(v)
            return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n') else v

        line = ",".join(c(x) for x in [
            row["report_no"], row["report_date"], row["customer"], row["part"],
            row["ncr_ref"], row["priority"], row["status"], row.get("pdf_path", ""),
            by, datetime.now(IST).strftime("%d-%b-%Y %H:%M")]) + "\n"
        await graph.upload_file(token, CSV_PATH, (ex + line).encode("utf-8"), "text/csv")
    except Exception as e:
        print("[8d-csv]", e)


async def _write_pdf(rno: str, rep: dict):
    try:
        pdf = build_8d_pdf(rep)
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in rno)
        path = f"{ONEDRIVE_BASE}/8D reports/{safe}.pdf"
        token = await graph.get_token()
        await graph.upload_file(token, path, pdf, "application/pdf")
        await db.execute("UPDATE eightd_reports SET pdf_path=$1 WHERE report_no=$2", path, rno)
    except Exception as e:
        print("[8d-pdf]", e)


class EightDIn(BaseModel):
    report_no: str
    report_date: Optional[str] = None
    customer: Optional[str] = ""
    part: Optional[str] = ""
    ncr_ref: Optional[str] = ""
    priority: Optional[str] = ""
    status: Optional[str] = "Open"
    details: dict = {}


@router.get("/list")
async def eightd_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    rows = await db.fetch(
        "SELECT report_no,report_date,customer,part,ncr_ref,priority,status,pdf_path,"
        "created_by,created_at FROM eightd_reports ORDER BY created_at DESC LIMIT 3000")
    return JSONResponse(jsonable_encoder([dict(r) for r in rows]),
                        headers={"Cache-Control": "no-store"})


@router.get("/record")
async def eightd_record(report_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM eightd_reports WHERE report_no=$1", report_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    det = d.get("details")
    if isinstance(det, str):
        try:
            det = json.loads(det)
        except Exception:
            det = {}
    return JSONResponse(jsonable_encoder({
        "report_no": d["report_no"], "report_date": d.get("report_date"),
        "customer": d.get("customer"), "part": d.get("part"), "ncr_ref": d.get("ncr_ref"),
        "priority": d.get("priority"), "status": d.get("status"), "details": det or {}}),
        headers={"Cache-Control": "no-store"})


def _rep(body: EightDIn, by: str) -> dict:
    return {"report_no": body.report_no.strip(), "report_date": body.report_date or "",
            "customer": body.customer or "", "part": body.part or "", "ncr_ref": body.ncr_ref or "",
            "priority": body.priority or "", "status": body.status or "Open",
            "details": body.details or {}, "created_by": by}


@router.post("/submit")
async def eightd_submit(body: EightDIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.report_no:
        raise HTTPException(400, "report_no required")
    rno = body.report_no.strip()
    by = _emp_name(emp)
    try:
        rdate = date.fromisoformat(str(body.report_date)[:10]) if body.report_date else None
    except ValueError:
        rdate = None
    await db.execute(
        "INSERT INTO eightd_reports (report_no,report_date,customer,part,ncr_ref,priority,status,"
        "details,created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
        "ON CONFLICT (report_no) DO UPDATE SET report_date=EXCLUDED.report_date,"
        "customer=EXCLUDED.customer,part=EXCLUDED.part,ncr_ref=EXCLUDED.ncr_ref,"
        "priority=EXCLUDED.priority,status=EXCLUDED.status,details=EXCLUDED.details,updated_at=now()",
        rno, rdate, body.customer or "", body.part or "", body.ncr_ref or "",
        body.priority or "", body.status or "Open", json.dumps(body.details or {}), by)
    rep = _rep(body, by)

    async def _bg():
        await _write_pdf(rno, rep)
        await _append_csv(rep, by)
    asyncio.create_task(_bg())
    return {"ok": True, "report_no": rno}


class DelIn(BaseModel):
    report_no: str


@router.post("/delete")
async def eightd_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM eightd_reports WHERE report_no=$1", body.report_no)
    return {"ok": True}


@router.get("/pdf")
async def eightd_pdf(report_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM eightd_reports WHERE report_no=$1", report_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    det = d.get("details")
    if isinstance(det, str):
        try:
            det = json.loads(det)
        except Exception:
            det = {}
    rep = {"report_no": d["report_no"], "report_date": str(d.get("report_date") or ""),
           "customer": d.get("customer") or "", "part": d.get("part") or "",
           "ncr_ref": d.get("ncr_ref") or "", "priority": d.get("priority") or "",
           "status": d.get("status") or "", "details": det or {}, "created_by": d.get("created_by") or ""}
    pdf = build_8d_pdf(rep)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(report_no))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{safe}.pdf"',
                             "Cache-Control": "no-store"})
