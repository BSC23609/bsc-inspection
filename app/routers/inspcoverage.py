"""Inspection Coverage — reconcile what SAP says was received / produced / dispatched
against what the QMS actually inspected, so gaps (un-inspected material) are visible.

SAP side  : app/insp_data.py  (insp_inward / insp_production / insp_dispatch CSVs)
QMS side  : Neon tables coil_inward / quality_inspection / shearing_inspection /
            pdi_reports, PLUS the legacy OneDrive logs (inspections before the Neon
            tables existed) via each module's own legacy reader.

Match keys:
  Inward / CTL / Shearing  ->  coil number  (mill batch, normalised; see insp_data)
  Pre-dispatch             ->  Sales Order  (PDI item 'so' vs dispatched SO)

Admin or ?token=AGENT_TOKEN. Read-only; never writes to SAP or QMS.
"""
import asyncio
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db, graph, odsync
from .. import insp_data as idata
from ..auth import current_employee
from ..config import AGENT_TOKEN, SAP_COMPANY_DB, ONEDRIVE_BASE

router = APIRouter(prefix="/insp")
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_FROM = "2026-06-01"


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)))


async def _gate(request, token):
    emp = await current_employee(request)
    if (emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN):
        return emp
    raise HTTPException(403, "Admins only")


def _in_range(d: str, d1: str, d2: str) -> bool:
    d = (d or "")[:10]
    return (not d) or (d1 <= d <= d2)


# ----- QMS inspection key sets (Neon + legacy) --------------------------------

async def _inward_keys(d1, d2, legacy=True):
    keys, raw = set(), []  # raw = sample of what QMS actually stored
    for r in await db.fetch(
            "SELECT batch_number FROM coil_inward WHERE log_date BETWEEN $1 AND $2",
            date.fromisoformat(d1), date.fromisoformat(d2)):
        raw.append(r["batch_number"])
        k = idata.batch_key(r["batch_number"])
        if k:
            keys.add(k)
    neon = len(keys)
    if legacy:
        try:
            from .coil import _read_inward_legacy
            for rec in await _read_inward_legacy(use_cache=True):
                if _in_range(str(rec.get("log_date") or ""), d1, d2):
                    raw.append(rec.get("batch_number"))
                    k = idata.batch_key(rec.get("batch_number"))
                    if k:
                        keys.add(k)
        except Exception as e:
            print("[insp inward legacy]", e)
    return keys, {"neon": neon, "total": len(keys), "raw": [x for x in raw if x][:12]}


async def _ctl_keys(d1, d2, legacy=True):
    keys, raw = set(), []
    for r in await db.fetch(
            "SELECT batch_number, coil_number FROM quality_inspection WHERE log_date BETWEEN $1 AND $2",
            date.fromisoformat(d1), date.fromisoformat(d2)):
        raw.append(r["coil_number"] or r["batch_number"])
        for v in (r["batch_number"], r["coil_number"]):
            k = idata.batch_key(v)
            if k:
                keys.add(k)
    neon = len(keys)
    if legacy:
        try:
            from .quality import _read_quality_legacy
            for rec in await _read_quality_legacy():
                if _in_range(str(rec.get("log_date") or rec.get("date") or ""), d1, d2):
                    raw.append(rec.get("coil_number") or rec.get("batch_number"))
                    for v in (rec.get("batch_number"), rec.get("coil_number")):
                        k = idata.batch_key(v)
                        if k:
                            keys.add(k)
        except Exception as e:
            print("[insp ctl legacy]", e)
    return keys, {"neon": neon, "total": len(keys), "raw": [x for x in raw if x][:12]}


async def _shearing_keys(d1, d2, legacy=True):
    keys, raw = set(), []
    for r in await db.fetch(
            "SELECT batch_number FROM shearing_inspection WHERE log_date BETWEEN $1 AND $2",
            date.fromisoformat(d1), date.fromisoformat(d2)):
        raw.append(r["batch_number"])
        k = idata.batch_key(r["batch_number"])
        if k:
            keys.add(k)
    neon = len(keys)
    if legacy:
        try:
            from .shearing import _read_shearing_legacy
            for rec in await _read_shearing_legacy():
                if _in_range(str(rec.get("log_date") or rec.get("date") or ""), d1, d2):
                    raw.append(rec.get("batch_number"))
                    k = idata.batch_key(rec.get("batch_number"))
                    if k:
                        keys.add(k)
        except Exception as e:
            print("[insp shearing legacy]", e)
    return keys, {"neon": neon, "total": len(keys), "raw": [x for x in raw if x][:12]}


