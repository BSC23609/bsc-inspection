"""Superadmin screens: people, app access, the app catalogue, and the activity log."""
import csv
import io
import re

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import lookup
from ..config import get_settings
from ..database import get_db
from ..models import COMPANIES, COMPANY_NAMES, App, AppAccess, AuditLog, User
from ..security import hash_password, new_app_secret, temp_password
from ..web import audit, require_admin, templates

router = APIRouter(prefix="/admin")
ICONS = ["grid", "ticket", "calc", "shield", "id", "chat", "chart", "lock", "truck", "box",
         "users", "clipboard", "wrench", "doc", "cart", "globe"]
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


CO_ORDER = {code: i for i, (code, _) in enumerate(COMPANIES)}


def _ordered_apps(db: Session) -> list[App]:
    return sorted(db.query(App).all(), key=lambda a: (CO_ORDER.get(a.company, 99), a.sort_order, a.name.lower()))


def _company(v: str) -> str:
    v = (v or "").strip().upper()
    return v if v in COMPANY_NAMES else "GROUP"


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


# ======================= people =======================
@router.get("", include_in_schema=False)
def admin_root():
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request, q: str = "", company: str = "", status: str = "", err: str = "", admin: User = Depends(require_admin),
               db: Session = Depends(get_db)):
    query = db.query(User)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(or_(func.lower(User.name).like(like), func.lower(User.email).like(like),
                                 func.lower(User.employee_code).like(like)))
    if company in COMPANY_NAMES:
        query = query.filter(User.company == company)
    if status in ("active", "inactive"):
        query = query.filter(User.is_active.is_(status == "active"))
    users = query.order_by(User.is_active.desc(), User.name).all()
    counts = dict(db.query(AppAccess.user_id, func.count(AppAccess.id)).group_by(AppAccess.user_id).all())
    matchable = [a.name for a in _ordered_apps(db) if lookup.is_configured(a.key)]
    return templates.TemplateResponse(request, "admin/users.html",
                                      {"user": admin, "users": users, "counts": counts, "q": q,
                                       "company": company, "status": status, "err": err,
                                       "common_pw": get_settings().common_password, "tab": "users", "matchable": matchable,
                                       "back": str(request.url.path) + (f"?{request.url.query}" if request.url.query else "")})


def _user_form_ctx(db: Session, admin: User, target: User | None, err: str | None = None, form: dict | None = None):
    grants = {a.app_id: a for a in target.access} if target else {}
    # Hidden apps only appear for someone who still has access to them (so it can be removed).
    apps = [a for a in _ordered_apps(db) if a.is_active or a.id in grants]
    lookup_on = {a.id for a in apps if lookup.is_configured(a.key)}
    return {"user": admin, "target": target, "apps": apps, "grants": grants, "err": err,
            "form": form or {}, "tab": "users", "lookup_on": lookup_on}


@router.get("/users/new", response_class=HTMLResponse)
def user_new(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "admin/user_form.html", _user_form_ctx(db, admin, None))


def _auto_id(app: App, target: User) -> str | None:
    """Look the person up in that app's own database (by email, then employee code)."""
    m = lookup.find(app.key, email=target.email, code=target.employee_code, name=None, phone=target.phone)
    return m.external_id if m else None


async def _apply_access(request: Request, db: Session, target: User) -> None:
    form = await request.form()
    existing = {a.app_id: a for a in target.access}
    for app in db.query(App).all():
        want = form.get(f"app_{app.id}") == "on"
        ext = _clean(form.get(f"ext_{app.id}"))
        if want and not ext:
            ext = _auto_id(app, target)
        row = existing.get(app.id)
        if want and not row:
            target.access.append(AppAccess(app_id=app.id, external_id=ext))
        elif want and row:
            row.external_id = ext
        elif not want and row:
            target.access.remove(row)


