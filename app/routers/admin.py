"""Admin settings API — shared QMS settings (form options, tolerances, email
routing) gated by the admin password, same contract as the Node app."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from ..auth import require_employee
from ..config import ADMIN_PASSWORD
from .. import settings_store

router = APIRouter(tags=["admin"])


@router.get("/settings")
async def get_settings(emp: dict = Depends(require_employee)):
    s = await settings_store.load_settings()
    return JSONResponse(jsonable_encoder(s),
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


class VerifyIn(BaseModel):
    password: str = ""


@router.post("/admin/verify")
async def admin_verify(body: VerifyIn, emp: dict = Depends(require_employee)):
    if body.password == ADMIN_PASSWORD:
        return {"ok": True}
    raise HTTPException(403, "Invalid password")


class SettingsIn(BaseModel):
    password: str = ""
    settings: dict = {}


@router.post("/admin/settings")
async def admin_settings(body: SettingsIn, emp: dict = Depends(require_employee)):
    if body.password != ADMIN_PASSWORD:
        raise HTTPException(403, "Invalid password")
    if not isinstance(body.settings, dict) or not body.settings:
        raise HTTPException(400, "Invalid settings")
    merged = await settings_store.save_settings(body.settings)
    return {"ok": True, "settings": jsonable_encoder(merged)}
