"""OneDrive Sync — shows records that never reached OneDrive (or failed) and
re-runs each module's od_sync() for them in small batches.

Back-fill is safe to run more than once: PDFs are overwritten in place, an Excel
row is only added if that record isn't already in the log, and CSV lines are only
added if the record's key isn't already in the file."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from .. import db, odsync
from ..auth import require_employee, current_employee
from . import coil, quality, shearing, pdi, eightd, complaint, dg
from .pages import templates, _ctx

router = APIRouter(tags=["onedrive-sync"])

# module -> (label, table, ref sql, label sql, sync fn, ensure fn)
MODULES = {
    "coil": ("Coil Inward", "coil_inward", "id::text",
             "COALESCE(batch_number,'') || ' · ' || COALESCE(vehicle_number,'')",
             lambda ref: coil.od_sync(int(ref), dedupe=True), coil.ensure_table),
    "quality": ("CTL Quality", "quality_inspection", "id::text",
                "COALESCE(batch_number,'') || ' · ' || COALESCE(machine_name,'')",
                lambda ref: quality.od_sync(int(ref), dedupe=True), quality.ensure_table),
    "shearing": ("Shearing Quality", "shearing_inspection", "id::text",
                 "COALESCE(batch_number,'') || ' · ' || COALESCE(customer_name,'')",
                 lambda ref: shearing.od_sync(int(ref), dedupe=True), shearing.ensure_table),
    "pdi": ("Pre-Delivery", "pdi_reports", "report_no", "COALESCE(customer,'')",
            lambda ref: pdi.od_sync(ref, dedupe=True), pdi.ensure_table),
    "eightd": ("8D Report", "eightd_reports", "report_no", "COALESCE(customer,'')",
               lambda ref: eightd.od_sync(ref, dedupe=True), eightd.ensure_table),
    "complaint": ("Quality Complaint", "quality_complaints", "complaint_no",
                  "COALESCE(customer_name,'')",
                  lambda ref: complaint.od_sync(ref, dedupe=True), complaint.ensure_table),
    "dg": ("DG Log", "dg_log", "id::text",
           "COALESCE(dg_set,'') || ' · ' || COALESCE(log_date::text,'') || ' ' || COALESCE(start_at,'')",
           lambda ref: dg.od_sync(int(ref), dedupe=True), dg.ensure_table),
}


def _since(v: Optional[str]) -> datetime:
    try:
        d = date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        d = date.today() - timedelta(days=14)
    return datetime(d.year, d.month, d.day, tzinfo=odsync.IST)


def _pending_sql(key: str) -> str:
    _lbl, table, ref, label, _fn, _en = MODULES[key]
    return (f"SELECT x.ref, x.label, x.created_at, s.ok, s.errors, s.attempts FROM "
            f"(SELECT {ref} AS ref, {label} AS label, created_at FROM {table} WHERE created_at >= $1) x "
            f"LEFT JOIN onedrive_sync s ON s.module='{key}' AND s.ref=x.ref "
            f"WHERE COALESCE(s.ok,false)=false ORDER BY x.created_at ASC")


@router.get("/onedrive-sync", response_class=HTMLResponse)
async def sync_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "onedrive_sync.html", _ctx(request, emp, "onedrive_sync", section="OneDrive Sync"))


@router.get("/onedrive-sync/status")
async def sync_status(since: Optional[str] = None, emp: dict = Depends(require_employee)):
    await odsync.ensure()
    t0 = _since(since)
    out = []
    for key, (label, table, _ref, _l, _fn, ensure) in MODULES.items():
        try:
            await ensure()
            total = await db.fetchval(f"SELECT COUNT(*) FROM {table} WHERE created_at >= $1", t0)
            pend = [dict(r) for r in await db.fetch(_pending_sql(key), t0)]
            out.append({"key": key, "label": label, "total": total or 0,
                        "pending": len(pend),
                        "failed": [p for p in pend if p.get("errors")][:50],
                        "never": sum(1 for p in pend if p.get("ok") is None)})
        except Exception as e:
            out.append({"key": key, "label": label, "error": str(e)})
    return JSONResponse(jsonable_encoder(out), headers={"Cache-Control": "no-store"})


class RunIn(BaseModel):
    module: str
    since: Optional[str] = None
    limit: int = 3
    exclude: list = []      # refs already tried in this pass (so a failing one can't loop)


@router.post("/onedrive-sync/run")
async def sync_run(body: RunIn, emp: dict = Depends(require_employee)):
    if body.module not in MODULES:
        raise HTTPException(400, "unknown module")
    await odsync.ensure()
    _label, _t, _r, _l, fn, ensure = MODULES[body.module]
    await ensure()
    t0 = _since(body.since)
    skip = {str(x) for x in (body.exclude or [])}
    pend = [dict(r) for r in await db.fetch(_pending_sql(body.module), t0) if str(r["ref"]) not in skip]
    batch = pend[:max(1, min(body.limit, 10))]
    done = []
    for p in batch:
        res = await fn(p["ref"])
        done.append({"ref": p["ref"], "label": p["label"], **res})
    left = len(pend) - len(batch)
    return {"ok": True, "processed": done, "remaining": left}
