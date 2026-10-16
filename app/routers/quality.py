"""CTL Quality Inspection API — full-parity with the legacy form. Stores the
complete collected payload to Neon (JSONB) for list/edit/PDF, and appends the
exact 315-column row to the EXISTING OneDrive workbook `Quality_Log.xlsx`
(table QualityLog) via Microsoft Graph — same contract as the Node app.
OneDrive writes are awaited inside the request (see app/odsync.py):
PDF -> Quality/PDF/CTL-n/<batch>_(dd-mm-yyyy)_CTL_Inspection.pdf,
photos -> Quality/Photos/CTL-n/<file name>/, row -> QualityLog,
cut-length sheets -> Quality_Measurements_Log.csv."""
import base64
import json
import re
from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph, xlsxlog, odsync
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


def _machine_sub(machine) -> str:
    m = re.sub(r"\s+", "", str(machine or "")).upper()
    return "CTL-1" if m in ("CTL-1", "CTL1") else "CTL-2" if m in ("CTL-2", "CTL2") else ""


async def _upload_photos(token, photo_folder: str, photos: list, start: int = 0) -> list:
    """Node layout: <Photos[/CTL-n]>/<file name>/photo_<n>_image.<ext>. Raises if any fail."""
    out, failed = [], 0
    for idx, p in enumerate(photos or []):
        # the form sends {name, data, type}; accept a bare data: URL too
        name = (p.get("name") if isinstance(p, dict) else "") or "image.jpg"
        if isinstance(p, dict):
            p = p.get("data") or ""
        m = re.match(r"^data:image/(png|jpe?g|webp);base64,(.+)$", p or "", re.I | re.S)
        if not m:
            continue
        ext = "jpg" if m.group(1).lower().startswith("jp") else m.group(1).lower()
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            failed += 1
            continue
        base = re.sub(r"[^a-zA-Z0-9.\-_]", "_", name.rsplit(".", 1)[0])[:60] or "image"
        path = f"{photo_folder}/photo_{start+idx+1}_{base}.{ext}"
        ct = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg", "jpeg") else "image/webp"
        try:
            await graph.upload_file(token, path, raw, ct)
            out.append(path)
        except Exception as e:
            failed += 1
            print("[quality-photo]", e)
    if failed:
        raise RuntimeError(f"{failed} of {len(photos)} photo(s) failed to upload")
    return out


async def _write_excel(token, data: dict, file_name: str, old_name: str = "", dedupe: bool = False):
    """Append the 315-col row to the existing OneDrive QualityLog table (raises on failure)."""
    item_id = await graph.require_item_id(token, QUALITY_XLSX)
    headers = quality_headers()
    if dedupe:
        names = await graph.column_values(token, item_id, QUALITY_TABLE, 0)
        if file_name in names or (old_name and old_name in names):
            return "exists"
        bat = await graph.column_values(token, item_id, QUALITY_TABLE, headers.index("Batch Number"))
        dts = await graph.column_values(token, item_id, QUALITY_TABLE, headers.index("Date"))
        have = {(odsync.norm(b), xlsxlog.date_part(d)) for b, d in zip(bat, dts)}
        if (odsync.norm(data.get("batch_number")), str(data.get("date") or "")[:10]) in have:
            return "exists"
    await graph.ensure_table_columns(token, item_id, QUALITY_TABLE, headers)
    row = quality_row(data, file_name)
    row = (row + [""] * len(headers))[:len(headers)]
    await graph.add_table_row(token, item_id, QUALITY_TABLE, row)


async def _write_meas_csv(token, data: dict, file_name: str, when):
    """Node appendMeasCsv: one line per measured sheet in Quality_Measurements_Log.csv."""
    groups = [g for g in (data.get("cut_groups") or []) if g and (g.get("sheets") or [])]
    if not groups:
        return "none"
    path = f"{ONEDRIVE_BASE}/Quality/Quality_Measurements_Log.csv"
    ex = await graph.read_text(token, path) or (
        "Timestamp,File Name,Cut Length,Sheet No,Thickness,Width,Length,Diag 1,Diag 2,Out Of Tolerance\n")
    if f",{odsync.csv_cell(file_name)}," in ex:
        return "exists"
    ts = odsync.iso_z(when)
    lines = "".join(odsync.csv_line([ts, file_name, g.get("cut_length"), sh.get("sheet_no"),
                                     sh.get("thickness"), sh.get("width"), sh.get("length"),
                                     sh.get("d1"), sh.get("d2"), "YES" if sh.get("out_of_tol") else ""])
                    for g in groups for sh in (g.get("sheets") or []))
    if not ex.endswith("\n"):
        ex += "\n"
    await graph.upload_file(token, path, (ex + lines).encode("utf-8"), "text/csv")


