"""Quality Complaint API — customer & vendor complaint intake with a register
and status tracking (Open → Under Investigation → Resolved → Closed). Stores to
Neon (`quality_complaints`), appends a CSV register, uploads defect photos and
document attachments to OneDrive, and renders a per-complaint PDF."""
import asyncio
import base64
import json
import re
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
from ..pdfgen import build_complaint_pdf

router = APIRouter(prefix="/complaint", tags=["complaint"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

CSV_PATH = f"{ONEDRIVE_BASE}/Complaints/Complaints_Register.csv"
_DATAURL = re.compile(r"^data:([^;]+);base64,(.+)$", re.I | re.S)

_FIELDS = ("source", "filed_by", "qc_date", "customer_name", "customer_email", "grade",
           "dimensions", "batch_number", "tc_number", "invoice_no", "invoice_date",
           "quantity", "mill", "vendor_name", "vendor_email", "defect_description",
           "status", "resolution")


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS quality_complaints (
            id SERIAL PRIMARY KEY, complaint_no TEXT UNIQUE, source TEXT, filed_by TEXT,
            qc_date DATE, customer_name TEXT, customer_email TEXT, grade TEXT, dimensions TEXT,
            batch_number TEXT, tc_number TEXT, invoice_no TEXT, invoice_date DATE, quantity TEXT,
            mill TEXT, vendor_name TEXT, vendor_email TEXT, defect_description TEXT,
            status TEXT DEFAULT 'Open', resolution TEXT, payload JSONB, pdf_path TEXT,
            created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ)
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_qc_date ON quality_complaints (qc_date)")
    _ensured = True


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


def _safe(s, n=50):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or ""))[:n]


async def _next_number() -> str:
    yr = datetime.now(IST).year
    n = await db.fetchval(
        "SELECT COALESCE(MAX((split_part(complaint_no,'-',4))::int),0) FROM quality_complaints "
        "WHERE complaint_no LIKE $1", f"BSC-QC-{yr}-%")
    return f"BSC-QC-{yr}-{int(n or 0) + 1:03d}"


@router.get("/next-number")
async def qc_next(emp: dict = Depends(require_employee)):
    await ensure_table()
    return JSONResponse({"complaint_no": await _next_number()}, headers={"Cache-Control": "no-store"})


async def _upload_asset(token, folder: str, name: str, data_url: str) -> Optional[dict]:
    m = _DATAURL.match(data_url or "")
    if not m:
        return None
    ctype = m.group(1)
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return None
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
           "application/pdf": "pdf"}.get(ctype, "bin")
    fname = _safe(name) or f"file.{ext}"
    if "." not in fname:
        fname += "." + ext
    path = f"{ONEDRIVE_BASE}/Complaints/{folder}/{fname}"
    try:
        await graph.upload_file(token, path, raw, ctype)
        return {"name": name, "path": path}
    except Exception as e:
        print("[qc-asset]", e)
        return None


async def _handle_assets(cno: str, attachments: dict, photos: list) -> dict:
    out = {"attachments": {}, "photos": []}
    try:
        token = await graph.get_token()
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        for key in ("po", "so", "tc", "invoice"):
            a = (attachments or {}).get(key)
            if a and a.get("data"):
                r = await _upload_asset(token, _safe(cno), f"{cno}_{key}_{a.get('name') or key}", a["data"])
                if r:
                    out["attachments"][key] = r
        for i, p in enumerate(photos or []):
            r = await _upload_asset(token, _safe(cno), f"{cno}_photo_{i+1}_{stamp}.jpg", p)
            if r:
                out["photos"].append(r["path"])
    except Exception as e:
        print("[qc-assets]", e)
    return out


async def _append_csv(row: dict, by: str):
    try:
        token = await graph.get_token()
        ex = await graph.read_text(token, CSV_PATH) or (
            "Complaint No,Source,Date,Filed By,Customer,Grade,Batch,Invoice,Qty (T),Mill,"
            "Vendor,Status,Defect,Created By,Created At\n")

        def c(v):
            v = "" if v is None else str(v)
            return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n') else v

        line = ",".join(c(x) for x in [
            row["complaint_no"], row["source"], row["qc_date"], row["filed_by"],
            row["customer_name"], row["grade"], row["batch_number"], row["invoice_no"],
            row["quantity"], row["mill"], row["vendor_name"], row["status"],
            row["defect_description"], by, datetime.now(IST).strftime("%d-%b-%Y %H:%M")]) + "\n"
        await graph.upload_file(token, CSV_PATH, (ex + line).encode("utf-8"), "text/csv")
    except Exception as e:
        print("[qc-csv]", e)


async def _write_pdf(cno: str, rep: dict):
    try:
        pdf = build_complaint_pdf(rep)
        path = f"{ONEDRIVE_BASE}/Complaints/{_safe(cno)}.pdf"
        token = await graph.get_token()
        await graph.upload_file(token, path, pdf, "application/pdf")
        await db.execute("UPDATE quality_complaints SET pdf_path=$1 WHERE complaint_no=$2", path, cno)
    except Exception as e:
        print("[qc-pdf]", e)


