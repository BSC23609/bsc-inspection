"""Shearing Quality Inspection API — full-parity with the legacy form. Stores
the complete payload to Neon (JSONB) and appends the exact 351-column row to the
EXISTING OneDrive workbook `Shearing_Log.xlsx` (table ShearingLog) via Graph.
OneDrive writes are awaited inside the request (see app/odsync.py):
PDF -> Shearing/PDF/<batch>_(dd-mm-yyyy)_Shearing.pdf, photos -> Shearing/Photos/<file name>/,
row -> ShearingLog, reworks -> Shearing_Rework_Register.csv."""
import base64
import json
import re
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, graph, xlsxlog, odsync
from ..auth import require_employee
from ..config import ONEDRIVE_BASE
from ..pdfgen import build_shearing_pdf

from .. import config as _cfg
AGENT_TOKEN = getattr(_cfg, "AGENT_TOKEN", "BSC@123")

router = APIRouter(prefix="/shearing", tags=["shearing"])
IST = ZoneInfo("Asia/Kolkata")
_ensured = False


def _insp_filename(data: dict, created_at, batch) -> str:
    """Correct OneDrive file name for a shearing record: <coilno>_dd-mm-yyyy_Shearing
    (coil/batch number first, then the INSPECTION date the user entered, no
    parentheses). Falls back to the upload time only when the inspection date is
    missing/unparseable."""
    d = (data.get("date") or "")[:10]
    try:
        when = datetime.fromisoformat(d).replace(tzinfo=IST) if d else None
    except ValueError:
        when = None
    if when is None:
        when = created_at or datetime.now(timezone.utc)
    return f"{odsync.legacy_safe(batch)}_{odsync.ist_date_str(when)}_Shearing"

SHEAR_XLSX = f"{ONEDRIVE_BASE}/Shearing/Shearing_Log.xlsx"
SHEAR_TABLE = "ShearingLog"

MP_SPEC = [
    ("blade_clearance", "Blade Clearance"),
    ("blade_condition", "Blade Condition"),
    ("backgage_reading", "Backgage Reading"),
    ("main_pressure", "Main Pressure"),
    ("hold_down_pressure", "Hold Down Pressure"),
    ("rack_angle", "Rack Angle"),
]


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS shearing_inspection (
            id SERIAL PRIMARY KEY, log_date DATE,
            customer_name TEXT, batch_number TEXT, grade TEXT, make TEXT,
            process TEXT, qc_name TEXT, sheets_measured INTEGER,
            rejection_flag TEXT, has_reworks TEXT, payload JSONB, file_name TEXT,
            created_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ)
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_shearing_date ON shearing_inspection (log_date)")
    _ensured = True


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


def shearing_headers() -> list[str]:
    h = ["File", "Timestamp", "Customer", "Date", "Batch", "Grade", "Make", "Type",
         "Process", "Operator", "Input Size", "QC Name", "Burr", "Blade Clearance",
         "Cutting Finish", "Surface Condition", "Bow/Bend", "Taper Cutting",
         "Rejection Flag", "Remarks", "Overall Observation"]
    for i in range(1, 31):
        h += [f"Sheet {i} No", f"Sheet {i} Width 1", f"Sheet {i} Width 2",
              f"Sheet {i} Diag 1", f"Sheet {i} Diag 2", f"Sheet {i} Remarks"]
    for i in range(1, 21):
        h.append(f"Output Size {i}")
    for i in range(1, 21):
        h += [f"PQ Size {i} Length", f"PQ Size {i} Sheets", f"PQ Size {i} Weight"]
    for i in range(1, 11):
        h += [f"Rej {i} Size", f"Rej {i} Qty"]
    for i in range(1, 11):
        h.append(f"Rej {i} Types")
    h += ["Lot Size", "Required Samples", "Tolerance Profile"]
    for i in range(1, 31):
        h.append(f"Sheet {i} Thickness")
    h.append("Coil Thickness")
    h += ["MP: " + lbl for _k, lbl in MP_SPEC]
    return h


