"""Admin / Settings — reads and writes the same OneDrive config the legacy app
used (BSC Inspections/Config/settings.json). These settings feed the inspection
forms (inspector lists, mills, grades, rejection types, sampling, tolerances)."""
import json
from datetime import datetime, timezone, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import graph, db
from .. import customer_auth as CA
from ..auth import require_employee, current_employee, hash_pw
from ..config import ONEDRIVE_BASE, AGENT_TOKEN, CLIENT_ID, TENANT_ID, USER_ID, CLIENT_SECRET

router = APIRouter(prefix="/admin", tags=["admin"])


def _graph_identity() -> dict:
    """Which Azure app the running site is actually configured with — so you can confirm
    CLIENT_ID / TENANT_ID straight from the browser (Vercel stores them write-only). The
    secret value is never exposed; only whether one is set and its length."""
    return {
        "client_id": CLIENT_ID or "(not set)",
        "tenant_id": TENANT_ID or "(not set)",
        "onedrive_user": USER_ID or "(not set)",
        "client_secret_set": bool(CLIENT_SECRET),
        "client_secret_len": len(CLIENT_SECRET or ""),
    }


@router.get("/graph-health")
async def graph_health(token: str = Query(default=""), alert: int = Query(default=0),
                       write: int = Query(default=0),
                       emp: Optional[dict] = Depends(current_employee)):
    """Check that OneDrive/Microsoft Graph access is working. Admin session or
    ?token=AGENT_TOKEN. Returns HTTP 200 {ok:true} when healthy, HTTP 503 {ok:false,...}
    when not (e.g. the Azure client secret has expired). With ?alert=1 it also emails an
    alert on failure, so a silent OneDrive outage is caught the same day instead of weeks
    later. A daily GitHub Action pings it with &alert=1."""
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ident = _graph_identity()
    res = await graph.check_access()
    wr = await graph.check_write() if write else None
    read_ok = bool(res.get("ok"))
    write_ok = (wr is None) or bool(wr.get("ok"))
    if read_ok and write_ok:
        out = {"ok": True, "checked_at": checked, "read": f"{res.get('stage')}: {res.get('detail')}",
               "identity": ident}
        if wr is not None:
            out["write"] = f"{wr.get('stage')}: {wr.get('detail')}"
        return out
    detail = (f"read {res.get('stage')}: {res.get('detail')}" if not read_ok
              else f"write {wr.get('stage')}: {wr.get('detail')}")
    if alert:
        try:
            from ..notify import send_alert_email
            send_alert_email(
                "QMS ALERT: OneDrive access is DOWN (inspections not saving to OneDrive)",
                "The QMS app cannot reach OneDrive via Microsoft Graph.\n\n"
                f"Checked (UTC): {checked}\n"
                f"Failure: {detail}\n\n"
                "Most likely the Azure AD client secret has expired. To fix:\n"
                "  1. Microsoft Entra > App registrations > [QMS app] > Certificates & secrets\n"
                "     > New client secret (copy the value).\n"
                "  2. Vercel > QMS project > Settings > Environment Variables > update\n"
                "     CLIENT_SECRET > Redeploy.\n\n"
                "Until fixed, Inward / Quality / Shearing / Pre-delivery PDFs and log rows\n"
                "are NOT being written to OneDrive. The database records are safe and can\n"
                "be backfilled afterwards.")
        except Exception:
            pass
    return JSONResponse(status_code=503,
                        content={"ok": False, "checked_at": checked, "error": detail, "identity": ident})