async def _pdi_sos(d1, d2):
    sos = set()
    for r in await db.fetch(
            "SELECT items FROM pdi_reports WHERE report_date BETWEEN $1 AND $2",
            date.fromisoformat(d1), date.fromisoformat(d2)):
        items = r["items"]
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        for it in (items or []):
            if isinstance(it, dict):
                k = idata.so_key(it.get("so"))
                if k:
                    sos.add(k)
    return sos


# ----- coil-stream reconciliation --------------------------------------------

def _reconcile_coils(sap_rows, coil_field, date_field, qms_keys, extra=()):
    """Group SAP rows by coil number, mark inspected if any token hits qms_keys."""
    by_coil = {}
    for r in sap_rows:
        coil = (r.get(coil_field) or "").strip()
        if not coil:
            continue
        g = by_coil.setdefault(coil, {"coil": coil, "date": r.get(date_field, ""),
                                      "tokens": idata.coil_tokens(coil)})
        d = (r.get(date_field) or "")[:10]
        if d and (not g["date"] or d < g["date"]):
            g["date"] = d
        for x in extra:
            g.setdefault(x, r.get(x, ""))
    missing, done = [], 0
    for g in by_coil.values():
        if g["tokens"] & qms_keys:
            done += 1
        else:
            row = {"coil": g["coil"], "date": g["date"]}
            for x in extra:
                row[x] = g.get(x, "")
            missing.append(row)
    total = len(by_coil)
    missing.sort(key=lambda r: r["date"])
    return {"total": total, "inspected": done, "missing_count": len(missing),
            "coverage_pct": round(100.0 * done / total, 1) if total else 100.0,
            "missing": missing}


