"""Inspection Analysis — one admin view across the four inspection modules:
Coil Inward, CTL Quality, Shearing Quality and PDI. Turns the recorded
inspections into quality insight: per-stage KPIs, a supplier (make) scorecard,
a defect/rejection Pareto, a monthly trend, and a batch traceability lookup.

Read-only; aggregates existing tables (coil_inward, quality_inspection,
shearing_inspection, pdi_reports). Small data volumes, so rows are fetched and
aggregated in Python to keep the JSON-field handling simple and robust.
"""
import json
from datetime import date, datetime, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse

from .. import db
from ..auth import require_employee

router = APIRouter(prefix="/analysis", tags=["analysis"])

ANALYSIS_BUILD = "2026-10-29-v1-inspection-analysis"

# PDI auto out-of-spec tolerances (same as the PDI form)
PDI_TOL = {"th": 0.5, "wd": 5.0, "len": 10.0}


# ---------------- helpers ----------------
def _json(obj):
    return JSONResponse(obj, headers={"Cache-Control": "no-store"})


def _isyes(v) -> bool:
    return str(v or "").strip().lower() in ("yes", "y", "true", "1")


def _isno(v) -> bool:
    return str(v or "").strip().lower() in ("no", "n", "false", "0")


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm(v) -> str:
    s = str(v or "").strip().upper()
    return s or "UNKNOWN"


def _payload(p):
    if isinstance(p, str):
        try:
            return json.loads(p or "{}")
        except Exception:
            return {}
    return p or {}


def _inward_flagged(r) -> bool:
    """A coil counts as 'damaged' — same rule the Inward screen uses."""
    if any(_isyes(r.get(k)) for k in ("edge_inner", "edge_outer", "scratch", "rust")):
        return True
    if _isno(r.get("id_sticker")):
        return True
    od = str(r.get("other_damages") or "").strip().lower()
    return bool(od and od != "nil")


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def _rej_yes(flag) -> bool:
    return str(flag or "").strip().lower() in ("yes", "y", "true", "1")


def _rejections(payload):
    """Yield (defect_type, qty) for each rejection recorded in a CTL/Shearing payload."""
    p = _payload(payload)
    for rj in (p.get("rejections") or []):
        if not isinstance(rj, dict):
            continue
        qty = _num(rj.get("qty")) or 0
        types = rj.get("types") or []
        if isinstance(types, str):
            types = [t.strip() for t in types.split(",") if t.strip()]
        if not types:
            types = ["(unspecified)"]
        for t in types:
            yield (_norm(t), qty)


def _pdi_item_oos(it):
    """Is a PDI item out of spec? (any dimension beyond tol, or any surface check NOT OK)"""
    if not isinstance(it, dict):
        return False
    sp, ob = it.get("spec") or {}, it.get("obs") or {}
    for k, tol in PDI_TOL.items():
        s, o = _num(sp.get(k)), _num(ob.get(k))
        if s is not None and o is not None and abs(o - s) > tol:
            return True
    for v in (it.get("checks") or {}).values():
        if str(v or "").strip().upper() in ("NOT OK", "NOTOK", "NO", "FAIL"):
            return True
    return False


def _month(d):
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m")
    s = str(d or "")[:7]
    return s or "?"


