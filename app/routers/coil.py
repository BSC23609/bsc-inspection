"""Coil Inward Inspection API — mother-coil arrival checks. Stores to Neon
(`coil_inward`), appends a CSV log to OneDrive, uploads coil/sticker photos and
renders a single-record PDF with ReportLab. Mirrors the DG module's shape."""
import asyncio
import base64
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
from ..pdfgen import build_coil_pdf

router = APIRouter(prefix="/coil", tags=["coil"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

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
    """Upload a base64 data URL to OneDrive and return its folder path."""
    m = _DATAURL.match(data_url or "")
    if not m:
        return ""
    ext = "jpg" if m.group(1).lower().startswith("jp") else m.group(1).lower()
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return ""
    stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "", (batch or "coil"))[:24] or "coil"
    path = f"{ONEDRIVE_BASE}/Inward/photos/{safe}_{kind}_{stamp}.{ext}"
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


async def _append_csv(row: dict, by: str):
    try:
        token = await graph.get_token()
        path = f"{ONEDRIVE_BASE}/Inward/Inward_Log.csv"
        ex = await graph.read_text(token, path) or (
            "Date,Vehicle No,Batch No,Make,Grade,Width (mm),Thickness (mm),Coil Weight (T),"
            "Coil ID (mm),Actual Thickness,Actual Width,ID Sticker,Edge Inner,Edge Outer,"
            "Scratch,Strapping,Rust,Other Damages,Wheels India,Inspected By,Remarks,"
            "Coil Photo,Sticker Photo,Entered By,Entered At\n")

        def c(v):
            v = "" if v is None else str(v)
            return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n') else v

        line = ",".join(c(x) for x in [
            row["log_date"], row["vehicle_number"], row["batch_number"], row["make_of_coil"],
            row["grade"], row["width"], row["thickness"], row["coil_weight"], row["coil_id"],
            row["actual_thickness"], row["actual_width"], row["id_sticker"], row["edge_inner"],
            row["edge_outer"], row["scratch"], row["strapping"], row["rust"], row["other_damages"],
            row["wheels_india"], row["inspected_by"], row["remarks"], row["coil_photo"],
            row["sticker_photo"], by, datetime.now(IST).strftime("%d-%b-%Y %H:%M")]) + "\n"
        await graph.upload_file(token, path, (ex + line).encode("utf-8"), "text/csv")
    except Exception as e:
        print("[coil-csv]", e)


@router.get("/list")
async def coil_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    cols = "id," + ",".join(_FIELDS) + ",created_by,created_at"
    rows = await db.fetch(
        f"SELECT {cols} FROM coil_inward ORDER BY log_date DESC NULLS LAST, id DESC LIMIT 2000")
    return JSONResponse(jsonable_encoder([dict(r) for r in rows]),
                        headers={"Cache-Control": "no-store"})


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
    asyncio.create_task(_append_csv(r, by))
    return {"ok": True, "id": new_id}


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
    return {"ok": True, "id": body.id}


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
