"""The launcher and the one-click hand-off into each app."""
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import COMPANIES, App, AppAccess, User
from ..security import issue_handoff
from ..web import audit, require_user, templates

router = APIRouter()


def apps_for(db: Session, user: User) -> list[tuple[App, AppAccess | None]]:
    """Apps this person may open, each with their access row (holds their in-app ID)."""
    q = (db.query(App, AppAccess)
         .outerjoin(AppAccess, (AppAccess.app_id == App.id) & (AppAccess.user_id == user.id))
         .filter(App.is_active.is_(True)))
    if not user.is_superadmin:
        q = q.filter(or_(App.open_to_all.is_(True), AppAccess.id.isnot(None)))
    return q.order_by(App.sort_order, App.name).all()


@router.get("/health")
def health():
    return JSONResponse({"ok": True})


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = apps_for(db, user)
    groups = []
    for code, label in COMPANIES:
        items = [a for a, _ in rows if a.company == code]
        if items:
            groups.append({"code": code, "label": label, "apps": items})
    return templates.TemplateResponse(request, "home.html",
                                      {"user": user, "groups": groups, "count": len(rows)})


@router.get("/launch/{key}")
def launch(key: str, request: Request, next: str = "", user: User = Depends(require_user),
           db: Session = Depends(get_db)):
    match = next_row = None
    for app, acc in apps_for(db, user):
        if app.key == key:
            match, next_row = app, acc
            break
    if not match:
        exists = db.query(App).filter(App.key == key, App.is_active.is_(True)).first()
        return templates.TemplateResponse(
            request, "message.html",
            {"user": user, "title": "No access" if exists else "App not found",
             "body": ("You don't have access to this app yet. Ask the IT team to add it to your account."
                      if exists else "This app is not in the group catalogue.")},
            status_code=403 if exists else 404)

    app = match
    if app.launch_mode == "sso" and app.sso_url and app.sso_secret:
        token = issue_handoff(user, app, next_row.external_id if next_row else None)
        params = {"token": token}
        if next.startswith("/") and not next.startswith("//"):
            params["next"] = next
        audit(db, request, "launch", user=user, app_key=app.key, mode="sso")
        db.commit()
        return RedirectResponse(f"{app.sso_url}?{urlencode(params)}", status_code=303)

    audit(db, request, "launch", user=user, app_key=app.key, mode="link")
    db.commit()
    return RedirectResponse(app.url, status_code=303)
