"""Coil Inward Inspection API — mother-coil arrival checks. Stores to Neon
(`coil_inward`) and, inside the request, writes to OneDrive exactly like the Node
app: PDF -> Inward/PDF/<batch>_(dd-mm-yyyy)_Inward.pdf, row -> Inward_Log.xlsx
(table InwardLog), photos -> Inward/Photos/<file name>/."""
import base64
import re
from datetime import date
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph, xlsxlog, odsync
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_coil_pdf

router = APIRouter(prefix="/coil", tags=["coil"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

INWARD_XLSX = f"{ONEDRIVE_BASE}/Inward/Inward_Log.xlsx"
INWARD_TABLE = "InwardLog"
# legacy InwardLog writer column order (used as a positional fallback)
_INWARD_POS = ["file", "timestamp", "vehicle_number", "batch_number", "make_of_coil", "grade",
               "width", "thickness", "coil_weight", "coil_id", "actual_thickness", "actual_width",
               "id_sticker", "edge_inner", "edge_outer", "scratch", "strapping", "rust",
               "other_damages", "inspected_by", "remarks"]


def _inward_record(row, hmap):
    """Map one InwardLog row to a coil record (prefer header names, else position)."""
    def by_pos(key):
        try:
            return xlsxlog.cell_at(row, _INWARD_POS.index(key))
        except ValueError:
            return ""

    def g(key, *names):
        v = xlsxlog.cell(row, hmap, *names) if names else ""
        return v or by_pos(key)

    ts = g("timestamp", "Timestamp")
    d = xlsxlog.cell(row, hmap, "Date") or xlsxlog.date_part(ts)
    return {
        "id": None, "_legacy": True,
        "log_date": xlsxlog.date_part(d),
        "vehicle_number": g("vehicle_number", "Vehicle Number", "Vehicle No", "Vehicle"),
        "batch_number": g("batch_number", "Batch Number", "Batch"),
        "make_of_coil": g("make_of_coil", "Make of Coil", "Make"),
        "grade": g("grade", "Grade"),
        "width": g("width", "Width", "Width (mm)"),
        "thickness": g("thickness", "Thickness", "Thickness (mm)", "Coil Thickness"),
        "coil_weight": g("coil_weight", "Coil Weight", "Coil Weight (T)", "Weight"),
        "coil_id": g("coil_id", "Coil ID", "Coil ID (mm)"),
        "actual_thickness": g("actual_thickness", "Actual Thickness"),
        "actual_width": g("actual_width", "Actual Width"),
        "id_sticker": g("id_sticker", "ID Sticker", "Identification Sticker"),
        "edge_inner": g("edge_inner", "Edge Inner", "Edge Damage Inner"),
        "edge_outer": g("edge_outer", "Edge Outer", "Edge Damage Outer"),
        "scratch": g("scratch", "Scratch", "Scratch Mark"),
        "strapping": g("strapping", "Strapping"),
        "rust": g("rust", "Rust", "Rust on Surface"),
        "other_damages": g("other_damages", "Other Damages", "Any Other Damages"),
        "inspected_by": g("inspected_by", "Inspector", "Inspected By"),
        "remarks": g("remarks", "Remarks"),
    }


async def _read_inward_legacy():
    """Read historical rows from the OneDrive InwardLog workbook (best effort)."""
    try:
        token = await graph.get_token()
        item_id = await graph.get_item_id(token, INWARD_XLSX)
        if not item_id:
            return []
        cols, rows = await graph.read_table(token, item_id, INWARD_TABLE)
        hmap = xlsxlog.header_map(cols)
        out = []
        for i, r in enumerate(rows):
            rec = _inward_record(r, hmap)
            if rec["batch_number"] or rec["vehicle_number"]:
                rec["_row"] = i
                out.append(rec)
        out.reverse()  # newest last in the sheet -> show newest first
        return out
    except Exception as e:
        print("[coil-legacy]", e)
        return []

_FIELDS = ("log_date", "vehicle_number", "batch_number", "make_of_coil", "grade",
           "width", "thickness", "coil_weight", "coil_id", "actual_thickness", "actual_width",
           "id_sticker", "edge_inner", "edge_outer", "scratch", "strapping", "rust",
           "other_damages", "wheels_india", "inspected_by", "remarks",
           "coil_photo", "sticker_photo")


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS coil_inward (
            id SERIAL PRIMARY KEY, log_date DATE,
            vehicle_number TEXT, batch_number TEXT, make_of_coil TEXT, grade TEXT,
            width TEXT, thickness NUMERIC, coil_weight NUMERIC, coil_id TEXT,
            actual_thickness NUMERIC, actual_width NUMERIC,
            id_sticker TEXT, edge_inner TEXT, edge_outer TEXT, scratch TEXT,
            strapping TEXT, rust TEXT, other_damages TEXT, wheels_india TEXT,
            inspected_by TEXT, remarks TEXT, coil_photo TEXT, sticker_photo TEXT,
            created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ)
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_coil_date ON coil_inward (log_date)")
    await db.execute("ALTER TABLE coil_inward ADD COLUMN IF NOT EXISTS file_name TEXT")
    _ensured = True


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(v, n=120):
    return (str(v).strip()[:n] if v is not None else "")


class CoilIn(BaseModel):
    id: Optional[int] = None
    log_date: Optional[str] = None
    vehicle_number: Optional[str] = ""
    batch_number: Optional[str] = ""
    make_of_coil: Optional[str] = ""
    grade: Optional[str] = ""
    width: Optional[str] = ""
    thickness: Optional[float] = None
    coil_weight: Optional[float] = None
    coil_id: Optional[str] = ""
    actual_thickness: Optional[float] = None
    actual_width: Optional[float] = None
    id_sticker: Optional[str] = ""
    edge_inner: Optional[str] = ""
    edge_outer: Optional[str] = ""
    scratch: Optional[str] = ""
    strapping: Optional[str] = ""
    rust: Optional[str] = ""
    other_damages: Optional[str] = ""
    wheels_india: Optional[str] = "No"
    inspected_by: Optional[str] = ""
    remarks: Optional[str] = ""
    # data: URLs on create; existing OneDrive paths are passed back unchanged on edit
    coil_photo: Optional[str] = ""
    sticker_photo: Optional[str] = ""


_DATAURL = re.compile(r"^data:image/(png|jpe?g|webp);base64,(.+)$", re.I | re.S)


async def _save_photo(token, kind: str, batch: str, data_url: str) -> str:
    """Upload a base64 data URL to Inward/Photos/<file name>/ (Node layout)."""
    m = _DATAURL.match(data_url or "")
    if not m:
        return ""
    ext = "jpg" if m.group(1).lower().startswith("jp") else m.group(1).lower()
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return ""
    fn = odsync.legacy_file_name(batch, None, "Inward")
    path = f"{ONEDRIVE_BASE}/Inward/Photos/{fn}/{kind}_photo.{ext}"
    ct = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg", "jpeg") else "image/webp"
    await graph.upload_file(token, path, raw, ct)
    return path


async def _normalize(b: CoilIn):
    if not b.log_date:
        raise HTTPException(400, "log_date required")
    if not (b.vehicle_number and b.batch_number):
        raise HTTPException(400, "vehicle_number and batch_number required")
    try:
        d = date.fromisoformat(str(b.log_date)[:10])
    except ValueError:
        raise HTTPException(400, "bad log_date")

    # photos: only upload fresh data: URLs; keep existing OneDrive paths as-is
    coil_photo, sticker_photo = b.coil_photo or "", b.sticker_photo or ""
    if coil_photo.startswith("data:") or sticker_photo.startswith("data:"):
        try:
            token = await graph.get_token()
            if coil_photo.startswith("data:"):
                coil_photo = await _save_photo(token, "coil", b.batch_number, coil_photo)
            if sticker_photo.startswith("data:"):
                sticker_photo = await _save_photo(token, "sticker", b.batch_number, sticker_photo)
        except Exception as e:
            print("[coil-photo]", e)
            if coil_photo.startswith("data:"):
                coil_photo = ""
            if sticker_photo.startswith("data:"):
                sticker_photo = ""

    return {
        "log_date": d,
        "vehicle_number": _s(b.vehicle_number, 40), "batch_number": _s(b.batch_number, 60),
        "make_of_coil": _s(b.make_of_coil, 60), "grade": _s(b.grade, 60),
        "width": _s(b.width, 30), "thickness": _num(b.thickness),
        "coil_weight": _num(b.coil_weight), "coil_id": _s(b.coil_id, 30),
        "actual_thickness": _num(b.actual_thickness), "actual_width": _num(b.actual_width),
        "id_sticker": _s(b.id_sticker, 8), "edge_inner": _s(b.edge_inner, 8),
        "edge_outer": _s(b.edge_outer, 8), "scratch": _s(b.scratch, 8),
        "strapping": _s(b.strapping, 8), "rust": _s(b.rust, 8),
        "other_damages": _s(b.other_damages, 120), "wheels_india": _s(b.wheels_india, 8) or "No",
        "inspected_by": _s(b.inspected_by, 80), "remarks": _s(b.remarks, 500),
        "coil_photo": _s(coil_photo, 300), "sticker_photo": _s(sticker_photo, 300),
    }


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


def _key(rec):
    return (str(rec.get("batch_number") or "").strip().lower(),
            str(rec.get("vehicle_number") or "").strip().lower(),
            str(rec.get("log_date") or "")[:10])


@router.get("/list")
async def coil_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    cols = "id," + ",".join(_FIELDS) + ",created_by,created_at"
    neon = [dict(r) for r in await db.fetch(
        f"SELECT {cols} FROM coil_inward ORDER BY log_date DESC NULLS LAST, id DESC LIMIT 2000")]
    seen = {_key(r) for r in neon}
    legacy = await _read_inward_legacy()
    merged = neon + [r for r in legacy if _key(r) not in seen]
    merged.sort(key=lambda r: str(r.get("log_date") or ""), reverse=True)
    return JSONResponse(jsonable_encoder(merged), headers={"Cache-Control": "no-store"})


async def od_sync(rec_id: int, excel: bool = True, dedupe: bool = False) -> dict:
    """Write one coil record to OneDrive (PDF + InwardLog row). Awaited inside the
    request. `dedupe` (backfill) skips the Excel row if the batch is already logged."""
    await ensure_table()
    st = odsync.Steps("coil", rec_id)
    row = await db.fetchrow("SELECT * FROM coil_inward WHERE id=$1", rec_id)
    if not row:
        st.fail("record", "not found in database")
        return await odsync.record(st)
    r = dict(row)
    fn = r.get("file_name") or odsync.legacy_file_name(r.get("batch_number"), r.get("created_at"), "Inward")
    if not r.get("file_name"):
        await db.execute("UPDATE coil_inward SET file_name=$1 WHERE id=$2", fn, rec_id)
    token = await st.run("OneDrive sign-in", graph.get_token())
    if not token:
        return await odsync.record(st)

    async def _pdf():
        await graph.upload_file(token, f"{ONEDRIVE_BASE}/Inward/PDF/{fn}.pdf",
                                build_coil_pdf(r), "application/pdf")
    await st.run("PDF report", _pdf())

    async def _xlsx():
        item_id = await graph.require_item_id(token, INWARD_XLSX)
        if dedupe:
            names = await graph.column_values(token, item_id, INWARD_TABLE, 0)
            batches = await graph.column_values(token, item_id, INWARD_TABLE, _INWARD_POS.index("batch_number"))
            if fn in names or odsync.norm(r.get("batch_number")) in {odsync.norm(b) for b in batches}:
                return "exists"

        def v(k):
            x = r.get(k)
            if x is None:
                return ""
            return f"{float(x):g}" if isinstance(x, (float, Decimal)) else str(x)
        value = {k: v(k) for k in _INWARD_POS}
        value["file"] = fn
        value["timestamp"] = odsync.iso_z(r.get("created_at"))
        row_vals = [value.get(k, "") for k in _INWARD_POS]
        cols = await graph.table_columns(token, item_id, INWARD_TABLE)
        if cols:
            row_vals = (row_vals + [""] * len(cols))[:len(cols)]
        await graph.add_table_row(token, item_id, INWARD_TABLE, row_vals)
    if excel:
        await st.run("Excel log (Inward_Log.xlsx)", _xlsx())
    return await odsync.record(st)


@router.post("/submit")
async def coil_submit(body: CoilIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    r = await _normalize(body)
    by = _emp_name(emp)
    cols = ",".join(_FIELDS)
    ph = ",".join(f"${i+1}" for i in range(len(_FIELDS)))
    vals = [r[f] for f in _FIELDS]
    new_id = await db.fetchval(
        f"INSERT INTO coil_inward ({cols},created_by) VALUES ({ph},${len(_FIELDS)+1}) RETURNING id",
        *vals, by)
    od = await od_sync(new_id)          # awaited: Vercel kills post-response work
    return {"ok": True, "id": new_id, "onedrive": od}


@router.post("/update")
async def coil_update(body: CoilIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.id:
        raise HTTPException(400, "id required")
    r = await _normalize(body)
    sets = ",".join(f"{f}=${i+1}" for i, f in enumerate(_FIELDS))
    vals = [r[f] for f in _FIELDS]
    await db.execute(
        f"UPDATE coil_inward SET {sets},updated_at=now() WHERE id=${len(_FIELDS)+1}",
        *vals, body.id)
    od = await od_sync(body.id, excel=False)   # refresh the PDF with the edits
    return {"ok": True, "id": body.id, "onedrive": od}


class DelIn(BaseModel):
    id: int


@router.post("/delete")
async def coil_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM coil_inward WHERE id=$1", body.id)
    return {"ok": True}


@router.get("/pdf")
async def coil_pdf(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT * FROM coil_inward WHERE id=$1", id)
    if not row:
        raise HTTPException(404, "not found")
    pdf = build_coil_pdf(dict(row))
    b = (dict(row).get("batch_number") or str(id)).replace(" ", "_")
    fn = f"Coil_Inward_{b}.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fn}"',
                             "Cache-Control": "no-store"})


@router.get("/pdf-legacy")
async def coil_pdf_legacy(i: int = Query(...), emp: dict = Depends(require_employee)):
    """Build a PDF from a historical InwardLog row (i = the sheet row index)."""
    legacy = await _read_inward_legacy()
    rec = next((r for r in legacy if r.get("_row") == i), None)
    if rec is None:
        raise HTTPException(404, "not found")
    rec = dict(rec)
    rec.setdefault("id", "")
    pdf = build_coil_pdf(rec)
    b = (rec.get("batch_number") or str(i)).replace(" ", "_")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Coil_Inward_{b}.pdf"',
                             "Cache-Control": "no-store"})