@router.get("/coverage/data")
async def coverage(request: Request,
                   token: str = Query(default=""),
                   date_from: str = Query(default=DEFAULT_FROM, alias="from"),
                   date_to: str = Query(default=""),
                   legacy: int = Query(default=1),
                   limit: int = Query(default=500)):
    await _gate(request, token)
    if not idata.is_configured():
        raise HTTPException(503, "SAP feed not configured (CONSUMER_DATA_BASE)")
    d1 = date_from[:10]
    d2 = (date_to or datetime.now(IST).date().isoformat())[:10]

    try:
        inward_all = [r for r in await idata.inward() if _in_range(r.get("GRPODate"), d1, d2)]
        prod = [r for r in await idata.production() if _in_range(r.get("ProdDate"), d1, d2)]
        disp = [r for r in await idata.dispatch() if _in_range(r.get("DispatchDate"), d1, d2)]
    except Exception as e:
        raise HTTPException(502, f"Could not read SAP feeds: {e}")

    # Coil Inward is for mother COILS only — drop plate/sheet items (T x W x L) that are
    # misfiled under the HR-coil item groups in SAP. Needs the Descr column in the feed;
    # if it's absent (older feed) nothing is dropped.
    inward = [r for r in inward_all if idata.is_coil_desc(r.get("Descr", ""))]
    plates_dropped = len(inward_all) - len(inward)

    lg = bool(legacy)
    (inward_keys, m_in), (ctl_keys, m_ctl), (shear_keys, m_sh), pdi_sos = await asyncio.gather(
        _inward_keys(d1, d2, lg), _ctl_keys(d1, d2, lg),
        _shearing_keys(d1, d2, lg), _pdi_sos(d1, d2))

    s_inward = _reconcile_coils(inward, "CoilNo", "GRPODate", inward_keys,
                                extra=("ItemGroup", "Supplier"))
    s_inward["plates_excluded"] = plates_dropped
    s_ctl = _reconcile_coils([r for r in prod if r.get("Process") == "CTL"],
                             "CoilNo", "ProdDate", ctl_keys, extra=("MachCode", "Grade"))
    s_shear = _reconcile_coils([r for r in prod if r.get("Process") == "Shearing"],
                               "CoilNo", "ProdDate", shear_keys, extra=("MachCode", "Grade"))

    # Pre-dispatch: distinct Sales Orders dispatched, matched to PDI 'so'
    so_seen, so_blank = {}, []
    for r in disp:
        so = idata.so_key(r.get("SO_No"))
        rec = {"so": (r.get("SO_No") or "").strip(), "customer": r.get("Customer", ""),
               "date": (r.get("DispatchDate") or "")[:10], "doc": r.get("DispatchDoc", ""),
               "doctype": r.get("DocType", "")}
        if not so:
            so_blank.append(rec)
            continue
        g = so_seen.get(so)
        if not g or rec["date"] < g["date"]:
            so_seen[so] = rec
    pd_missing = [v for k, v in so_seen.items() if k not in pdi_sos]
    pd_done = len(so_seen) - len(pd_missing)
    pd_missing.sort(key=lambda r: (r["customer"], r["date"]))
    s_pre = {"total": len(so_seen), "inspected": pd_done, "missing_count": len(pd_missing),
             "coverage_pct": round(100.0 * pd_done / len(so_seen), 1) if so_seen else 100.0,
             "missing": pd_missing, "no_so_count": len(so_blank)}

    def cap(s):
        s = dict(s)
        s["missing"] = s["missing"][:limit]
        return s

    return _json({
        "range": {"from": d1, "to": d2},
        "company_db": SAP_COMPANY_DB,
        "streams": {
            "inward": cap(s_inward), "ctl": cap(s_ctl),
            "shearing": cap(s_shear), "predispatch": cap(s_pre),
        },
        "qms_counts": {"inward_keys": len(inward_keys), "ctl_keys": len(ctl_keys),
                       "shearing_keys": len(shear_keys), "pdi_sos": len(pdi_sos),
                       "legacy_on": lg},
        "diagnostics": {
            "inward": {"sap": [r.get("CoilNo") for r in inward[:12]],
                       "qms_raw": m_in["raw"], "neon": m_in["neon"], "total": m_in["total"]},
            "ctl": {"sap": [r.get("CoilNo") for r in prod if r.get("Process") == "CTL"][:12],
                    "qms_raw": m_ctl["raw"], "neon": m_ctl["neon"], "total": m_ctl["total"]},
            "shearing": {"sap": [r.get("CoilNo") for r in prod if r.get("Process") == "Shearing"][:12],
                         "qms_raw": m_sh["raw"], "neon": m_sh["neon"], "total": m_sh["total"]},
            "predispatch": {"sap": [r.get("SO_No") for r in disp[:12]],
                            "qms_raw": sorted(list(pdi_sos))[:12]},
        },
    })


# =============================================================================
#  ONE-OFF: file EXISTING report PDFs into date-wise sub-folders
#  New reports already save into <module>/PDF/<YYYY-MM-DD>/… ; this moves the
#  historical flat files in. Idempotent, dry-run by default, chunked by `limit`.
#    /insp/organize-pdfs?token=BSC%40123&module=all&dry=1
#    …&module=coil&dry=0&limit=150   (repeat until remaining=0)
# =============================================================================
_ROOTS = {"coil": "Inward/PDF", "quality": "Quality/PDF",
          "shearing": "Shearing/PDF", "pdi": "Pre-Delivery"}
_DATEDIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_from_name(name: str, pdi_map: dict | None = None) -> str | None:
    base = name[:-4] if name.lower().endswith(".pdf") else name
    d = odsync.date_folder(name)                 # parses (dd-mm-yyyy)
    if d != "undated":
        return d
    if pdi_map and base in pdi_map:              # PDI: look up report_date by report_no
        d = odsync.date_folder(pdi_map[base])
        if d != "undated":
            return d
    m = re.search(r"-(\d{2})(\d{2})(\d{2})(?:-|$)", base)   # report_no …-161126-09
    if m:
        dd, mm, yy = m.groups()
        return f"20{yy}-{mm}-{dd}"
    return None