# ---------------- data endpoint ----------------
@router.get("/inspection/data")
async def inspection_data(
        frm: str = Query(default="", alias="from"),
        to: str = Query(default=""),
        make: str = Query(default=""),
        grade: str = Query(default=""),
        customer: str = Query(default=""),
        emp: dict = Depends(require_employee)):
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")

    today = date.today()
    try:
        d_to = datetime.strptime(to, "%Y-%m-%d").date() if to else today
    except ValueError:
        d_to = today
    try:
        d_from = datetime.strptime(frm, "%Y-%m-%d").date() if frm else (d_to - timedelta(days=365))
    except ValueError:
        d_from = d_to - timedelta(days=365)
    mk, gr, cu = make.strip().lower(), grade.strip().lower(), customer.strip().lower()

    def mkeep(v):
        return (not mk) or mk in str(v or "").lower()

    def gkeep(v):
        return (not gr) or gr in str(v or "").lower()

    def ckeep(v):
        return (not cu) or cu in str(v or "").lower()

    # ---- fetch (date-bounded), then filter/aggregate in Python ----
    inward = [dict(r) for r in await db.fetch(
        "SELECT log_date, make_of_coil, grade, batch_number, thickness, actual_thickness, "
        "width, actual_width, edge_inner, edge_outer, scratch, strapping, rust, "
        "other_damages, id_sticker, inspected_by FROM coil_inward "
        "WHERE log_date BETWEEN $1 AND $2", d_from, d_to)]
    inward = [r for r in inward if mkeep(r.get("make_of_coil")) and gkeep(r.get("grade"))]

    ctl = [dict(r) for r in await db.fetch(
        "SELECT log_date, make, grade, customer_name, batch_number, coil_number, machine_name, "
        "inspector, sheets_measured, oot_count, rejection_flag, payload FROM quality_inspection "
        "WHERE log_date BETWEEN $1 AND $2", d_from, d_to)]
    ctl = [r for r in ctl if mkeep(r.get("make")) and gkeep(r.get("grade")) and ckeep(r.get("customer_name"))]

    shr = [dict(r) for r in await db.fetch(
        "SELECT log_date, make, grade, customer_name, batch_number, qc_name, sheets_measured, "
        "rejection_flag, has_reworks, payload FROM shearing_inspection "
        "WHERE log_date BETWEEN $1 AND $2", d_from, d_to)]
    shr = [r for r in shr if mkeep(r.get("make")) and gkeep(r.get("grade")) and ckeep(r.get("customer_name"))]

    pdi = [dict(r) for r in await db.fetch(
        "SELECT report_date, customer, items, inspected_by FROM pdi_reports "
        "WHERE report_date BETWEEN $1 AND $2", d_from, d_to)]
    pdi = [r for r in pdi if ckeep(r.get("customer"))]

    # ---- per-stage KPI summary ----
    kpis = []

    inw_dmg = sum(1 for r in inward if _inward_flagged(r))
    thdevs = [(_num(r.get("actual_thickness")) - _num(r.get("thickness")))
              for r in inward
              if _num(r.get("actual_thickness")) is not None and _num(r.get("thickness")) is not None]
    kpis.append({"stage": "Inward", "records": len(inward), "units": len(inward),
                 "units_label": "coils",
                 "reject_pct": _pct(inw_dmg, len(inward)), "reject_label": "Damage %",
                 "oot_pct": None, "rework_pct": None,
                 "note": (("avg thk dev %.2f mm" % (sum(thdevs) / len(thdevs))) if thdevs else "")})

    ctl_sheets = sum(int(_num(r.get("sheets_measured")) or 0) for r in ctl)
    ctl_oot = sum(int(_num(r.get("oot_count")) or 0) for r in ctl)
    ctl_rej = sum(1 for r in ctl if _rej_yes(r.get("rejection_flag")))
    ctl_fp = sum(1 for r in ctl
                 if not _rej_yes(r.get("rejection_flag")) and int(_num(r.get("oot_count")) or 0) == 0)
    kpis.append({"stage": "CTL", "records": len(ctl), "units": ctl_sheets, "units_label": "sheets",
                 "reject_pct": _pct(ctl_rej, len(ctl)), "reject_label": "Rejection %",
                 "oot_pct": _pct(ctl_oot, ctl_sheets), "rework_pct": None,
                 "first_pass_pct": _pct(ctl_fp, len(ctl))})

    shr_sheets = sum(int(_num(r.get("sheets_measured")) or 0) for r in shr)
    shr_rej = sum(1 for r in shr if _rej_yes(r.get("rejection_flag")))
    shr_rw = sum(1 for r in shr if _rej_yes(r.get("has_reworks")))
    shr_fp = sum(1 for r in shr if not _rej_yes(r.get("rejection_flag")))
    kpis.append({"stage": "Shearing", "records": len(shr), "units": shr_sheets, "units_label": "sheets",
                 "reject_pct": _pct(shr_rej, len(shr)), "reject_label": "Rejection %",
                 "oot_pct": None, "rework_pct": _pct(shr_rw, len(shr)),
                 "first_pass_pct": _pct(shr_fp, len(shr))})

    pdi_items_total = 0
    pdi_oos_reports = 0
    for r in pdi:
        items = r.get("items")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        items = items or []
        pdi_items_total += len(items)
        if any(_pdi_item_oos(it) for it in items):
            pdi_oos_reports += 1
    kpis.append({"stage": "PDI", "records": len(pdi), "units": pdi_items_total, "units_label": "items",
                 "reject_pct": _pct(pdi_oos_reports, len(pdi)), "reject_label": "Out-of-spec %",
                 "oot_pct": None, "rework_pct": None})

    # ---- supplier (make) scorecard ----
    sup = {}

    def _sup(mk_):
        return sup.setdefault(mk_, {"make": mk_, "coils": 0, "inw_dmg": 0,
                                    "ctl_n": 0, "ctl_rej": 0, "shr_n": 0, "shr_rej": 0,
                                    "thdev_sum": 0.0, "thdev_n": 0})
    for r in inward:
        g = _sup(_norm(r.get("make_of_coil")))
        g["coils"] += 1
        if _inward_flagged(r):
            g["inw_dmg"] += 1
        a, d0 = _num(r.get("actual_thickness")), _num(r.get("thickness"))
        if a is not None and d0 is not None:
            g["thdev_sum"] += (a - d0); g["thdev_n"] += 1
    for r in ctl:
        g = _sup(_norm(r.get("make")))
        g["ctl_n"] += 1
        if _rej_yes(r.get("rejection_flag")):
            g["ctl_rej"] += 1
    for r in shr:
        g = _sup(_norm(r.get("make")))
        g["shr_n"] += 1
        if _rej_yes(r.get("rejection_flag")):
            g["shr_rej"] += 1
    suppliers = []
    for g in sup.values():
        down_n = g["ctl_n"] + g["shr_n"]
        down_rej = g["ctl_rej"] + g["shr_rej"]
        suppliers.append({
            "make": g["make"], "coils": g["coils"],
            "inward_damage_pct": _pct(g["inw_dmg"], g["coils"]),
            "avg_thk_dev": (round(g["thdev_sum"] / g["thdev_n"], 2) if g["thdev_n"] else None),
            "ctl_reject_pct": _pct(g["ctl_rej"], g["ctl_n"]),
            "shr_reject_pct": _pct(g["shr_rej"], g["shr_n"]),
            "overall_reject_pct": _pct(down_rej, down_n)})
    suppliers.sort(key=lambda s: (s["overall_reject_pct"], s["inward_damage_pct"]), reverse=True)

    # ---- defect / rejection Pareto ----
    par = {}

    def _par(stage, typ, qty=0):
        k = (stage, typ)
        d0 = par.setdefault(k, {"stage": stage, "type": typ, "count": 0, "qty": 0})
        d0["count"] += 1
        d0["qty"] += int(qty or 0)
    _INW_FLAGS = [("edge_inner", "EDGE DAMAGE - INNER"), ("edge_outer", "EDGE DAMAGE - OUTER"),
                  ("scratch", "SCRATCH"), ("rust", "RUST")]
    for r in inward:
        for col, lab in _INW_FLAGS:
            if _isyes(r.get(col)):
                _par("Inward", lab)
        if _isno(r.get("id_sticker")):
            _par("Inward", "STICKER MISSING")
        od = str(r.get("other_damages") or "").strip()
        if od and od.lower() != "nil":
            _par("Inward", "OTHER DAMAGE")
    for r in ctl:
        for t, q in _rejections(r.get("payload")):
            _par("CTL", t, q)
    for r in shr:
        for t, q in _rejections(r.get("payload")):
            _par("Shearing", t, q)
    for r in pdi:
        items = r.get("items")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            sp, ob = it.get("spec") or {}, it.get("obs") or {}
            for k, tol in PDI_TOL.items():
                s, o = _num(sp.get(k)), _num(ob.get(k))
                if s is not None and o is not None and abs(o - s) > tol:
                    _par("PDI", {"th": "THICKNESS OOS", "wd": "WIDTH OOS", "len": "LENGTH OOS"}[k])
            for cname, cv in (it.get("checks") or {}).items():
                if str(cv or "").strip().upper() in ("NOT OK", "NOTOK", "NO", "FAIL"):
                    _par("PDI", cname.upper() + " NOT OK")
    pareto = sorted(par.values(), key=lambda d: (d["count"], d["qty"]), reverse=True)

    # ---- monthly trend (defect-bearing records per stage) ----
    tr = {}

    def _tr(m, stage, bad=False):
        d0 = tr.setdefault((m, stage), {"month": m, "stage": stage, "records": 0, "bad": 0})
        d0["records"] += 1
        if bad:
            d0["bad"] += 1
    for r in inward:
        _tr(_month(r.get("log_date")), "Inward", _inward_flagged(r))
    for r in ctl:
        _tr(_month(r.get("log_date")), "CTL", _rej_yes(r.get("rejection_flag")))
    for r in shr:
        _tr(_month(r.get("log_date")), "Shearing", _rej_yes(r.get("rejection_flag")))
    for r in pdi:
        items = r.get("items")
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except Exception:
                items = []
        _tr(_month(r.get("report_date")), "PDI", any(_pdi_item_oos(it) for it in (items or [])))
    trend = sorted(tr.values(), key=lambda d: (d["month"], d["stage"]))
    for d0 in trend:
        d0["bad_pct"] = _pct(d0["bad"], d0["records"])

    return _json({"ok": True, "build": ANALYSIS_BUILD,
                  "from": d_from.isoformat(), "to": d_to.isoformat(),
                  "kpis": kpis, "suppliers": suppliers, "pareto": pareto, "trend": trend,
                  "counts": {"inward": len(inward), "ctl": len(ctl),
                             "shearing": len(shr), "pdi": len(pdi)}})


