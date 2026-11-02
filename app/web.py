"""Shared template setup and request helpers."""
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import get_db
from .models import COMPANIES, COMPANY_NAMES, AuditLog, User
from .security import current_user

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(COMPANIES=COMPANIES, COMPANY_NAMES=COMPANY_NAMES)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))[:64]


def audit(db: Session, request: Request, action: str, user: User | None = None,
          app_key: str | None = None, email: str | None = None, **details) -> None:
    db.add(AuditLog(action=action, user_id=user.id if user else None,
                    email=(user.email if user else email), app_key=app_key,
                    ip=client_ip(request), details=details or None))


class RedirectTo(Exception):
    """Raised from a dependency to send the browser elsewhere (login, forced password change)."""
    def __init__(self, url: str):
        self.url = url


def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not user:
        raise RedirectTo(f"/login?next={quote(here)}")
    if user.must_reset_password and request.url.path != "/change-password":
        raise RedirectTo(f"/change-password?next={quote(here)}")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


__all__ = ["RedirectTo", "templates", "audit", "require_user", "require_admin", "get_db"]
