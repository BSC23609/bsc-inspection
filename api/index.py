"""Vercel entrypoint — exposes the FastAPI ASGI app as `app`."""
import os
import sys

# make the project root importable (…/app, …/templates, …/static)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import APP_NAME
from app.routers import pages, dg, coil, quality, shearing, pdi, eightd, complaint, admin, customer, pwa, forgot, maint, analysis

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
app.include_router(analysis.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "app": APP_NAME}

# ---- TEMPORARY (DB migration): delete this block after copying the database URL ----
import hashlib as _rv_hashlib
import os as _rv_os
import secrets as _rv_secrets
from fastapi import Request as _RvRequest
from fastapi.responses import JSONResponse as _RvJSON

_RV_TOKEN_SHA256 = "e0d292e439e260caf37d4047fed93fad6704c831d7cd54531644120b001bbed6"


@app.get("/api/reveal-db", include_in_schema=False)
def _reveal_db(request: _RvRequest):
    given = request.headers.get("x-reveal-token", "")
    digest = _rv_hashlib.sha256(given.encode()).hexdigest()
    if not given or not _rv_secrets.compare_digest(digest, _RV_TOKEN_SHA256):
        return _RvJSON({"detail": "Not Found"}, status_code=404)
    keys = ("DATABASE", "POSTGRES", "NEON", "PG")
    return _RvJSON({k: v for k, v in _rv_os.environ.items()
                    if any(s in k.upper() for s in keys) and "RIPGREP" not in k.upper()},
                   headers={"Cache-Control": "no-store"})



# The WhatsApp maintenance reminder's button links to qms.bharatsteels.in/<token>
# (the template's dynamic URL button). The token is a URL-safe staff name, not a
# real page, so anyone who taps it would hit "Not Found". Send them to the
# Maintenance Board instead. Declared LAST so it only catches single-segment
# paths that no real route (login, /, /coil, /maint, /static, …) already handles.
@app.get("/{slug}")
async def _reminder_button_redirect(slug: str):
    return RedirectResponse("/maint", status_code=307)