@router.get("/inspection-counts")
async def inspection_counts(token: str = Query(default=""),
                            emp: Optional[dict] = Depends(current_employee)):
    """Diagnostic: per-module record counts, latest record, and a per-day breakdown for
    the last 30 days — to tell whether inspections are still being SAVED (DB) after a given
    date. Read-only. Admin session or ?token=AGENT_TOKEN."""
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    tables = {"inward": "coil_inward", "quality": "quality_inspection",
              "shearing": "shearing_inspection", "pre_delivery": "pdi_reports"}
    out = {}
    for label, tbl in tables.items():      # tbl values are a fixed whitelist, not user input
        try:
            total = await db.fetchval(f"SELECT count(*) FROM {tbl}")
            latest = await db.fetchval(f"SELECT max(created_at) FROM {tbl}")
            last30 = await db.fetchval(
                f"SELECT count(*) FROM {tbl} WHERE created_at >= now() - interval '30 days'")
            recent = await db.fetch(
                f"SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS d, count(*) AS n "
                f"FROM {tbl} WHERE created_at >= now() - interval '40 days' GROUP BY d ORDER BY d DESC")
            out[label] = {
                "table": tbl,
                "total": total,
                "last_30_days": last30,
                "latest_created_at": latest.isoformat() if latest else None,
                "by_day_last_40": {str(r["d"]): r["n"] for r in recent},
            }
        except Exception as e:
            out[label] = {"table": tbl, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return out


@router.get("/backfill-onedrive")
async def backfill_onedrive(module: str = Query(default=""), token: str = Query(default=""),
                            limit: int = Query(default=6), skip: int = Query(default=0),
                            since: str = Query(default=""), log_only: int = Query(default=0),
                            emp: Optional[dict] = Depends(current_employee)):
    """Regenerate missing OneDrive PDFs/log rows from the DATABASE records for a module —
    inward | shearing | pre_delivery (Quality has its own /quality/admin/backfill-pdfs).
    Paginated: follow next_url until remaining=0. ?since=YYYY-MM-DD limits to records on/after
    that date (use since=2026-10-23 for the gap). Admin session or ?token=AGENT_TOKEN."""
    if not ((emp and emp.get("is_admin")) or (AGENT_TOKEN and token == AGENT_TOKEN)):
        raise HTTPException(403, "Admins only")
    try:
        from . import coil, shearing, pdi          # local import avoids any load-order issue
        reg = {
            "inward":       ("coil_inward",         "id",        coil.od_sync),
            "shearing":     ("shearing_inspection", "id",        shearing.od_sync),
            "pre_delivery": ("pdi_reports",         "report_no", pdi.od_sync),
        }
        mod = (module or "").strip().lower()
        if mod not in reg:
            return JSONResponse(status_code=400,
                                content={"ok": False, "error": "module must be inward, shearing or pre_delivery"})
        tbl, idcol, writer = reg[mod]
        limit = max(1, min(int(limit or 6), 10))
        skip = max(0, int(skip or 0))
        where = ""
        s = (since or "").strip()
        if s:
            try:
                d = date.fromisoformat(s)           # validates; safe to inline below
            except ValueError:
                return JSONResponse(status_code=400,
                                    content={"ok": False, "error": "since must be YYYY-MM-DD"})
            where = f"WHERE created_at >= '{d.isoformat()}'::timestamptz"
        total = await db.fetchval(f"SELECT count(*) FROM {tbl} {where}")
        rows = await db.fetch(
            f"SELECT {idcol} AS ref FROM {tbl} {where} ORDER BY {idcol} LIMIT {limit} OFFSET {skip}")
        # log_only=1 skips the slow PDF/photo uploads and writes just the Excel log row —
        # much faster when the PDFs are already backfilled. Only inward/shearing take `pdf`.
        wkw = {"dedupe": True}
        if int(log_only or 0) and mod in ("inward", "shearing"):
            wkw["pdf"] = False
        results = []
        for r in rows:
            ref = r["ref"]
            try:
                res = await writer(ref, **wkw)
                results.append({"ref": str(ref), "ok": bool(res.get("ok")),
                                "saved": res.get("saved"), "errors": res.get("errors")})
            except Exception as e:
                results.append({"ref": str(ref), "ok": False,
                                "errors": [f"{type(e).__name__}: {str(e)[:200]}"]})
        done_through = skip + len(rows)
        remaining = max(0, (total or 0) - done_through)
        nxt = (f"https://qms.bharatsteels.in/admin/backfill-onedrive?module={mod}"
               f"&token={token}&limit={limit}&skip={done_through}" + (f"&since={s}" if s else "")
               + ("&log_only=1" if int(log_only or 0) else ""))
        return {"ok": True, "module": mod, "records_total": total, "done_through": done_through,
                "remaining": remaining, "ok_count": sum(1 for x in results if x["ok"]),
                "results": results, "next_url": (nxt if remaining > 0 else None)}
    except Exception as e:
        import traceback
        return JSONResponse(status_code=500,
                            content={"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}",
                                     "trace": traceback.format_exc()[-800:]})


SETTINGS_PATH = f"{ONEDRIVE_BASE}/Config/settings.json"

DEFAULT_SETTINGS = {
    "inspectors_inward": ["Sathya", "Kumar", "Mahendran", "Dhanush", "Sam"],
    "inspectors_quality": ["Sathya", "Kumar", "Mahendran", "Dhanush", "Vignesh"],
    "qc_shearing_default": "Vignesh",
    "mills": ["SAIL", "SAIL RSP", "SAIL BSP", "JSW", "TATA", "AMNS", "NMDC", "JSPL"],
    "vendors": {"SAIL": "", "NMDC": "", "RINL": "", "JSW": ""},
    "grades": ["E250", "E350 / ST52", "HSFQ450 / 450 / EQV", "HSFQ550 / 550 / EQV"],
    "complaint_emails": {
        "sales_raise_to": ["pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
        "internal_close_to": ["info@bharatsteels.in", "gourav@bharatsteels.in", "kannan@bharatsteels.in"],
        "vendor_escalation_cc": ["pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
        "resolved_to": ["info@bharatsteels.in", "pdqc@bharatsteels.in", "kannan@bharatsteels.in", "gourav@bharatsteels.in"],
    },
    "inward_emails": {
        "default_to": ["support@bharatsteels.in"],
        "wheels_india_to": ["support@bharatsteels.in", "kannan@bharatsteels.in"],
    },
    "rejection_types": ["WIDTH VARIATION", "LENGTH VARIATION", "DIAGONAL VARIATION", "LINE MARK",
                        "DENT MARK", "CORE LAMINATION", "D SCALE", "RUST", "PITTED", "BURR", "BREAKAWAY"],
    "sample_frequency": [
        {"min": 1, "max": 2, "samples": "1-2"},
        {"min": 3, "max": 20, "samples": 3},
        {"min": 21, "max": 50, "samples": 5},
        {"min": 51, "max": 150, "samples": 8},
        {"min": 151, "max": 300, "samples": 11},
        {"min": 301, "max": 450, "samples": 15},
    ],
    "calc_constants": {"HR": 7.89, "CHEQUERED": 8.1},
    "tolerance_profiles": {
        "wheels_india": {"width_under": 15, "width_over": 15, "width_threshold": 1500,
                         "thickness": 0.2, "length": 3, "diagonal": 3},
        "other": {"width_under": 20, "width_over": 25, "width_threshold": 1500,
                  "thickness": 0.5, "length": 10, "diagonal": 5},
    },
    "pdi_tolerance": {"thickness": 0.5, "width": 5, "length": 10},
}


async def load_settings() -> dict:
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))  # deep copy
    try:
        token = await graph.get_token()
        text = await graph.read_text(token, SETTINGS_PATH)
        if text:
            merged.update(json.loads(text))
    except Exception as e:
        print("[settings-load]", e)
    return merged


async def save_settings(settings: dict) -> dict:
    current = await load_settings()
    current.update(settings or {})
    token = await graph.get_token()
    # Graph PUT to root:/<path>:/content auto-creates the Config folder.
    await graph.upload_file(token, SETTINGS_PATH,
                            json.dumps(current, indent=2).encode("utf-8"), "application/json")
    return current


@router.get("/settings")
async def get_settings(emp: dict = Depends(require_employee)):
    return JSONResponse(await load_settings(),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


class SettingsIn(BaseModel):
    settings: dict


@router.post("/settings/save")
async def post_settings(body: SettingsIn, emp: dict = Depends(require_employee)):
    await _admin_guard(emp)
    if not isinstance(body.settings, dict):
        raise HTTPException(400, "invalid settings")
    merged = await save_settings(body.settings)
    return {"ok": True, "settings": merged}


# ============================================================
#  Users / employees management (shared `employees` table)
# ============================================================
def _is_admin(emp: dict) -> bool:
    return bool(emp.get("is_admin"))


async def _admin_guard(emp: dict):
    """Allow admins; also allow any employee while NO admin exists yet (bootstrap)."""
    if _is_admin(emp):
        return
    try:
        n = await db.fetchval("SELECT COUNT(*) FROM employees WHERE COALESCE(is_admin,false)=true")
    except Exception:
        n = 1
    if int(n or 0) == 0:
        return
    raise HTTPException(403, "admin only")


_emp_cols_ok = False


async def _ensure_emp_cols():
    """Add the maintenance-user flag to the shared employees table (once)."""
    global _emp_cols_ok
    if _emp_cols_ok:
        return
    try:
        await db.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_maint BOOLEAN DEFAULT false")
    except Exception as e:
        print("[admin] is_maint column add skipped:", e)
    _emp_cols_ok = True


@router.get("/users")
async def users_list(emp: dict = Depends(require_employee)):
    await _ensure_emp_cols()
    try:
        rows = await db.fetch(
            "SELECT id, emp_no, name, COALESCE(mobile,'') AS mobile, COALESCE(active,true) AS active, "
            "COALESCE(is_admin,false) AS is_admin, COALESCE(is_maint,false) AS is_maint, "
            "COALESCE(token_version,0) AS token_version "
            "FROM employees ORDER BY lower(name) NULLS LAST, emp_no")
    except Exception as e:
        # is_maint column may not exist yet (ALTER blocked) — fall back without it
        print("[admin] users_list is_maint fallback:", e)
        rows = await db.fetch(
            "SELECT id, emp_no, name, COALESCE(mobile,'') AS mobile, COALESCE(active,true) AS active, "
            "COALESCE(is_admin,false) AS is_admin, false AS is_maint, "
            "COALESCE(token_version,0) AS token_version "
            "FROM employees ORDER BY lower(name) NULLS LAST, emp_no")
    return JSONResponse([dict(r) for r in rows], headers={"Cache-Control": "no-store"})


class UserIn(BaseModel):
    id: Optional[int] = None
    emp_no: str
    name: Optional[str] = ""
    mobile: Optional[str] = ""
    is_admin: Optional[bool] = False
    active: Optional[bool] = True
    password: Optional[str] = ""   # only on create, or when explicitly changing


@router.post("/users/save")
async def users_save(body: UserIn, emp: dict = Depends(require_employee)):
    # The maintenance-user flag is managed in its own popup (/users/maint-toggle),
    # so this save never touches is_maint and can't clobber it.
    await _admin_guard(emp)
    en = (body.emp_no or "").strip()
    if not en:
        raise HTTPException(400, "emp_no required")
    try:
        if body.id:
            await db.execute(
                "UPDATE employees SET emp_no=$1, name=$2, mobile=$3, is_admin=$4, active=$5 WHERE id=$6",
                en, (body.name or "").strip(), (body.mobile or "").strip(),
                bool(body.is_admin), bool(body.active), body.id)
            if body.password:
                await db.execute(
                    "UPDATE employees SET password_hash=$1, token_version=COALESCE(token_version,0)+1 WHERE id=$2",
                    hash_pw(body.password), body.id)
            return {"ok": True, "id": body.id}
        # create
        if not body.password:
            raise HTTPException(400, "password required for a new user")
        exists = await db.fetchval("SELECT id FROM employees WHERE lower(emp_no)=lower($1)", en)
        if exists:
            raise HTTPException(400, "an employee with that code already exists")
        new_id = await db.fetchval(
            "INSERT INTO employees (emp_no, name, mobile, password_hash, is_admin, active, token_version) "
            "VALUES ($1,$2,$3,$4,$5,true,0) RETURNING id",
            en, (body.name or "").strip(), (body.mobile or "").strip(),
            hash_pw(body.password), bool(body.is_admin))
        return {"ok": True, "id": new_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"DB error: {e}")


# ============================================================
#  Customers list (read-only, from the legacy `customers` table)
# ============================================================
@router.get("/customers")
async def customers_list(emp: dict = Depends(require_employee)):
    await _admin_guard(emp)
    return JSONResponse(await CA.list_all(), headers={"Cache-Control": "no-store"})


class UserPwIn(BaseModel):
    id: int
    password: str


@router.post("/users/reset")
async def users_reset(body: UserPwIn, emp: dict = Depends(require_employee)):
    await _admin_guard(emp)
    if not body.password or len(body.password) < 4:
        raise HTTPException(400, "password too short")
    await db.execute(
        "UPDATE employees SET password_hash=$1, token_version=COALESCE(token_version,0)+1 WHERE id=$2",
        hash_pw(body.password), body.id)
    return {"ok": True}


class UserToggleIn(BaseModel):
    id: int
    active: bool


@router.post("/users/toggle")
async def users_toggle(body: UserToggleIn, emp: dict = Depends(require_employee)):
    await _admin_guard(emp)
    # bumping token_version signs the user out everywhere when deactivated
    await db.execute(
        "UPDATE employees SET active=$1, token_version=COALESCE(token_version,0)+1 WHERE id=$2",
        bool(body.active), body.id)
    return {"ok": True}


class MaintToggleIn(BaseModel):
    id: int
    is_maint: bool


@router.post("/users/maint-toggle")
async def users_maint_toggle(body: MaintToggleIn, emp: dict = Depends(require_employee)):
    """Mark / unmark an employee as a maintenance user (from the Users popup)."""
    await _admin_guard(emp)
    await _ensure_emp_cols()
    try:
        await db.execute("UPDATE employees SET is_maint=$1 WHERE id=$2", bool(body.is_maint), body.id)
    except Exception as e:
        # column still absent — add it then retry once
        print("[admin] maint-toggle retry:", e)
        await db.execute("ALTER TABLE employees ADD COLUMN IF NOT EXISTS is_maint BOOLEAN DEFAULT false")
        await db.execute("UPDATE employees SET is_maint=$1 WHERE id=$2", bool(body.is_maint), body.id)
    return {"ok": True}