# ---------------- batch traceability ----------------
@router.get("/inspection/batch")
async def inspection_batch(q: str = Query(default=""), emp: dict = Depends(require_employee)):
    if not emp.get("is_admin"):
        raise HTTPException(403, "Admins only")
    q = (q or "").strip()
    if not q:
        return _json({"ok": False, "msg": "Enter a batch or coil number."})
    like = "%" + q + "%"
    rows = []
    for r in await db.fetch(
            "SELECT log_date, make_of_coil, grade, batch_number, thickness, actual_thickness, "
            "edge_inner, edge_outer, scratch, rust, id_sticker, other_damages, inspected_by "
            "FROM coil_inward WHERE batch_number ILIKE $1 OR coil_id ILIKE $1 "
            "ORDER BY log_date", like):
        r = dict(r)
        rows.append({"stage": "Inward", "date": str(r.get("log_date") or ""),
                     "make": r.get("make_of_coil") or "", "grade": r.get("grade") or "",
                     "ref": r.get("batch_number") or "",
                     "detail": ("Flagged" if _inward_flagged(r) else "Condition OK"),
                     "by": r.get("inspected_by") or ""})
    for r in await db.fetch(
            "SELECT log_date, make, grade, batch_number, coil_number, machine_name, inspector, "
            "sheets_measured, oot_count, rejection_flag FROM quality_inspection "
            "WHERE batch_number ILIKE $1 OR coil_number ILIKE $1 ORDER BY log_date", like):
        r = dict(r)
        rows.append({"stage": "CTL", "date": str(r.get("log_date") or ""),
                     "make": r.get("make") or "", "grade": r.get("grade") or "",
                     "ref": r.get("batch_number") or r.get("coil_number") or "",
                     "detail": "%s · %s sheets · OOT %s · %s" % (
                         r.get("machine_name") or "-", r.get("sheets_measured") or 0,
                         r.get("oot_count") or 0,
                         "Rejected" if _rej_yes(r.get("rejection_flag")) else "No rejection"),
                     "by": r.get("inspector") or ""})
    for r in await db.fetch(
            "SELECT log_date, make, grade, batch_number, qc_name, sheets_measured, "
            "rejection_flag, has_reworks FROM shearing_inspection "
            "WHERE batch_number ILIKE $1 ORDER BY log_date", like):
        r = dict(r)
        rows.append({"stage": "Shearing", "date": str(r.get("log_date") or ""),
                     "make": r.get("make") or "", "grade": r.get("grade") or "",
                     "ref": r.get("batch_number") or "",
                     "detail": "%s sheets · %s · %s" % (
                         r.get("sheets_measured") or 0,
                         "Rejected" if _rej_yes(r.get("rejection_flag")) else "No rejection",
                         "Rework" if _rej_yes(r.get("has_reworks")) else "No rework"),
                     "by": r.get("qc_name") or ""})
    rows.sort(key=lambda x: (x["date"], x["stage"]))
    return _json({"ok": True, "q": q, "rows": rows,
                  "note": "PDI is recorded per sales order (no batch field), so it is not linked here."})


@router.get("/version")
async def analysis_version():
    return _json({"build": ANALYSIS_BUILD})