def shearing_row(data: dict, file_name: str) -> list:
    sh = data.get("measurements") or []

    def sr(i):
        return sh[i] if i < len(sh) else {}

    os_ = data.get("output_sizes") or []
    spq = data.get("processed_qty") or {}

    def spqGet(n):
        return spq.get(f"size_{n}") or {}

    rj = data.get("rejections") or []

    def rjGet(i):
        return rj[i] if i < len(rj) else {}

    mp_by = {p.get("key"): p for p in (data.get("machine_params") or []) if p and p.get("key")}

    row = [
        file_name, _s(data.get("timestamp")), _s(data.get("customer_name")), _s(data.get("date")),
        _s(data.get("batch_number")), _s(data.get("grade")), _s(data.get("make")), _s(data.get("type")),
        _s(data.get("process")), _s(data.get("operator")), _s(data.get("input_size")), _s(data.get("qc_name")),
        _s(data.get("burr")), _s(data.get("blade_clearance")), _s(data.get("cutting_finish")),
        _s(data.get("surface_condition")), _s(data.get("bow_bend")), _s(data.get("taper_cutting")),
        _s(data.get("rejection_flag")) or "No", _s(data.get("remarks")), _s(data.get("overall_observation")),
    ]
    for i in range(30):
        m = sr(i)
        row += [_s(m.get("sheet_no")), _s(m.get("width1")), _s(m.get("width2")),
                _s(m.get("diag1")), _s(m.get("diag2")), _s(m.get("remarks"))]
    for i in range(20):
        row.append(_s(os_[i]) if i < len(os_) else "")
    for i in range(1, 21):
        s = spqGet(i)
        row += [_s(s.get("length")), _s(s.get("nos")), _s(s.get("weight_t"))]
    for i in range(10):
        r = rjGet(i)
        row += [_s(r.get("size")), _s(r.get("qty"))]
    for i in range(10):
        r = rjGet(i)
        row.append(", ".join(str(t) for t in (r.get("types") or [])))
    row += [_s(data.get("lot_size")), _s(data.get("required_samples")), _s(data.get("tolerance_profile"))]
    for i in range(30):
        row.append(_s(sr(i).get("thickness")))
    row.append(_s(data.get("coil_thickness")))
    row += [_mp_progression(mp_by.get(k)) for k, _lbl in MP_SPEC]
    return row


def _emp_name(emp: dict) -> str:
    return emp.get("name") or emp.get("emp_no") or ""


def _safe(s: str, n=40) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "", str(s or ""))[:n]


REWORK_DEFECTS = {"RW-01": "Length variation", "RW-02": "Width variation",
                  "RW-03": "Diagonal variation / squareness", "RW-04": "Burr",
                  "RW-05": "Blade impression", "RW-06": "Edge crack / damaged edge",
                  "RW-07": "Camber", "RW-08": "Bow / flatness issue",
                  "RW-09": "Surface damage", "RW-10": "Other"}


async def _upload_photos(token, photo_folder: str, photos: list, start: int = 0) -> list:
    """Node layout: Shearing/Photos/<file name>/photo_<n>_image.<ext>. Raises if any fail."""
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
            print("[shearing-photo]", e)
    if failed:
        raise RuntimeError(f"{failed} of {len(photos)} photo(s) failed to upload")
    return out


async def _write_excel(token, data: dict, file_name: str, old_name: str = "", dedupe: bool = False):
    """Append the row to the existing OneDrive ShearingLog table (raises on failure)."""
    item_id = await graph.require_item_id(token, SHEAR_XLSX)
    headers = shearing_headers()
    if dedupe:
        names = await graph.column_values(token, item_id, SHEAR_TABLE, 0)
        if file_name in names or (old_name and old_name in names):
            return "exists"
        live = [h.strip().lower() for h in await graph.table_columns(token, item_id, SHEAR_TABLE)]

        def pos(name):
            return live.index(name.lower()) if name.lower() in live else headers.index(name)
        bat = await graph.column_values(token, item_id, SHEAR_TABLE, pos("Batch"))
        dts = await graph.column_values(token, item_id, SHEAR_TABLE, pos("Date"))
        have = {(odsync.norm(b), xlsxlog.date_part(d)) for b, d in zip(bat, dts)}
        if (odsync.norm(data.get("batch_number")), str(data.get("date") or "")[:10]) in have:
            return "exists"
    await graph.ensure_table_columns(token, item_id, SHEAR_TABLE, headers)
    actual = await graph.table_columns(token, item_id, SHEAR_TABLE)
    row = odsync.fit_row(actual, headers, shearing_row(data, file_name))
    try:
        await graph.add_table_row(token, item_id, SHEAR_TABLE, row)
    except RuntimeError as e:
        raise RuntimeError(f"{e} [table has {len(actual)} columns, sent {len(row)}]")


