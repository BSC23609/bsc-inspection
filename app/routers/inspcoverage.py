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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db
from .. import insp_data as idata
from ..auth import current_employee
from ..config import AGENT_TOKEN, SAP_COMPANY_DB

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