class QCIn(BaseModel):
    complaint_no: str
    source: Optional[str] = "Customer"
    filed_by: Optional[str] = ""
    qc_date: Optional[str] = None
    customer_name: Optional[str] = ""
    customer_email: Optional[str] = ""
    grade: Optional[str] = ""
    dimensions: Optional[str] = ""
    batch_number: Optional[str] = ""
    tc_number: Optional[str] = ""
    invoice_no: Optional[str] = ""
    invoice_date: Optional[str] = None
    quantity: Optional[str] = ""
    mill: Optional[str] = ""
    vendor_name: Optional[str] = ""
    vendor_email: Optional[str] = ""
    defect_description: Optional[str] = ""
    status: Optional[str] = "Open"
    resolution: Optional[str] = ""
    attachments: dict = {}
    photos: list = []


def _d(v):
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _rep(body: QCIn, by: str, assets: dict) -> dict:
    r = {f: getattr(body, f) or "" for f in _FIELDS}
    r["complaint_no"] = body.complaint_no.strip()
    r["created_by"] = by
    r["assets"] = assets
    return r


@router.get("/list")
async def qc_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    rows = await db.fetch(
        "SELECT complaint_no,source,qc_date,customer_name,grade,batch_number,invoice_no,"
        "quantity,mill,vendor_name,status,defect_description,pdf_path,created_by,created_at "
        "FROM quality_complaints ORDER BY created_at DESC LIMIT 3000")
    return JSONResponse(jsonable_encoder([dict(r) for r in rows]),
                        headers={"Cache-Control": "no-store"})


@router.get("/record")
async def qc_record(complaint_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM quality_complaints WHERE complaint_no=$1", complaint_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    payload = d.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    d["payload"] = payload or {}
    return JSONResponse(jsonable_encoder(d), headers={"Cache-Control": "no-store"})


@router.post("/submit")
async def qc_submit(body: QCIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.complaint_no:
        raise HTTPException(400, "complaint_no required")
    cno = body.complaint_no.strip()
    by = _emp_name(emp)
    # upload assets first so paths can be stored in payload
    assets = await _handle_assets(cno, body.attachments or {}, body.photos or [])
    vals = {f: getattr(body, f) or "" for f in _FIELDS}
    await db.execute(
        "INSERT INTO quality_complaints (complaint_no,source,filed_by,qc_date,customer_name,"
        "customer_email,grade,dimensions,batch_number,tc_number,invoice_no,invoice_date,quantity,"
        "mill,vendor_name,vendor_email,defect_description,status,resolution,payload,created_by) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21) "
        "ON CONFLICT (complaint_no) DO UPDATE SET source=EXCLUDED.source,filed_by=EXCLUDED.filed_by,"
        "qc_date=EXCLUDED.qc_date,customer_name=EXCLUDED.customer_name,customer_email=EXCLUDED.customer_email,"
        "grade=EXCLUDED.grade,dimensions=EXCLUDED.dimensions,batch_number=EXCLUDED.batch_number,"
        "tc_number=EXCLUDED.tc_number,invoice_no=EXCLUDED.invoice_no,invoice_date=EXCLUDED.invoice_date,"
        "quantity=EXCLUDED.quantity,mill=EXCLUDED.mill,vendor_name=EXCLUDED.vendor_name,"
        "vendor_email=EXCLUDED.vendor_email,defect_description=EXCLUDED.defect_description,"
        "status=EXCLUDED.status,resolution=EXCLUDED.resolution,payload=EXCLUDED.payload,updated_at=now()",
        cno, vals["source"] or "Customer", vals["filed_by"], _d(body.qc_date), vals["customer_name"],
        vals["customer_email"], vals["grade"], vals["dimensions"], vals["batch_number"],
        vals["tc_number"], vals["invoice_no"], _d(body.invoice_date), vals["quantity"], vals["mill"],
        vals["vendor_name"], vals["vendor_email"], vals["defect_description"],
        vals["status"] or "Open", vals["resolution"], json.dumps({"assets": assets}), by)
    rep = _rep(body, by, assets)

    async def _bg():
        await _write_pdf(cno, rep)
        await _append_csv(rep, by)
    asyncio.create_task(_bg())
    return {"ok": True, "complaint_no": cno}


class StatusIn(BaseModel):
    complaint_no: str
    status: str
    resolution: Optional[str] = ""


@router.post("/status")
async def qc_status(body: StatusIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute(
        "UPDATE quality_complaints SET status=$1, resolution=$2, updated_at=now() WHERE complaint_no=$3",
        body.status, body.resolution or "", body.complaint_no)
    return {"ok": True}


class DelIn(BaseModel):
    complaint_no: str


@router.post("/delete")
async def qc_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM quality_complaints WHERE complaint_no=$1", body.complaint_no)
    return {"ok": True}


@router.get("/pdf")
async def qc_pdf(complaint_no: str = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM quality_complaints WHERE complaint_no=$1", complaint_no)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    payload = d.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    rep = {f: d.get(f) for f in _FIELDS}
    rep["complaint_no"] = d["complaint_no"]
    rep["created_by"] = d.get("created_by")
    rep["qc_date"] = str(d.get("qc_date") or "")
    rep["invoice_date"] = str(d.get("invoice_date") or "")
    rep["assets"] = (payload or {}).get("assets", {})
    pdf = build_complaint_pdf(rep)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{_safe(complaint_no)}.pdf"',
                             "Cache-Control": "no-store"})