async def _write_reworks(token, data: dict, file_name: str):
    """Node appendShearingReworks: one line per rework in Shearing_Rework_Register.csv."""
    reworks = data.get("reworks") or []
    if str(data.get("has_reworks") or "").lower() != "yes" or not reworks:
        return "none"
    path = f"{ONEDRIVE_BASE}/Shearing/Shearing_Rework_Register.csv"
    ex = await graph.read_text(token, path) or (
        "Sl. No.,Date,Batch / Coil No.,Customer,Grade,Thickness (mm),Defect Code,Defect Description,"
        "Rework Qty,Required,Actual,Variation,Blade Gap,Reason,Details,Action Taken,QC Verify,"
        "Disposition,Remarks\n")
    marker = f"{data.get('date') or ''},{odsync.csv_cell(data.get('batch_number') or '')},"
    if marker in ex:
        return "exists"
    prior = len(ex.strip().split("\n")) - 1
    thick = data.get("coil_thickness") or data.get("thickness") or ""
    lines = ""
    for i, rw in enumerate(reworks):
        code = rw.get("defect_code") or ""
        lines += odsync.csv_line([prior + i + 1, data.get("date") or "", data.get("batch_number") or "",
                                  data.get("customer_name") or "", data.get("grade") or "", thick, code,
                                  rw.get("defect_desc") or REWORK_DEFECTS.get(code, ""),
                                  rw.get("rework_qty"), rw.get("required"), rw.get("actual"),
                                  rw.get("variation"), rw.get("blade_gap"), rw.get("reason"),
                                  rw.get("details"), rw.get("action_taken"), rw.get("qc_verification"),
                                  rw.get("disposition"), rw.get("remarks")])
    if not ex.endswith("\n"):
        ex += "\n"
    await graph.upload_file(token, path, (ex + lines).encode("utf-8"), "text/csv")


async def od_sync(rec_id: int, photos: list = None, excel: bool = True, dedupe: bool = False) -> dict:
    """Write one shearing inspection to OneDrive — PDF, photos, ShearingLog row, rework CSV."""
    await ensure_table()
    st = odsync.Steps("shearing", rec_id)
    row = await db.fetchrow("SELECT id, payload, file_name, created_at, batch_number, log_date "
                            "FROM shearing_inspection WHERE id=$1", rec_id)
    if not row:
        st.fail("record", "not found in database")
        return await odsync.record(st)
    r = dict(row)
    # Previously stored OneDrive file name — lets the Excel log find and update the
    # existing row when the name changes (date edit/rename) instead of duplicating it.
    # Must be defined: referenced below in the Excel step.
    old_name = r.get("file_name") or ""
    data = odsync.as_obj(r.get("payload"), {})
    if not data.get("timestamp"):
        data["timestamp"] = odsync.iso_z(r.get("created_at") or datetime.now(timezone.utc))
    # File name is always <coilno>_dd-mm-yyyy_Shearing built from the inspection date
    # the user entered (the "Date" shown in the app), NOT the upload timestamp.
    fn = _insp_filename(data, r.get("created_at"), data.get("batch_number"))
    # De-duplicate the filename: two inspections with the SAME coil + date build the same
    # name, so the 2nd PDF would OVERWRITE the 1st in OneDrive. Give the 2nd, 3rd... record
    # of the same (batch, date) a _2, _3 suffix — ordered by record id so each keeps its own
    # stable file and nothing gets clobbered.
    try:
        sibs = [x["id"] for x in await db.fetch(
            "SELECT id FROM shearing_inspection "
            "WHERE coalesce(batch_number,'') = coalesce($1,'') "
            "AND log_date IS NOT DISTINCT FROM $2 ORDER BY id, created_at",
            r.get("batch_number"), r.get("log_date"))]
        pos = sibs.index(rec_id) if rec_id in sibs else 0
        if pos > 0:
            fn = f"{fn}_{pos + 1}"
    except Exception:
        pass
    token = await st.run("OneDrive sign-in", graph.get_token())
    if token:
        if photos:
            have = list(data.get("photo_paths") or [])
            paths = await st.run("Photos", _upload_photos(token, f"{ONEDRIVE_BASE}/Shearing/Photos/{fn}", photos, start=len(have)))
            if paths:
                data["photo_paths"] = have + paths
        await st.run("PDF report", graph.upload_file(
            token, f"{ONEDRIVE_BASE}/Shearing/PDF/{fn}.pdf",
            build_shearing_pdf(data, rec_id, MP_SPEC), "application/pdf"))
        if excel:
            await st.run("Excel log (Shearing_Log.xlsx)", _write_excel(token, data, fn, old_name, dedupe))
            await st.run("Rework register CSV", _write_reworks(token, data, fn))
    await db.execute("UPDATE shearing_inspection SET file_name=$1, payload=$2 WHERE id=$3",
                     fn, json.dumps(data), rec_id)
    return await odsync.record(st)


