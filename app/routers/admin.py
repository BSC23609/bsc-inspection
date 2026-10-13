"""Admin / Settings — reads and writes the same OneDrive config the legacy app
used (BSC Inspections/Config/settings.json). These settings feed the inspection
forms (inspector lists, mills, grades, rejection types, sampling, tolerances)."""
import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import graph, db
from ..auth import require_employee, hash_pw
from ..config import ONEDRIVE_BASE

router = APIRouter(prefix="/admin", tags=["admin"])

SETTINGS_PATH = f"{ONEDRIVE_BASE}/Config/settings.json"

DEFAULT_SETTINGS = {
    "inspectors_inward": ["Sathya", "Kumar", "Mahendran", "Dhanush"],
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


@router.get("/users")
async def users_list(emp: dict = Depends(require_employee)):
    rows = await db.fetch(
        "SELECT id, emp_no, name, COALESCE(mobile,'') AS mobile, COALESCE(active,true) AS active, "
        "COALESCE(is_admin,false) AS is_admin, COALESCE(token_version,0) AS token_version "
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