async def od_sync(rec_id: int, photos: list = None, excel: bool = True, dedupe: bool = False) -> dict:
    """Write one CTL inspection to OneDrive — PDF, photos, QualityLog row, measurements CSV."""
    await ensure_table()
    st = odsync.Steps("quality", rec_id)
    row = await db.fetchrow("SELECT id, payload, file_name, created_at FROM quality_inspection WHERE id=$1", rec_id)
    if not row:
        st.fail("record", "not found in database")
        return await odsync.record(st)
    r = dict(row)
    data = odsync.as_obj(r.get("payload"), {})
    old_name = r.get("file_name") or ""
    fn = old_name if "_(" in old_name else odsync.legacy_file_name(
        data.get("batch_number"), r.get("created_at"), "CTL_Inspection")
    sub = _machine_sub(data.get("machine_name"))
    pdf_folder = f"{ONEDRIVE_BASE}/Quality/PDF" + (f"/{sub}" if sub else "")
    photo_folder = f"{ONEDRIVE_BASE}/Quality/Photos" + (f"/{sub}" if sub else "") + f"/{fn}"
    token = await st.run("OneDrive sign-in", graph.get_token())
    if token:
        if photos:
            have = list(data.get("photo_paths") or [])
            paths = await st.run("Photos", _upload_photos(token, photo_folder, photos, start=len(have)))
            if paths:
                data["photo_paths"] = have + paths
        await st.run("PDF report", graph.upload_file(
            token, f"{pdf_folder}/{fn}.pdf", build_quality_pdf(data, rec_id, MP_SPEC), "application/pdf"))
        if excel:
            await st.run("Excel log (Quality_Log.xlsx)", _write_excel(token, data, fn, old_name, dedupe))
            await st.run("Measurements CSV", _write_meas_csv(token, data, fn, r.get("created_at")))
    await db.execute("UPDATE quality_inspection SET file_name=$1, payload=$2 WHERE id=$3",
                     fn, json.dumps(data), rec_id)
    return await odsync.record(st)


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


def _qlog_record(row, hmap):
    def c(*n):
        return xlsxlog.cell(row, hmap, *n)
    return {
        "id": None, "_legacy": True,
        "log_date": xlsxlog.date_part(c("Date")),
        "customer_name": c("Customer Name", "Customer"),
        "coil_number": c("Coil Number"),
        "batch_number": c("Batch Number", "Batch"),
        "grade": c("Coil Grade", "Grade"),
        "make": c("Make"),
        "machine_name": c("Machine No", "Machine Name"),
        "inspector": c("Inspector"),
        "sheets_measured": c("Sheets Measured"),
        "oot_count": c("Out Of Tolerance Count"),
        "rejection_flag": c("Rejection Flag"),
    }


def _qlog_payload(row, hmap):
    """Reconstruct a Quality payload from an Excel row (for the historical PDF)."""
    import json as _json

    def c(*n):
        return xlsxlog.cell(row, hmap, *n)
    pq = {}
    for i in range(1, 21):
        pq[f"size_{i}"] = {"length": c(f"Size {i} Length"), "nos": c(f"Size {i} Sheets"),
                           "weight_t": c(f"Size {i} Weight (T)")}
    try:
        cut_groups = _json.loads(c("Cut Groups") or "[]")
    except Exception:
        cut_groups = []
    rejections = []
    for i in range(1, 11):
        sz, qt, ty = c(f"Rej {i} Size"), c(f"Rej {i} Qty"), c(f"Rej {i} Types")
        if sz or qt or ty:
            rejections.append({"size": sz, "qty": qt, "types": [t.strip() for t in ty.split(",") if t.strip()]})
    mps = []
    for key, label in MP_SPEC:
        prog = c(f"MP: {label}")
        initial, adjs = "", []
        if prog:
            parts = [p.strip() for p in prog.split("|")]
            for p in parts:
                if p.lower().startswith("set:"):
                    initial = p.split(":", 1)[1].strip()
                elif p.lower().startswith("adj"):
                    seg = p.split(":", 1)[1].strip() if ":" in p else p
                    val, tm = seg, ""
                    if "@" in seg:
                        val, tm = [x.strip() for x in seg.split("@", 1)]
                    adjs.append({"value": val, "time": tm})
        mps.append({"key": key, "label": label, "initial": initial, "adjustments": adjs})
    return {
        "date": xlsxlog.date_part(c("Date")), "time": c("Time"),
        "customer_name": c("Customer Name", "Customer"), "coil_number": c("Coil Number"),
        "batch_number": c("Batch Number", "Batch"), "make": c("Make"),
        "coil_thickness": c("Coil Thickness"), "coil_grade": c("Coil Grade", "Grade"),
        "coil_width": c("Coil Width"), "coil_weight": c("Coil Weight"),
        "first_bit": c("First Bit"), "last_bit": c("Last Bit"), "defective": c("Defective"),
        "balance_wt": c("Balance Wt"), "coil_verified": c("Coil Verified"),
        "blade_clearance": c("Blade Clearance"), "operator": c("Operator"),
        "machine_name": c("Machine No", "Machine Name"), "inspector": c("Inspector"),
        "remarks": c("Remarks"), "bur": c("Bur"), "cutting_finish": c("Cutting Finish"),
        "scalling": c("Scalling"), "pit_marks": c("Pit Marks"), "waviness": c("Waviness"),
        "center_bow": c("Center Bow"), "cutting_bow": c("Cutting Bow"),
        "surface_defects": c("Surface Defects"), "processed_qty": pq, "cut_groups": cut_groups,
        "rejection_flag": c("Rejection Flag") or "No", "rejections": rejections,
        "lot_size": c("Lot Size"), "required_samples": c("Required Samples"),
        "tolerance_profile": c("Tolerance Profile"), "cut_lengths": c("Cut Lengths"),
        "sheets_measured": c("Sheets Measured"), "oot_count": c("Out Of Tolerance Count"),
        "machine_params": mps,
    }