class SIn(BaseModel):
    id: Optional[int] = None
    data: dict


def _summ(data: dict):
    d = (data.get("date") or "")[:10]
    try:
        log_date = date.fromisoformat(d) if d else None
    except ValueError:
        log_date = None
    return {
        "log_date": log_date,
        "customer_name": (data.get("customer_name") or "")[:120],
        "batch_number": (data.get("batch_number") or "")[:60],
        "grade": (data.get("grade") or "")[:40],
        "make": (data.get("make") or "")[:60],
        "process": (data.get("process") or "")[:60],
        "qc_name": (data.get("qc_name") or "")[:80],
        "sheets_measured": len(data.get("measurements") or []),
        "rejection_flag": (data.get("rejection_flag") or "No")[:8],
        "has_reworks": (data.get("has_reworks") or "No")[:8],
    }


def _slog_record(row, hmap):
    def c(*n):
        return xlsxlog.cell(row, hmap, *n)
    sheets = 0
    for i in range(1, 31):
        if c(f"Sheet {i} No"):
            sheets += 1
    return {
        "id": None, "_legacy": True,
        "log_date": xlsxlog.date_part(c("Date")),
        "customer_name": c("Customer", "Customer Name"),
        "batch_number": c("Batch", "Batch Number"),
        "grade": c("Grade"), "make": c("Make"), "process": c("Process"),
        "qc_name": c("QC Name"), "sheets_measured": sheets,
        "rejection_flag": c("Rejection Flag"), "has_reworks": "",
    }


def _slog_payload(row, hmap):
    def c(*n):
        return xlsxlog.cell(row, hmap, *n)
    meas = []
    for i in range(1, 31):
        sn = c(f"Sheet {i} No")
        if sn or c(f"Sheet {i} Width 1"):
            meas.append({"sheet_no": sn, "thickness": c(f"Sheet {i} Thickness"),
                         "width1": c(f"Sheet {i} Width 1"), "width2": c(f"Sheet {i} Width 2"),
                         "diag1": c(f"Sheet {i} Diag 1"), "diag2": c(f"Sheet {i} Diag 2"),
                         "remarks": c(f"Sheet {i} Remarks")})
    output_sizes = [c(f"Output Size {i}") for i in range(1, 21) if c(f"Output Size {i}")]
    pq = {}
    for i in range(1, 21):
        pq[f"size_{i}"] = {"length": c(f"PQ Size {i} Length"), "nos": c(f"PQ Size {i} Sheets"),
                           "weight_t": c(f"PQ Size {i} Weight")}
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
            for p in [x.strip() for x in prog.split("|")]:
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
        "date": xlsxlog.date_part(c("Date")), "customer_name": c("Customer", "Customer Name"),
        "batch_number": c("Batch", "Batch Number"), "grade": c("Grade"), "make": c("Make"),
        "type": c("Type"), "process": c("Process"), "operator": c("Operator"),
        "input_size": c("Input Size"), "qc_name": c("QC Name"), "burr": c("Burr"),
        "blade_clearance": c("Blade Clearance"), "cutting_finish": c("Cutting Finish"),
        "surface_condition": c("Surface Condition"), "bow_bend": c("Bow/Bend"),
        "taper_cutting": c("Taper Cutting"), "coil_thickness": c("Coil Thickness"),
        "remarks": c("Remarks"), "overall_observation": c("Overall Observation"),
        "rejection_flag": c("Rejection Flag") or "No", "lot_size": c("Lot Size"),
        "required_samples": c("Required Samples"), "tolerance_profile": c("Tolerance Profile"),
        "measurements": meas, "output_sizes": output_sizes, "processed_qty": pq,
        "rejections": rejections, "machine_params": mps, "has_reworks": "No", "reworks": [],
    }


async def _read_shearing_legacy(full=False):
    try:
        token = await graph.get_token()
        item_id = await graph.get_item_id(token, SHEAR_XLSX)
        if not item_id:
            return []
        cols, rows = await graph.read_table(token, item_id, SHEAR_TABLE)
        hmap = xlsxlog.header_map(cols)
        out = []
        for i, r in enumerate(rows):
            rec = (_slog_payload(r, hmap) if full else _slog_record(r, hmap))
            if full or rec["batch_number"] or rec["customer_name"]:
                rec["_row"] = i
                out.append(rec)
        out.reverse()
        return out
    except Exception as e:
        print("[shearing-legacy]", e)
        return []


def _skey(r):
    return (str(r.get("batch_number") or "").strip().lower(),
            str(r.get("customer_name") or "").strip().lower(),
            str(r.get("log_date") or "")[:10])


