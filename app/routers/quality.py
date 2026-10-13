"""CTL Quality Inspection API — full-parity with the legacy form. Stores the
complete collected payload to Neon (JSONB) for list/edit/PDF, and appends the
exact 315-column row to the EXISTING OneDrive workbook `Quality_Log.xlsx`
(table QualityLog) via Microsoft Graph — same contract as the Node app."""
import asyncio
import base64
import json
import re
from datetime import date, datetime
from typing import Optional, Any
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_quality_pdf

router = APIRouter(prefix="/quality", tags=["quality"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False

QUALITY_XLSX = f"{ONEDRIVE_BASE}/Quality/Quality_Log.xlsx"
QUALITY_TABLE = "QualityLog"

# Machine Setting Parameters for CTL — key, label (order must match the workbook)
MP_SPEC = [
    ("coil_car_pressure", "Coil Car Monitor Pressure"),
    ("coil_center_align", "Coil Center Alignment"),
    ("bound_spreader_roller", "Bound Spreader Roller"),
    ("rack_angle", "Rack Angle"),
    ("leveller_entry_gap", "Leveller Entry Gap"),
    ("leveller_exit_gap", "Leveller Exit Gap"),
    ("blade_gap_clearance", "Blade Gap Clearance"),
]


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS quality_inspection (
            id SERIAL PRIMARY KEY, log_date DATE, time TEXT,
            customer_name TEXT, coil_number TEXT, batch_number TEXT,
            grade TEXT, make TEXT, machine_name TEXT, inspector TEXT,
            sheets_measured INTEGER, oot_count INTEGER, rejection_flag TEXT,
            payload JSONB, file_name TEXT,
            created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ)
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_quality_date ON quality_inspection (log_date)")
    _ensured = True


# ---------- Excel row contract (byte-for-byte with legacy appendExcelRow) ----------
def _s(v):
    return "" if v is None else str(v)


def _mp_adj_cell(a: dict) -> str:
    v = _s(a.get("value")).strip()
    t = _s(a.get("time")).strip()
    if v and t:
        return f"{v} @ {t}"
    return v or (f"@ {t}" if t else "")


def _mp_progression(rec: Optional[dict]) -> str:
    if not rec:
        return ""
    init = _s(rec.get("initial")).strip()
    adjs = [a for a in (rec.get("adjustments") or [])
            if a and (_s(a.get("value")).strip() or _s(a.get("time")).strip())]
    parts = []
    if init:
        parts.append("Set: " + init)
    for i, a in enumerate(adjs):
        parts.append(f"Adj{i+1}: " + _mp_adj_cell(a))
    return "  |  ".join(parts)


def quality_headers() -> list[str]:
    h = ["File Name", "Timestamp", "Customer Name", "Date", "Time", "Coil Number",
         "Batch Number", "Make", "Coil Thickness", "Coil Grade", "Coil Width", "Coil Weight",
         "First Bit", "Last Bit", "Defective", "Balance Wt", "Coil Verified", "Blade Clearance",
         "Operator", "Machine No", "Inspector", "Remarks", "Bur", "Cutting Finish", "Scalling",
         "Pit Marks", "Waviness", "Center Bow", "Cutting Bow", "Surface Defects"]
    for i in range(1, 21):
        h += [f"Size {i} Length", f"Size {i} Sheets", f"Size {i} Weight (T)"]
    for i in range(1, 31):
        h += [f"Sheet {i} No", f"Sheet {i} Thickness", f"Sheet {i} Width",
              f"Sheet {i} Length", f"Sheet {i} Diag 1", f"Sheet {i} Diag 2"]
    h.append("Rejection Flag")
    for i in range(1, 11):
        h += [f"Rej {i} Size", f"Rej {i} Qty", f"Rej {i} Types"]
    h += ["Lot Size", "Required Samples", "Tolerance Profile", "Cut Lengths",
          "Sheets Measured", "Out Of Tolerance Count", "Cut Groups"]
    h += ["MP: " + lbl for _k, lbl in MP_SPEC]
    return h


def quality_row(data: dict, file_name: str) -> list:
    pq = data.get("processed_qty") or {}

    def s(n):
        return pq.get(f"size_{n}") or {}

    sm = data.get("sheet_measurements") or []

    def m(i):
        return sm[i] if i < len(sm) else {}

    rj = data.get("rejections") or []

    def r(i):
        return rj[i] if i < len(rj) else {}

    mp_by_key = {}
    for p in (data.get("machine_params") or []):
        if p and p.get("key"):
            mp_by_key[p["key"]] = p

    row = [
        file_name, _s(data.get("timestamp")), _s(data.get("customer_name")),
        _s(data.get("date")), _s(data.get("time")), _s(data.get("coil_number")),
        _s(data.get("batch_number")), _s(data.get("make")), _s(data.get("coil_thickness")),
        _s(data.get("coil_grade")), _s(data.get("coil_width")), _s(data.get("coil_weight")),
        _s(data.get("first_bit")), _s(data.get("last_bit")), _s(data.get("defective")),
        _s(data.get("balance_wt")), _s(data.get("coil_verified")), _s(data.get("blade_clearance")),
        _s(data.get("operator")), _s(data.get("machine_name")), _s(data.get("inspector")),
        _s(data.get("remarks")), _s(data.get("bur")), _s(data.get("cutting_finish")),
        _s(data.get("scalling")), _s(data.get("pit_marks")), _s(data.get("waviness")),
        _s(data.get("center_bow")), _s(data.get("cutting_bow")), _s(data.get("surface_defects")),
    ]
    for i in range(1, 21):
        row += [_s(s(i).get("length")), _s(s(i).get("nos")), _s(s(i).get("weight_t"))]
    for i in range(30):
        mm = m(i)
        row += [_s(mm.get("sheet_no")), _s(mm.get("thickness")), _s(mm.get("width")),
                _s(mm.get("length")), _s(mm.get("d1")), _s(mm.get("d2"))]
    row.append(_s(data.get("rejection_flag")) or "No")
    for i in range(10):
        rr = r(i)
        types = rr.get("types") or []
        row += [_s(rr.get("size")), _s(rr.get("qty")), ", ".join(str(t) for t in types)]
    row += [_s(data.get("lot_size")), _s(data.get("required_samples")),
            _s(data.get("tolerance_profile")), _s(data.get("cut_lengths")),
            _s(data.get("sheets_measured")), _s(data.get("oot_count")),
            json.dumps(data.get("cut_groups") or [])]
    row += [_mp_progression(mp_by_key.get(k)) for k, _lbl in MP_SPEC]
    return row


# ---------- helpers ----------
def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


def _safe(s: str, n=40) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "", str(s or ""))[:n]


async def _upload_photos(token, batch: str, photos: list) -> list:
    out = []
    for idx, p in enumerate(photos or []):
        m = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", p or "", re.I | re.S)
        if not m:
            continue
        ext = "jpg" if m.group(1).lower().startswith("jp") else m.group(1).lower()
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            continue
        stamp = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        path = f"{ONEDRIVE_BASE}/Quality/Photos/{_safe(batch) or 'ctl'}_{idx+1}_{stamp}.{ext}"
        ct = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg", "jpeg") else "image/webp"
        try:
            await graph.upload_file(token, path, raw, ct)
            out.append(path)
        except Exception as e:
            print("[quality-photo]", e)
    return out


async def _write_excel(data: dict, file_name: str):
    """Append the 315-col row to the existing OneDrive QualityLog table."""
    try:
        token = await graph.get_token()
        item_id = await graph.get_item_id(token, QUALITY_XLSX)
        if not item_id:
            print("[quality-xlsx] workbook not found:", QUALITY_XLSX)
            return
        headers = quality_headers()
        await graph.ensure_table_columns(token, item_id, QUALITY_TABLE, headers)
        row = quality_row(data, file_name)
        # pad/truncate defensively to the header length
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        row = row[:len(headers)]
        await graph.add_table_row(token, item_id, QUALITY_TABLE, row)
    except Exception as e:
        print("[quality-xlsx]", e)


class QIn(BaseModel):
    id: Optional[int] = None
    data: dict


def _summ(data: dict):
    d = (data.get("date") or "")[:10]
    try:
        log_date = date.fromisoformat(d) if d else None
    except ValueError:
        log_date = None
    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0
    return {
        "log_date": log_date, "time": (data.get("time") or "")[:8],
        "customer_name": (data.get("customer_name") or "")[:120],
        "coil_number": (data.get("coil_number") or "")[:60],
        "batch_number": (data.get("batch_number") or "")[:60],
        "grade": (data.get("coil_grade") or "")[:40],
        "make": (data.get("make") or "")[:60],
        "machine_name": (data.get("machine_name") or "")[:40],
        "inspector": (data.get("inspector") or "")[:80],
        "sheets_measured": _int(data.get("sheets_measured")),
        "oot_count": _int(data.get("oot_count")),
        "rejection_flag": (data.get("rejection_flag") or "No")[:8],
    }


@router.get("/list")
async def q_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    rows = await db.fetch(
        "SELECT id,log_date,customer_name,coil_number,batch_number,grade,make,machine_name,"
        "inspector,sheets_measured,oot_count,rejection_flag,created_at FROM quality_inspection "
        "ORDER BY log_date DESC NULLS LAST, id DESC LIMIT 2000")
    return JSONResponse(jsonable_encoder([dict(r) for r in rows]),
                        headers={"Cache-Control": "no-store"})


@router.get("/record")
async def q_record(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT id, payload FROM quality_inspection WHERE id=$1", id)
    if not row:
        raise HTTPException(404, "not found")
    d = dict(row)
    payload = d.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    return JSONResponse({"id": d["id"], "payload": payload or {}},
                        headers={"Cache-Control": "no-store"})


@router.post("/submit")
async def q_submit(body: QIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    data = body.data or {}
    by = _emp_name(emp)
    s = _summ(data)
    file_name = f"CTL_{_safe(s['batch_number']) or 'rec'}_{s['log_date'] or ''}"
    photos = data.pop("photos", []) if isinstance(data.get("photos"), list) else []
    new_id = await db.fetchval(
        "INSERT INTO quality_inspection (log_date,time,customer_name,coil_number,batch_number,"
        "grade,make,machine_name,inspector,sheets_measured,oot_count,rejection_flag,payload,"
        "file_name,created_by) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15) RETURNING id",
        s["log_date"], s["time"], s["customer_name"], s["coil_number"], s["batch_number"],
        s["grade"], s["make"], s["machine_name"], s["inspector"], s["sheets_measured"],
        s["oot_count"], s["rejection_flag"], json.dumps(data), file_name, by)

    async def _bg():
        token = None
        try:
            if photos:
                token = await graph.get_token()
                paths = await _upload_photos(token, s["batch_number"], photos)
                if paths:
                    data["photo_paths"] = paths
        except Exception as e:
            print("[quality-photos-bg]", e)
        await _write_excel(data, file_name)
    asyncio.create_task(_bg())
    return {"ok": True, "id": new_id}


@router.post("/update")
async def q_update(body: QIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.id:
        raise HTTPException(400, "id required")
    data = body.data or {}
    data.pop("photos", None)
    s = _summ(data)
    await db.execute(
        "UPDATE quality_inspection SET log_date=$1,time=$2,customer_name=$3,coil_number=$4,"
        "batch_number=$5,grade=$6,make=$7,machine_name=$8,inspector=$9,sheets_measured=$10,"
        "oot_count=$11,rejection_flag=$12,payload=$13,updated_at=now() WHERE id=$14",
        s["log_date"], s["time"], s["customer_name"], s["coil_number"], s["batch_number"],
        s["grade"], s["make"], s["machine_name"], s["inspector"], s["sheets_measured"],
        s["oot_count"], s["rejection_flag"], json.dumps(data), body.id)
    return {"ok": True, "id": body.id}


class DelIn(BaseModel):
    id: int


@router.post("/delete")
async def q_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM quality_inspection WHERE id=$1", body.id)
    return {"ok": True}


@router.get("/pdf")
async def q_pdf(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT id, payload FROM quality_inspection WHERE id=$1", id)
    if not row:
        raise HTTPException(404, "not found")
    payload = dict(row).get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    pdf = build_quality_pdf(payload or {}, id, MP_SPEC)
    b = _safe((payload or {}).get("batch_number") or str(id))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="CTL_Quality_{b}.pdf"',
                             "Cache-Control": "no-store"})