@router.get("/organize-pdfs")
async def organize_pdfs(request: Request, token: str = Query(default=""),
                        module: str = Query(default="all"),
                        dry: int = Query(default=1), limit: int = Query(default=50)):
    await _gate(request, token)
    mods = list(_ROOTS) if module == "all" else [m for m in [module] if m in _ROOTS]
    if not mods:
        raise HTTPException(400, "module must be coil|quality|shearing|pdi|all")
    tok = await graph.get_token()
    if not tok:
        raise HTTPException(502, "OneDrive sign-in failed")
    out = {}
    budget = max(1, limit)
    for m in mods:
        rootp = f"{ONEDRIVE_BASE}/{_ROOTS[m]}"
        # DB fallbacks for files whose NAME carries no (dd-mm-yyyy): map by file_name,
        # and for CTL by the trailing record id in the file name.
        pdi_map, name_map, id_map = {}, {}, {}
        if m == "pdi":
            for r in await db.fetch("SELECT report_no, report_date FROM pdi_reports"):
                pdi_map[str(r["report_no"])] = str(r["report_date"] or "")
        elif m == "coil":
            for r in await db.fetch("SELECT file_name, log_date, created_at FROM coil_inward WHERE file_name IS NOT NULL"):
                name_map[str(r["file_name"])] = odsync.date_folder(r["log_date"] or r["created_at"])
        elif m == "shearing":
            for r in await db.fetch("SELECT file_name, log_date, created_at FROM shearing_inspection WHERE file_name IS NOT NULL"):
                name_map[str(r["file_name"])] = odsync.date_folder(r["log_date"] or r["created_at"])
        elif m == "quality":
            for r in await db.fetch("SELECT id, log_date, created_at FROM quality_inspection"):
                id_map[int(r["id"])] = odsync.date_folder(r["log_date"] or r["created_at"])
        files = await graph.list_files_deep(tok, rootp, max_depth=2)
        planned, moved, errors = [], [], []
        skipped = undated = 0
        ensured: dict[str, str] = {}   # target folder path -> id (cache)
        for f in files:
            name = f["name"]
            if not name.lower().endswith(".pdf"):
                continue
            parent = f["parent_path"]
            rel = parent[len(rootp):].strip("/")         # "" or "CTL-1" or "2026-11-26[/CTL-1]"
            segs = [s for s in rel.split("/") if s]
            if segs and _DATEDIR.match(segs[0]):          # already in a date folder
                skipped += 1
                continue
            d = _date_from_name(name, pdi_map)
            if not d:                                   # DB fallback by file name / record id
                base = name[:-4]
                cand = name_map.get(base)
                if (not cand or cand == "undated") and m == "quality":
                    mm = re.search(r"_(\d+)$", base)
                    if mm:
                        cand = id_map.get(int(mm.group(1)))
                if cand and cand != "undated":
                    d = cand
            if not d:
                undated += 1
                continue
            tail = "/".join(segs)                         # preserve e.g. the CTL-1 machine folder
            target = f"{rootp}/{d}" + (f"/{tail}" if tail else "")
            planned.append({"file": name, "from": parent or rootp, "to": target})
            if dry or budget <= 0:
                continue
            try:
                if target not in ensured:
                    await graph.create_folder(tok, rootp, d)
                    if tail:
                        await graph.create_folder(tok, f"{rootp}/{d}", tail)
                    tid = await graph.get_item_id(tok, target)
                    if not tid:
                        errors.append({"file": name, "error": f"target folder missing: {target}"})
                        continue
                    ensured[target] = tid
                if await graph.move_item(tok, f["id"], ensured[target]):
                    moved.append(name)
                    budget -= 1
                else:
                    errors.append({"file": name, "error": "move failed"})
            except Exception as e:
                errors.append({"file": name, "error": str(e)[:160]})
        out[m] = {"pdf_files": sum(1 for f in files if f["name"].lower().endswith(".pdf")),
                  "already_filed": skipped, "undated": undated,
                  "to_move": len(planned), "moved": len(moved),
                  "remaining": len(planned) - len(moved),
                  "errors": errors[:20], "sample": planned[:12]}
    return _json({"dry_run": bool(dry), "modules": out})