@router.get("/list")
async def s_list(emp: dict = Depends(require_employee)):
    await ensure_table()
    neon = [dict(r) for r in await db.fetch(
        "SELECT id,log_date,customer_name,batch_number,grade,make,process,qc_name,"
        "sheets_measured,rejection_flag,has_reworks,created_at FROM shearing_inspection "
        "ORDER BY log_date DESC NULLS LAST, id DESC LIMIT 2000")]
    seen = {_skey(r) for r in neon}
    legacy = await _read_shearing_legacy()
    merged = neon + [r for r in legacy if _skey(r) not in seen]
    merged.sort(key=lambda r: str(r.get("log_date") or ""), reverse=True)
    return JSONResponse(jsonable_encoder(merged), headers={"Cache-Control": "no-store"})


@router.get("/record")
async def s_record(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT id, payload FROM shearing_inspection WHERE id=$1", id)
    if not row:
        raise HTTPException(404, "not found")
    payload = dict(row).get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    return JSONResponse({"id": id, "payload": payload or {}}, headers={"Cache-Control": "no-store"})


@router.post("/submit")
async def s_submit(body: SIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    data = body.data or {}
    by = _emp_name(emp)
    s = _summ(data)
    file_name = f"SH_{_safe(s['batch_number']) or 'rec'}_{s['log_date'] or ''}"
    photos = data.pop("photos", []) if isinstance(data.get("photos"), list) else []
    new_id = await db.fetchval(
        "INSERT INTO shearing_inspection (log_date,customer_name,batch_number,grade,make,"
        "process,qc_name,sheets_measured,rejection_flag,has_reworks,payload,file_name,created_by) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id",
        s["log_date"], s["customer_name"], s["batch_number"], s["grade"], s["make"],
        s["process"], s["qc_name"], s["sheets_measured"], s["rejection_flag"],
        s["has_reworks"], json.dumps(data), file_name, by)

    # The inspection is already saved in the DB above; a OneDrive/Graph failure must NOT
    # fail the save (it only means the PDF/Excel didn't sync yet). Report it, don't raise.
    try:
        od = await od_sync(new_id, photos=photos)
    except Exception as e:  # noqa: BLE001
        od = {"ok": False, "error": "saved to database; OneDrive sync failed: %s" % e}
    return {"ok": True, "id": new_id, "onedrive": od}


@router.post("/update")
async def s_update(body: SIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    if not body.id:
        raise HTTPException(400, "id required")
    data = body.data or {}
    photos = data.pop("photos", []) if isinstance(data.get("photos"), list) else []
    prev = await db.fetchrow("SELECT payload FROM shearing_inspection WHERE id=$1", body.id)
    if prev:   # the edit form doesn't carry photo_paths — keep the ones already saved
        old = odsync.as_obj(dict(prev).get("payload"), {}) or {}
        if old.get("photo_paths"):
            data["photo_paths"] = old["photo_paths"]
    s = _summ(data)
    await db.execute(
        "UPDATE shearing_inspection SET log_date=$1,customer_name=$2,batch_number=$3,grade=$4,"
        "make=$5,process=$6,qc_name=$7,sheets_measured=$8,rejection_flag=$9,has_reworks=$10,"
        "payload=$11,updated_at=now() WHERE id=$12",
        s["log_date"], s["customer_name"], s["batch_number"], s["grade"], s["make"],
        s["process"], s["qc_name"], s["sheets_measured"], s["rejection_flag"],
        s["has_reworks"], json.dumps(data), body.id)
    try:
        od = await od_sync(body.id, photos=photos, excel=False)   # new photos + refreshed PDF
    except Exception as e:  # noqa: BLE001
        od = {"ok": False, "error": "saved to database; OneDrive sync failed: %s" % e}
    return {"ok": True, "id": body.id, "onedrive": od}


class DelIn(BaseModel):
    id: int


@router.post("/delete")
async def s_delete(body: DelIn, emp: dict = Depends(require_employee)):
    await ensure_table()
    await db.execute("DELETE FROM shearing_inspection WHERE id=$1", body.id)
    return {"ok": True}


@router.get("/pdf")
async def s_pdf(id: int = Query(...), emp: dict = Depends(require_employee)):
    await ensure_table()
    row = await db.fetchrow("SELECT id, payload FROM shearing_inspection WHERE id=$1", id)
    if not row:
        raise HTTPException(404, "not found")
    payload = dict(row).get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = {}
    pdf = build_shearing_pdf(payload or {}, id, MP_SPEC)
    b = _safe((payload or {}).get("batch_number") or str(id))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Shearing_{b}.pdf"',
                             "Cache-Control": "no-store"})


@router.get("/pdf-legacy")
async def s_pdf_legacy(i: int = Query(...), emp: dict = Depends(require_employee)):
    legacy = await _read_shearing_legacy(full=True)
    rec = next((r for r in legacy if r.get("_row") == i), None)
    if rec is None:
        raise HTTPException(404, "not found")
    pdf = build_shearing_pdf(rec, rec.get("batch_number") or i, MP_SPEC)
    b = _safe(rec.get("batch_number") or str(i))
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="Shearing_{b}.pdf"',
                             "Cache-Control": "no-store"})


