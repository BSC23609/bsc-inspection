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
         {"name": "Inspection Analysis", "icon": "gauge", "desc": "Quality across Inward, CTL, Shearing and PDI — supplier scorecard, defect Pareto, batch trace.",
          "href": "/analysis/inspection", "active_key": "analysis_inspection", "admin_only": True},
     ]},
    {"key": "complaints", "title": "Complaints", "accent": "navy", "icon": "complaints",
     "blurb": "Raise, investigate and close quality issues.",
     "items": [
         {"name": "8D Report", "icon": "warn", "desc": "Problem solving D1–D8, from team to closure, in one report.",
          "href": "/eightd", "active_key": "eightd"},
         {"name": "Quality Complaint", "icon": "msg", "desc": "Customer and internal complaints.",
          "href": "/complaint", "active_key": "complaint"},
     ]},
    {"key": "maintenance", "title": "Maintenance", "accent": "teal", "icon": "wrench",
     "blurb": "Assets, PM schedules, checklists and machine history.",
     "items": [
         {"name": "My Checklist", "icon": "clipboard", "desc": "The checkpoints assigned to you — tick them as you complete them.",
          "href": "/maint/my-checklist", "active_key": "maint_my"},
         {"name": "Analytics", "icon": "gauge", "desc": "MTTR, MTBF, availability, downtime and PM compliance.",
          "href": "/maint/analytics", "active_key": "maint_analytics"},
         {"name": "Maintenance Board", "icon": "gauge", "desc": "Due today, overdue and upcoming PM at a glance.",
          "href": "/maint", "active_key": "maint"},
         {"name": "Monitor", "icon": "clipboard", "desc": "All work orders with per-assignee status; mark absentees.",
          "href": "/maint/monitor", "active_key": "maint_monitor"},
         {"name": "Assets", "icon": "cube", "desc": "Machine / asset register with running hours.",
          "href": "/maint/assets", "active_key": "maint_assets"},
         {"name": "Checklists", "icon": "clipboard", "desc": "Per-asset check points, tagged by category.",
          "href": "/maint/checklists", "active_key": "maint_checklists"},
         {"name": "Checkpoint Assignment", "icon": "users", "desc": "Assign each checkpoint to a person; reassign work when staff change.",
          "href": "/maint/checkpoint-assignment", "active_key": "maint_cp_assign"},
         {"name": "Schedules", "icon": "calendar", "desc": "PM cycles by date interval and running hours.",
          "href": "/maint/schedules", "active_key": "maint_schedules"},
         {"name": "Raise Breakdown", "icon": "warn", "desc": "Raise an unplanned stoppage and alert the team.",
          "href": "/maint/breakdown", "active_key": "maint_breakdown"},
         {"name": "WhatsApp Log", "icon": "msg", "desc": "Every maintenance WhatsApp sent and its delivery result.",
          "href": "/maint/wati-log", "active_key": "maint_wati_log"},
         {"name": "Leave", "icon": "users", "desc": "Mark staff on leave so they skip reminders.",
          "href": "/maint/leave", "active_key": "maint_leave"},
     ]},
    {"key": "generator", "title": "Generator", "accent": "sky", "icon": "bolt",
     "blurb": "Daily diesel generator running records.",
     "items": [
         {"name": "Diesel Generator Log", "icon": "bolt", "desc": "Runs, running hours, diesel and kWh.",
          "href": "/dglog", "active_key": "dglog"},
     ]},
]

ADMIN_NAV = [
    {"name": "Users", "icon": "users", "href": "/users", "active_key": "users"},
    {"name": "Customers", "icon": "store", "href": "/customers", "active_key": "customers"},
    {"name": "Settings", "icon": "gear", "href": "/admin", "active_key": "admin"},
]

router = APIRouter()

# Maintenance sub-items a NON-admin may see. Admins see the whole group.
MAINT_PUBLIC_KEYS = {"maint_my", "maint", "maint_breakdown"}


def _is_admin(emp) -> bool:
    return bool(emp and emp.get("is_admin"))


