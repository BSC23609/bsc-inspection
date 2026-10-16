"""Vercel entrypoint — exposes the FastAPI ASGI app as `app`."""
import os
import sys

# make the project root importable (…/app, …/templates, …/static)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME
from app.routers import pages, dg, coil, quality, shearing, pdi, eightd, complaint, admin, customer, pwa, forgot, maint, onedrive_sync

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None)

# Static assets (Vercel also serves /static directly; this covers local runs)
_static_dir = os.path.join(_BASE, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

app.include_router(pages.router)
app.include_router(dg.router)
app.include_router(coil.router)
app.include_router(quality.router)
app.include_router(shearing.router)
app.include_router(pdi.router)
app.include_router(eightd.router)
app.include_router(complaint.router)
app.include_router(customer.router)
app.include_router(admin.router)
app.include_router(pwa.router)
app.include_router(forgot.router)
app.include_router(maint.router)
app.include_router(onedrive_sync.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "app": APP_NAME}