@router.get("/fix-filenames")
async def s_fix_filenames(token: str = Query(...), apply: int = Query(0),
                          limit: int = Query(500)):
    """One-shot repair: rename existing OneDrive shearing PDFs so the date in the
    file name matches the INSPECTION date in the app (not the upload date).

    Open /shearing/fix-filenames?token=<AGENT_TOKEN>           -> dry run (lists changes)
         /shearing/fix-filenames?token=<AGENT_TOKEN>&apply=1   -> actually rename
    """
    if token != AGENT_TOKEN:
        raise HTTPException(403, "bad token")
    await ensure_table()
    rows = await db.fetch(
        "SELECT id, payload, file_name, created_at, batch_number, log_date "
        "FROM shearing_inspection ORDER BY id DESC LIMIT $1", limit)
    tok = await graph.get_token()
    # Index existing PDFs by name so we can find item ids to delete old copies.
    children = await graph.list_children(tok, f"{ONEDRIVE_BASE}/Shearing/PDF") if tok else []
    by_name = {c["name"]: c["id"] for c in children if not c.get("is_folder")}

    # Pass 1 — assign a UNIQUE final name to every record. If the same batch was
    # inspected more than once on the same day, both map to the same base name;
    # keep the earliest (lowest id) on the clean name and give the others a _2/_3
    # suffix so no PDF is overwritten. Deterministic: order by id ascending.
    recs = []
    for row in rows:
        r = dict(row)
        data = odsync.as_obj(r.get("payload"), {})
        batch = data.get("batch_number") or r.get("batch_number")
        base = _insp_filename(data, r.get("created_at"), batch)
        recs.append({"id": r["id"], "data": data, "batch": batch,
                     "insp_date": str(r.get("log_date") or (data.get("date") or "")[:10]),
                     "cur": r.get("file_name") or "", "base": base})
    taken = {}      # base name -> count already assigned
    final = {}      # id -> unique final name
    collisions = []
    for rec in sorted(recs, key=lambda x: x["id"]):
        b = rec["base"]
        n = taken.get(b, 0)
        name = b if n == 0 else f"{b}_{n + 1}"
        taken[b] = n + 1
        final[rec["id"]] = name
        if n > 0:
            collisions.append({"id": rec["id"], "batch": rec["batch"],
                               "insp_date": rec["insp_date"], "name": name})

    # Pass 2 — rename the records whose current name isn't the final one.
    planned, changed, errors = [], [], []
    for rec in recs:                       # original DESC order for the report
        correct = final[rec["id"]]
        cur = rec["cur"]
        if cur == correct:
            continue
        item = {"id": rec["id"], "batch": rec["batch"], "insp_date": rec["insp_date"],
                "old_name": cur, "new_name": correct}
        planned.append(item)
        if not apply:
            continue
        if not tok:
            errors.append({**item, "error": "no OneDrive token"}); continue
        try:
            # Regenerate the PDF from THIS record and upload under its unique name,
            # then remove the stale copy (only if nothing else now uses that name).
            await graph.upload_file(
                tok, f"{ONEDRIVE_BASE}/Shearing/PDF/{correct}.pdf",
                build_shearing_pdf(rec["data"], rec["id"], MP_SPEC), "application/pdf")
            old_id = by_name.get(f"{cur}.pdf") if cur else None
            if old_id and cur != correct and cur not in final.values():
                await graph.delete_item(tok, old_id)
            await db.execute("UPDATE shearing_inspection SET file_name=$1 WHERE id=$2",
                             correct, rec["id"])
            changed.append(item)
        except Exception as e:
            errors.append({**item, "error": str(e)[:200]})
    return {"ok": True, "apply": bool(apply), "scanned": len(rows),
            "mismatched": len(planned), "renamed": len(changed),
            "duplicates_suffixed": collisions,
            "planned": planned, "changed": changed, "errors": errors}