def _nav_groups_for(emp):
    """Hide admin-only maintenance items from non-admins (sidebar, dashboard
    tiles and the module popup all render from this same list)."""
    if _is_admin(emp):
        return NAV_GROUPS
    out = []
    for g in NAV_GROUPS:
        g = dict(g)
        items = g["items"]
        if g.get("key") == "maintenance":
            items = [it for it in items if it.get("active_key") in MAINT_PUBLIC_KEYS]
        # hide any admin-only item (e.g. Inspection Analysis) from non-admins
        items = [it for it in items if not it.get("admin_only")]
        g["items"] = items
        out.append(g)
    return out


def _ctx(request, emp, active, **extra):
    base = {"request": request, "emp": emp, "app_name": APP_NAME,
            "active": active, "groups": _nav_groups_for(emp),
            "admin_nav": ADMIN_NAV if _is_admin(emp) else []}
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


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    if await current_employee(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("forgot.html", {"request": request, "app_name": APP_NAME})


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


@router.get("/analysis/inspection", response_class=HTMLResponse)
async def inspection_analysis_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    if not _is_admin(emp):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("analysis_inspection.html",
                                      _ctx(request, emp, "analysis_inspection", section="Inspection Analysis"))


@router.get("/eightd", response_class=HTMLResponse)
async def eightd_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("eightd.html", _ctx(request, emp, "eightd", section="8D Report"))


@router.get("/complaint", response_class=HTMLResponse)
async def complaint_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("complaint.html", _ctx(request, emp, "complaint", section="Quality Complaint"))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    if not _is_admin(emp):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("admin.html", _ctx(request, emp, "admin", section="Settings"))


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    if not _is_admin(emp):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("users.html", _ctx(request, emp, "users", section="Users"))


@router.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request):
    emp = await current_employee(request)
    if not emp:
        return RedirectResponse("/login", status_code=303)
    if not _is_admin(emp):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("customers.html", _ctx(request, emp, "customers", section="Customers"))


# ---- Maintenance module pages ----
# Last flag = admin_only. Non-admins may open only the Maintenance Board and
# Raise Breakdown; the rest redirect them back to the board even by direct URL.
_MAINT_PAGES = [
    ("/maint/my-checklist", "maint_my.html", "maint_my", "My Checklist", False),
    ("/maint/analytics", "maint_analytics.html", "maint_analytics", "Maintenance Analytics", True),
    ("/maint", "maint_board.html", "maint", "Maintenance Board", False),
    ("/maint/monitor", "maint_monitor.html", "maint_monitor", "Monitor", True),
    ("/maint/assets", "maint_assets.html", "maint_assets", "Assets", True),
    ("/maint/checklists", "maint_checklists.html", "maint_checklists", "Checklists", True),
    ("/maint/checkpoint-assignment", "maint_checkpoint_assign.html", "maint_cp_assign", "Checkpoint Assignment", True),
    ("/maint/schedules", "maint_schedules.html", "maint_schedules", "Schedules", True),
    ("/maint/breakdown", "maint_breakdown.html", "maint_breakdown", "Raise Breakdown", False),
    ("/maint/wati-log", "maint_wati_log.html", "maint_wati_log", "WhatsApp Log", True),
    ("/maint/leave", "maint_leave.html", "maint_leave", "Leave", True),
]


def _mk_maint_route(tmpl, active, section, admin_only=False):
    async def _page(request: Request):
        emp = await current_employee(request)
        if not emp:
            return RedirectResponse("/login", status_code=303)
        if admin_only and not _is_admin(emp):
            return RedirectResponse("/maint", status_code=303)
        return templates.TemplateResponse(tmpl, _ctx(request, emp, active, section=section))
    return _page


for _path, _tmpl, _active, _section, _admin in _MAINT_PAGES:
    router.add_api_route(_path, _mk_maint_route(_tmpl, _active, _section, _admin),
                         methods=["GET"], response_class=HTMLResponse)