async def _read_quality_legacy(full=False):
    try:
        token = await graph.get_token()
        item_id = await graph.get_item_id(token, QUALITY_XLSX)
        if not item_id:
            return []
        cols, rows = await graph.read_table(token, item_id, QUALITY_TABLE)
        hmap = xlsxlog.header_map(cols)
        out = []
        for i, r in enumerate(rows):
            rec = (_qlog_payload(r, hmap) if full else _qlog_record(r, hmap))
            if full:
                rec["_row"] = i
                out.append(rec)
            elif rec["coil_number"] or rec["batch_number"]:
                rec["_row"] = i
                out.append(rec)
        out.reverse()
        return out
    except Exception as e:
        print("[quality-legacy]", e)
        return []


def _qkey(r):
    return (str(r.get("coil_number") or "").strip().lower(),
            str(r.get("batch_number") or "").strip().lower(),
            str(r.get("log_date") or "")[:10])


@router.get("/list")
async def q_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    neon = [dict(r) for r in await db.fetch(
        "SELECT id,log_date,customer_name,coil_number,batch_number,grade,make,machine_name,"
        "inspector,sheets_measured,oot_count,rejection_flag,created_at FROM quality_inspection "
        "ORDER BY log_date DESC NULLS LAST, id DESC LIMIT 2000")]
    seen = {_qkey(r) for r in neon}
    legacy = await _read_quality_legacy()
    merged = neon + [r for r in legacy if _qkey(r) not in seen]
    merged.sort(key=lambda r: str(r.get("log_date") or ""), reverse=True)
    return JSONResponse(jsonable_encoder(merged), headers={"Cache-Control": "no-store"})


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

    od = await od_sync(new_id, photos=photos)   # awaited: Vercel kills post-response work
    return {"ok": True, "id": new_id, "onedrive": od}


@router.post("/update")
async def q_update(body: QIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.id:
        raise HTTPException(400, "id required")
    data = body.data or {}
    photos = data.pop("photos", []) if isinstance(data.get("photos"), list) else []
    prev = await db.fetchrow("SELECT payload FROM quality_inspection WHERE id=$1", body.id)
    if prev:   # the edit form doesn't carry photo_paths — keep the ones already saved
        old = odsync.as_obj(dict(prev).get("payload"), {}) or {}
        if old.get("photo_paths"):
            data["photo_paths"] = old["photo_paths"]
    s = _summ(data)
    await db.execute(
        "UPDATE quality_inspection SET log_date=$1,time=$2,customer_name=$3,coil_number=$4,"
        "batch_number=$5,grade=$6,make=$7,machine_name=$8,inspector=$9,sheets_measured=$10,"
        "oot_count=$11,rejection_flag=$12,payload=$13,updated_at=now() WHERE id=$14",
        s["log_date"], s["time"], s["customer_name"], s["coil_number"], s["batch_number"],
        s["grade"], s["make"], s["machine_name"], s["inspector"], s["sheets_measured"],
        s["oot_count"], s["rejection_flag"], json.dumps(data), body.id)
    od = await od_sync(body.id, photos=photos, excel=False)   # new photos + refreshed PDF
    return {"ok": True, "id": body.id, "onedrive": od}


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


@router.get("/pdf-legacy")
async def q_pdf_legacy(i: int = Query(...), emp: dict = Depends(require_employee)):
    legacy = await _read_quality_legacy(full=True)
    rec = next((r for r in legacy if r.get("_row") == i), None)
    if rec is None:
        raise HTTPException(404, "not found")
    pdf = build_quality_pdf(rec, rec.get("coil_number") or rec.get("batch_number") or i, MP_SPEC)
    b = _safe(rec.get("batch_number") or str(i))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="CTL_Quality_{b}.pdf"',
                             "Cache-Control": "no-store"})