@router.get("/fix-dates")
async def s_fix_dates(token: str = Query(...), apply: int = Query(0),
                      limit: int = Query(500)):
    """Stamp each shearing PDF's OneDrive date (Modified + Created) with its
    INSPECTION date, so the folder can be sorted date-wise (current day on top)
    independent of the coil-first file name. Sort OneDrive by "Modified" descending.

    /shearing/fix-dates?token=<AGENT_TOKEN>          -> dry run (lists what it would set)
    /shearing/fix-dates?token=<AGENT_TOKEN>&apply=1  -> actually stamp the dates
    """
    if token != AGENT_TOKEN:
        raise HTTPException(403, "bad token")
    await ensure_table()
    rows = await db.fetch(
        "SELECT id, payload, file_name, created_at, batch_number, log_date "
        "FROM shearing_inspection ORDER BY id DESC LIMIT $1", limit)
    tok = await graph.get_token()
    children = await graph.list_children(tok, f"{ONEDRIVE_BASE}/Shearing/PDF") if tok else []
    by_name = {c["name"]: c["id"] for c in children if not c.get("is_folder")}
    planned, changed, errors = [], [], []
    for row in rows:
        r = dict(row)
        data = odsync.as_obj(r.get("payload"), {})
        batch = data.get("batch_number") or r.get("batch_number")
        fn = r.get("file_name") or _insp_filename(data, r.get("created_at"), batch)
        d = (data.get("date") or str(r.get("log_date") or "") or "")[:10]
        if not d:
            continue
        try:
            dt = datetime.fromisoformat(d)
        except ValueError:
            continue
        # Reuse the upload time-of-day so same-day files keep a stable order.
        ca = r.get("created_at")
        if ca is not None:
            cai = ca.astimezone(IST)
            dt = dt.replace(hour=cai.hour, minute=cai.minute, second=cai.second)
        else:
            dt = dt.replace(hour=12)
        iso = dt.replace(tzinfo=IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        item = {"id": r["id"], "batch": batch, "file": fn,
                "insp_date": d, "stamp_utc": iso}
        item_id = by_name.get(f"{fn}.pdf")
        if not item_id:
            errors.append({**item, "error": "pdf not found in OneDrive"})
            continue
        planned.append(item)
        if not apply:
            continue
        if not tok:
            errors.append({**item, "error": "no OneDrive token"}); continue
        try:
            if await graph.set_file_dates(tok, item_id, iso, iso):
                changed.append(item)
            else:
                errors.append({**item, "error": "patch failed"})
        except Exception as e:
            errors.append({**item, "error": str(e)[:200]})
    return {"ok": True, "apply": bool(apply), "scanned": len(rows),
            "to_stamp": len(planned), "stamped": len(changed),
            "planned": planned, "changed": changed, "errors": errors}


def _dup_sig(data: dict) -> str:
    """Content signature of a shearing record, ignoring volatile/non-content keys. Two
    records with the same signature are identical copies (e.g. the same inspection saved
    twice by the old 'Save failed' retry), NOT two different inspections."""
    d = {k: v for k, v in (data or {}).items()
         if k not in ("timestamp", "photo_paths", "photos", "file_name")}
    return json.dumps(d, sort_keys=True, default=str)


@router.get("/dedupe")
async def s_dedupe(token: str = Query(...), apply: int = Query(0), limit: int = Query(2000),
                   mode: str = Query("content"), date: str = Query("")):
    """Remove DUPLICATE shearing entries (and their OneDrive PDFs).

    mode=content (default): only rows that are EXACT copies (same batch + date + identical
      content) are duplicates; the earliest is kept. Genuinely different inspections are
      never touched.
    mode=batchdate: treats every record of the same coil on the same day as a duplicate;
      keeps the MOST COMPLETE one (most sheet measurements, then lowest id) and deletes the
      rest. Use with date=YYYY-MM-DD to limit it to one day. (More aggressive — ALWAYS dry
      run first and check the to_delete list.)

    date=YYYY-MM-DD limits the whole operation to that inspection date.
    A PDF kept by a surviving row is never deleted.

    Open /shearing/dedupe?token=<AGENT_TOKEN>[&mode=batchdate][&date=2026-11-04]  -> dry run
         ...&apply=1                                                              -> delete
    """
    if token != AGENT_TOKEN:
        raise HTTPException(403, "bad token")
    await ensure_table()
    rows = await db.fetch(
        "SELECT id, payload, file_name, batch_number, log_date, created_at "
        "FROM shearing_inspection ORDER BY id LIMIT $1", limit)

    def _nmeas(r):
        return len(odsync.as_obj(r.get("payload"), {}).get("measurements") or [])

    groups = {}
    for row in rows:
        r = dict(row)
        ld = str(r.get("log_date") or "")
        if date and ld != date:
            continue
        if mode == "batchdate":
            key = (r.get("batch_number") or "", ld)
        else:
            key = (r.get("batch_number") or "", ld, _dup_sig(odsync.as_obj(r.get("payload"), {})))
        groups.setdefault(key, []).append(r)

    kept_names, to_delete = set(), []
    for key, lst in groups.items():
        if mode == "batchdate":
            keeper = sorted(lst, key=lambda r: (-_nmeas(r), r["id"]))[0]
        else:
            keeper = lst[0]
        kept_names.add(keeper.get("file_name") or "")
        for r in lst:
            if r["id"] == keeper["id"]:
                continue
            to_delete.append({"id": r["id"], "batch": r.get("batch_number"),
                              "date": str(r.get("log_date") or ""),
                              "file_name": r.get("file_name") or "",
                              "keep_id": keeper["id"]})
    result = {"ok": True, "apply": bool(apply), "mode": mode, "date": date or None,
              "scanned": len(rows),
              "duplicate_groups": sum(1 for lst in groups.values() if len(lst) > 1),
              "to_delete": to_delete, "deleted": [], "pdf_removed": [], "errors": []}
    if not apply or not to_delete:
        return result

    tok = await graph.get_token()
    by_name = {}
    if tok:
        for c in await graph.list_children(tok, f"{ONEDRIVE_BASE}/Shearing/PDF"):
            if not c.get("is_folder"):
                by_name[c["name"]] = c["id"]
    for d in to_delete:
        try:
            await db.execute("DELETE FROM shearing_inspection WHERE id=$1", d["id"])
            result["deleted"].append(d["id"])
            fn = d["file_name"]
            # only delete a PDF that no surviving row keeps
            if fn and fn not in kept_names and tok:
                item_id = by_name.get(f"{fn}.pdf")
                if item_id and await graph.delete_item(tok, item_id):
                    result["pdf_removed"].append(f"{fn}.pdf")
        except Exception as e:
            result["errors"].append({"id": d["id"], "error": str(e)[:200]})
    return result


@router.get("/resync")
async def s_resync(token: str = Query(...), id: int = Query(...)):
    """Regenerate ONE shearing record's PDF (+ photos/Excel) and upload it to OneDrive
    under its correct, de-duplicated file name. Use when a record's PDF is missing from
    OneDrive (e.g. it was overwritten before the de-dup fix).

    /shearing/resync?token=<AGENT_TOKEN>&id=<record id>
    """
    if token != AGENT_TOKEN:
        raise HTTPException(403, "bad token")
    row = await db.fetchrow("SELECT id, file_name FROM shearing_inspection WHERE id=$1", int(id))
    if not row:
        raise HTTPException(404, "record not found")
    try:
        od = await od_sync(int(id))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"resync failed: {e}")
    r = await db.fetchrow("SELECT file_name FROM shearing_inspection WHERE id=$1", int(id))
    return {"ok": True, "id": int(id), "file_name": (dict(r).get("file_name") if r else None),
            "onedrive": od}