@router.post("/lookup-ids")
async def lookup_ids(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """'Find in apps' button: returns, per app, the person's ID in that app's own database."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower() or None
    code = (form.get("employee_code") or "").strip() or None
    name = (form.get("name") or "").strip() or None
    phone = (form.get("phone") or "").strip() or None
    out = {}
    for app in db.query(App).all():
        if not lookup.is_configured(app.key):
            out[app.id] = {"state": "off"}
            continue
        m = lookup.find(app.key, email=email, code=code, name=name, allow_name=True, phone=phone)
        out[app.id] = ({"state": "found", "id": m.external_id, "name": m.name, "how": m.how}
                       if m else {"state": "missing", "note": lookup.last_note(app.key)})
    return JSONResponse(out)


@router.post("/users-match")
def users_match(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Match everyone at once: fill blank IDs and give access wherever the person exists in an app."""
    apps = [a for a in _ordered_apps(db) if lookup.is_configured(a.key)]
    filled = granted = 0
    per_app = {a.id: 0 for a in apps}
    for person in db.query(User).filter(User.is_active.is_(True)).all():
        rows = {r.app_id: r for r in person.access}
        for app in apps:
            row = rows.get(app.id)
            if row and row.external_id:
                continue
            m = lookup.find(app.key, email=person.email, code=person.employee_code, name=None, phone=person.phone)
            if not m:
                continue
            if row:
                row.external_id = m.external_id
                filled += 1
            else:
                person.access.append(AppAccess(app_id=app.id, external_id=m.external_id))
                granted += 1
            per_app[app.id] += 1
    audit(db, request, "admin_users_matched", user=admin, filled=filled, granted=granted)
    db.commit()
    return templates.TemplateResponse(request, "admin/match_result.html",
                                      {"user": admin, "tab": "users", "apps": apps, "per_app": per_app,
                                       "filled": filled, "granted": granted})


def _validate_user(db: Session, email: str, code: str | None, name: str, exclude_id: int | None) -> str | None:
    if not EMAIL_RE.match(email):
        return "Enter a valid email address."
    if not name.strip():
        return "Name is required."
    clash = db.query(User).filter(func.lower(User.email) == email)
    if exclude_id:
        clash = clash.filter(User.id != exclude_id)
    if clash.first():
        return "Another person already uses this email."
    if code:
        clash = db.query(User).filter(func.upper(User.employee_code) == code.upper())
        if exclude_id:
            clash = clash.filter(User.id != exclude_id)
        if clash.first():
            return "Another person already uses this employee code."
    return None


@router.post("/users/new")
async def user_create(request: Request, email: str = Form(...), name: str = Form(...),
                      employee_code: str = Form(""), company: str = Form("GROUP"),
                      department: str = Form(""), phone: str = Form(""),
                      is_superadmin: str = Form(""), admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    email = email.strip().lower()
    code = _clean(employee_code)
    err = _validate_user(db, email, code, name, None)
    if err:
        return templates.TemplateResponse(request, "admin/user_form.html",
                                          _user_form_ctx(db, admin, None, err, dict(await request.form())),
                                          status_code=400)
    pw = temp_password()
    target = User(email=email, name=name.strip(), employee_code=code.upper() if code else None,
                  company=_company(company), department=_clean(department), phone=_clean(phone),
                  is_superadmin=is_superadmin == "on", password_hash=hash_password(pw), must_reset_password=True)
    db.add(target)
    db.flush()
    await _apply_access(request, db, target)
    audit(db, request, "admin_user_created", user=admin, target=email)
    db.commit()
    return templates.TemplateResponse(request, "admin/credential.html",
                                      {"user": admin, "target": target, "password": pw, "tab": "users",
                                       "headline": "Account created"})


@router.get("/users/{uid}", response_class=HTMLResponse)
def user_edit(uid: int, request: Request, saved: int = 0, admin: User = Depends(require_admin),
              db: Session = Depends(get_db)):
    target = db.get(User, uid)
    if not target:
        return RedirectResponse("/admin/users", status_code=303)
    ctx = _user_form_ctx(db, admin, target)
    ctx["saved"] = bool(saved)
    return templates.TemplateResponse(request, "admin/user_form.html", ctx)


@router.post("/users/{uid}")
async def user_update(uid: int, request: Request, email: str = Form(...), name: str = Form(...),
                      employee_code: str = Form(""), company: str = Form("GROUP"),
                      department: str = Form(""), phone: str = Form(""),
                      is_superadmin: str = Form(""), admin: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    target = db.get(User, uid)
    if not target:
        return RedirectResponse("/admin/users", status_code=303)
    email = email.strip().lower()
    code = _clean(employee_code)
    err = _validate_user(db, email, code, name, uid)
    make_admin = is_superadmin == "on"
    if not err and target.id == admin.id and not make_admin:
        err = "You can't remove your own admin rights."
    if err:
        return templates.TemplateResponse(request, "admin/user_form.html",
                                          _user_form_ctx(db, admin, target, err), status_code=400)
    target.email, target.name = email, name.strip()
    target.employee_code = code.upper() if code else None
    target.company, target.department, target.phone = _company(company), _clean(department), _clean(phone)
    target.is_superadmin = make_admin
    await _apply_access(request, db, target)
    audit(db, request, "admin_user_updated", user=admin, target=email)
    db.commit()
    return RedirectResponse(f"/admin/users/{uid}?saved=1", status_code=303)


@router.post("/users/{uid}/reset-password")
async def user_reset_password(uid: int, request: Request, admin: User = Depends(require_admin),
                              db: Session = Depends(get_db)):
    target = db.get(User, uid)
    if not target:
        return RedirectResponse("/admin/users", status_code=303)
    # Like QMS "Reset PW": the admin may type the temporary password; blank = the common one.
    typed = str((await request.form()).get("password") or "").strip()
    if typed and (len(typed) < 8 or typed.isdigit() or typed.isalpha()):
        return RedirectResponse("/admin/users?err=pw", status_code=303)
    pw = typed or temp_password()
    target.password_hash = hash_password(pw)
    target.must_reset_password = True
    target.failed_logins, target.locked_until = 0, None
    target.session_version = (target.session_version or 1) + 1
    audit(db, request, "admin_password_reset", user=admin, target=target.email)
    db.commit()
    return templates.TemplateResponse(request, "admin/credential.html",
                                      {"user": admin, "target": target, "password": pw, "tab": "users",
                                       "headline": "Password reset"})


@router.post("/users/{uid}/toggle-active")
async def user_toggle(uid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    back = str((await request.form()).get("back") or "")
    target = db.get(User, uid)
    if target and target.id != admin.id:
        target.is_active = not target.is_active
        target.session_version = (target.session_version or 1) + 1
        audit(db, request, "admin_user_activated" if target.is_active else "admin_user_deactivated",
              user=admin, target=target.email)
        db.commit()
    if back == "/admin/users" or back.startswith("/admin/users?"):  # came from the people list
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/admin/users/{uid}?saved=1", status_code=303)


# -------- bulk import (CSV) --------
IMPORT_COLUMNS = ["email", "employee_code", "name", "company", "department", "phone", "apps"]


@router.get("/users-import", response_class=HTMLResponse)
def import_page(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    apps = _ordered_apps(db)
    return templates.TemplateResponse(request, "admin/import.html",
                                      {"user": admin, "apps": apps, "columns": IMPORT_COLUMNS, "tab": "users"})


@router.get("/users-import/template.csv")
def import_template(admin: User = Depends(require_admin)):
    body = ",".join(IMPORT_COLUMNS) + "\n" + \
        "ravi@metfraa.com,MET66,Ravi Kumar,METFRAA,Design,9840000000,metfraa-portal;metfraa-costing\n" + \
        "anita@bharatsteels.in,BSC104,Anita S,BSC,Accounts,,bsc-portal=1042;qms\n"
    return Response(body, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="hub-users-template.csv"'})


@router.post("/users-import")
async def import_run(request: Request, file: UploadFile = File(...), admin: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    """Create new people from a CSV. Existing emails get their app grants added, never passwords changed.
    Returns a CSV of the new temporary passwords — the only time they are ever shown."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    apps = {a.key: a for a in db.query(App).all()}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["row", "email", "name", "result", "temporary_password", "notes"])
    created = updated = failed = 0
    for i, raw in enumerate(reader, start=2):
        r = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        email, name, code = r.get("email", "").lower(), r.get("name", ""), _clean(r.get("employee_code"))
        notes = []
        if not EMAIL_RE.match(email) or not name:
            w.writerow([i, email, name, "SKIPPED", "", "email and name are required"])
            failed += 1
            continue
        target = db.query(User).filter(func.lower(User.email) == email).first()
        pw = ""
        if target is None:
            if code and db.query(User).filter(func.upper(User.employee_code) == code.upper()).first():
                w.writerow([i, email, name, "SKIPPED", "", f"employee code {code} already used"])
                failed += 1
                continue
            pw = temp_password()
            target = User(email=email, name=name, employee_code=code.upper() if code else None,
                          company=_company(r.get("company")), department=_clean(r.get("department")),
                          phone=_clean(r.get("phone")), password_hash=hash_password(pw), must_reset_password=True)
            db.add(target)
            db.flush()
            result = "CREATED"
            created += 1
        else:
            result = "EXISTS - access added"
            updated += 1
        have = {a.app_id for a in target.access}
        for part in [p.strip() for p in r.get("apps", "").replace(",", ";").split(";") if p.strip()]:
            key, _, ext = part.partition("=")
            app = apps.get(key.strip().lower())
            if not app:
                notes.append(f"unknown app '{key.strip()}'")
                continue
            if app.id not in have:
                target.access.append(AppAccess(app_id=app.id, external_id=_clean(ext)))
                have.add(app.id)
        w.writerow([i, email, name, result, pw, "; ".join(notes)])
    audit(db, request, "admin_bulk_import", user=admin, created=created, updated=updated, skipped=failed)
    db.commit()
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="hub-import-result.csv"'})


# ======================= apps =======================
@router.get("/apps", response_class=HTMLResponse)
def apps_list(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    apps = _ordered_apps(db)
    counts = dict(db.query(AppAccess.app_id, func.count(AppAccess.id)).group_by(AppAccess.app_id).all())
    return templates.TemplateResponse(request, "admin/apps.html",
                                      {"user": admin, "apps": apps, "counts": counts, "tab": "apps"})


def _app_ctx(admin, app, err=None, form=None, saved=False):
    ctx = {"user": admin, "app": app, "icons": ICONS, "err": err, "form": form or {}, "tab": "apps",
           "saved": saved}
    if app:
        ctx["lookup_env"] = lookup.env_name(app.key)
        ctx["lookup_state"], ctx["lookup_msg"] = lookup.status(app.key)
    return ctx


@router.get("/apps/new", response_class=HTMLResponse)
def app_new(request: Request, admin: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin/app_form.html", _app_ctx(admin, None))


def _read_app_form(form) -> tuple[dict, str | None]:
    d = {
        "key": (form.get("key") or "").strip().lower(),
        "name": (form.get("name") or "").strip(),
        "description": _clean(form.get("description")),
        "company": _company(form.get("company")),
        "url": (form.get("url") or "").strip(),
        "launch_mode": "sso" if form.get("launch_mode") == "sso" else "link",
        "sso_url": _clean(form.get("sso_url")),
        "identity_field": "email" if form.get("identity_field") == "email" else "employee_code",
        "icon": form.get("icon") if form.get("icon") in ICONS else "grid",
        "open_to_all": form.get("open_to_all") == "on",
        "is_active": form.get("is_active") == "on",
        "sort_order": int(form.get("sort_order") or 100) if str(form.get("sort_order") or "100").isdigit() else 100,
    }
    if not KEY_RE.match(d["key"]):
        return d, "Key: lowercase letters, numbers and dashes (e.g. metfraa-portal)."
    if not d["name"]:
        return d, "Name is required."
    if not d["url"].startswith("https://"):
        return d, "App address must start with https://"
    if d["launch_mode"] == "sso" and not (d["sso_url"] or "").startswith("https://"):
        return d, "Single sign-on needs the app's sign-on address (https://…/auth/sso)."
    return d, None


@router.post("/apps/new")
async def app_create(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    d, err = _read_app_form(form)
    if not err and db.query(App).filter(App.key == d["key"]).first():
        err = "That key is already used by another app."
    if err:
        return templates.TemplateResponse(request, "admin/app_form.html", _app_ctx(admin, None, err, dict(form)),
                                          status_code=400)
    app = App(**d, sso_secret=new_app_secret())
    db.add(app)
    audit(db, request, "admin_app_created", user=admin, app_key=app.key)
    db.commit()
    return RedirectResponse(f"/admin/apps/{app.id}?saved=1", status_code=303)


@router.get("/apps/{aid}", response_class=HTMLResponse)
def app_edit(aid: int, request: Request, saved: int = 0, admin: User = Depends(require_admin),
             db: Session = Depends(get_db)):
    app = db.get(App, aid)
    if not app:
        return RedirectResponse("/admin/apps", status_code=303)
    return templates.TemplateResponse(request, "admin/app_form.html", _app_ctx(admin, app, saved=bool(saved)))


@router.post("/apps/{aid}")
async def app_update(aid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    app = db.get(App, aid)
    if not app:
        return RedirectResponse("/admin/apps", status_code=303)
    form = await request.form()
    d, err = _read_app_form(form)
    if not err and d["key"] != app.key and db.query(App).filter(App.key == d["key"]).first():
        err = "That key is already used by another app."
    if err:
        return templates.TemplateResponse(request, "admin/app_form.html", _app_ctx(admin, app, err), status_code=400)
    for k, v in d.items():
        setattr(app, k, v)
    if not app.sso_secret:
        app.sso_secret = new_app_secret()
    audit(db, request, "admin_app_updated", user=admin, app_key=app.key)
    db.commit()
    return RedirectResponse(f"/admin/apps/{aid}?saved=1", status_code=303)


@router.post("/apps/{aid}/rotate-secret")
def app_rotate(aid: int, request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    app = db.get(App, aid)
    if app:
        app.sso_secret = new_app_secret()
        audit(db, request, "admin_app_secret_rotated", user=admin, app_key=app.key)
        db.commit()
    return RedirectResponse(f"/admin/apps/{aid}?saved=1", status_code=303)


# ======================= activity =======================
@router.get("/activity", response_class=HTMLResponse)
def activity(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(300).all()
    return templates.TemplateResponse(request, "admin/activity.html",
                                      {"user": admin, "rows": rows, "tab": "activity"})


# -------- WhatsApp / email check for "Forgot password" --------
@router.get("/message-test")
def message_test(phone: str = "", email: str = "", admin: User = Depends(require_admin)):
    """Sends test code 123456 and shows exactly what WATI / the mail server answered."""
    from .. import notify
    out = {"whatsapp_settings": {"WATI_BASE_URL": notify.wati_base() or "MISSING",
                                 "WATI_TOKEN": "set" if notify._env("WATI_TOKEN") else "MISSING",
                                 "template": notify._env("WATI_OTP_TEMPLATE") or "otp_password2"},
           "email_settings": {"SMTP_HOST": notify._env("SMTP_HOST") or "MISSING"}}
    phone = phone or admin.phone or ""
    out["whatsapp_to"] = notify.wa_number(phone) or "no mobile number (add ?phone=9840000000)"
    if notify.whatsapp_ready() and notify.wa_number(phone):
        ok, why = notify.send_whatsapp_code(phone, "123456")
        out["whatsapp_result"] = "SENT" if ok else f"FAILED: {why}"
    if notify.email_ready():
        ok, why = notify.send_email_code(email or admin.email, admin.name, "123456")
        out["email_result"] = "SENT" if ok else f"FAILED: {why}"
    return out
