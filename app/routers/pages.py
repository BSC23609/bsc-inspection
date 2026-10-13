"""Server-rendered pages (Jinja2) — login, dashboard and the module screens."""
import os
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..auth import current_employee, authenticate_employee, make_token, set_cookie, clear_cookie
from ..config import APP_NAME

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
templates = Jinja2Templates(directory=os.path.join(_BASE, "templates"))

# ---- the module map: one source of truth for the sidebar, dashboard and popups ----
NAV_GROUPS = [
    {"key": "inspection", "title": "Inspection", "accent": "steel", "icon": "inspect",
     "blurb": "Record line inspections from coil to delivery.",
     "items": [
         {"name": "Coil Inward", "icon": "coil", "desc": "Inspect mother coils on arrival.",
          "href": "/coil", "active_key": "coil"},
         {"name": "Quality Inspection", "icon": "ruler", "desc": "Cut-to-length quality and sheet measurements.",
          "href": "/quality", "active_key": "quality"},
         {"name": "Shearing Quality Inspection", "icon": "scissors", "desc": "Shearing quality, output sizes, reworks.",
          "href": "/shearing", "active_key": "shearing"},
         {"name": "Pre-Delivery Inspection", "icon": "truck", "desc": "PDI against each sales order.",
          "href": "/pdi", "active_key": "pdi"},
     ]},
    {"key": "complaints", "title": "Complaints", "accent": "navy", "icon": "complaints",
     "blurb": "Raise, investigate and close quality issues.",
     "items": [
         {"name": "8D Report", "icon": "warn", "desc": "Problem solving D1–D8, from team to closure, in one report.",
          "href": "/eightd", "active_key": "eightd"},
         {"name": "Quality Complaint", "icon": "msg", "desc": "Customer and internal complaints."},
     ]},
    {"key": "generator", "title": "Generator", "accent": "sky", "icon": "bolt",
     "blurb": "Daily diesel generator running records.",
     "items": [
         {"name": "Diesel Generator Log", "icon": "bolt", "desc": "Runs, running hours, diesel and kWh.",
          "href": "/dglog", "active_key": "dglog"},
     ]},
]

router = APIRouter()


def _ctx(request, emp, active, **extra):
    base = {"request": request, "emp": emp, "app_name": APP_NAME,
            "active": active, "groups": NAV_GROUPS}
    base.update(extra)
    return base


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await current_employee(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "app_name": APP_NAME, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, identifier: str = Form(...), password: str = Form(...)):
    emp = await authenticate_employee(identifier, password)
    if not emp:
        return templates.TemplateResponse(
            "login.html", {"request": request, "app_name": APP_NAME,
                           "error": "That employee code or password didn't match. Try again."},
            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    set_cookie(resp, make_token(emp, "employee"))
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    clear_cookie(resp)
    return resp


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("home.html", _ctx(request, emp, "dashboard"))


@router.get("/dglog", response_class=HTMLResponse)
async def dglog_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("dg_log.html", _ctx(request, emp, "dglog", section="Diesel Generator Log"))


@router.get("/coil", response_class=HTMLResponse)
async def coil_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("coil_inward.html", _ctx(request, emp, "coil", section="Coil Inward Inspection"))


@router.get("/quality", response_class=HTMLResponse)
async def quality_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("quality.html", _ctx(request, emp, "quality", section="CTL Quality Inspection"))


@router.get("/shearing", response_class=HTMLResponse)
async def shearing_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("shearing.html", _ctx(request, emp, "shearing", section="Shearing Quality Inspection"))


@router.get("/pdi", response_class=HTMLResponse)
async def pdi_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("pdi.html", _ctx(request, emp, "pdi", section="Pre-Delivery Inspection"))


@router.get("/eightd", response_class=HTMLResponse)
async def eightd_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("eightd.html", _ctx(request, emp, "eightd", section="8D Report"))