@router.get("/resync")
async def s_resync(token: str = Query(...), id: int = Query(0),
                   all: int = Query(0), excel: int = Query(0), limit: int = Query(2000)):
    """Regenerate a shearing record's OneDrive PDF from its OWN data and write it under its
    correct, de-duplicated name. Use this to recover a record whose PDF is missing or holds
    another record's content (e.g. an old same-coil+date overwrite).

    /shearing/resync?token=<AGENT_TOKEN>&id=55     -> regenerate one record's PDF
    /shearing/resync?token=<AGENT_TOKEN>&all=1     -> regenerate every record's PDF (slower)
    Add &excel=1 to also refresh the Excel log / rework CSV rows.
    """
    if token != AGENT_TOKEN:
        raise HTTPException(403, "bad token")
    await ensure_table()
    if id:
        ids = [id]
    elif all:
        ids = [r["id"] for r in await db.fetch(
            "SELECT id FROM shearing_inspection ORDER BY id LIMIT $1", limit)]
    else:
        raise HTTPException(400, "pass ?id=<record id> or ?all=1")
    done, errors = [], []
    for i in ids:
        try:
            await od_sync(i, excel=bool(excel))
            done.append(i)
        except Exception as e:  # noqa: BLE001
            errors.append({"id": i, "error": str(e)[:200]})
    return {"ok": True, "resynced": len(done), "done": done, "errors": errors}
