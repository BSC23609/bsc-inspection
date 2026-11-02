"""Passwords, the hub session cookie, and the short-lived hand-off token each app trusts."""
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_db
from .models import App, User

settings = get_settings()
SESSION_COOKIE = "bsg_session"
ALG = "HS256"


# ---------------- passwords ----------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), (hashed or "").encode("utf-8"))
    except ValueError:
        return False


def password_problem(pw: str) -> str | None:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw.isdigit() or pw.isalpha():
        return "Use a mix of letters and numbers."
    from .config import get_settings
    if pw == get_settings().common_password:
        return "Choose your own password, not the common starting one."
    return None


def temp_password() -> str:
    """The group's common starting password; the person must change it on first sign-in."""
    from .config import get_settings
    return get_settings().common_password


def random_password() -> str:
    # Readable, no look-alike characters.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10)) + str(secrets.randbelow(10))


def new_app_secret() -> str:
    return secrets.token_urlsafe(48)


# ---------------- hub session ----------------
def issue_session(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "sv": user.session_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.session_days)).timestamp()),
    }
    return jwt.encode(payload, settings.session_secret, algorithm=ALG)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=settings.session_days * 86400,
        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """The signed-in person, or None. Re-reads the row so deactivation bites immediately."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = jwt.decode(raw, settings.session_secret, algorithms=[ALG])
    except jwt.PyJWTError:
        return None
    user = db.get(User, int(data.get("sub", 0) or 0))
    if not user or not user.is_active or user.session_version != data.get("sv"):
        return None
    return user


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths — never //evil.com or absolute URLs."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


# ---------------- hand-off token (hub → app) ----------------
def identity_for(user: User, app: App, external_id: str | None) -> str:
    if external_id:
        return external_id.strip()
    if app.identity_field == "email":
        return user.email
    return user.employee_code or user.email


def issue_handoff(user: User, app: App, external_id: str | None) -> str:
    """One-minute token signed with THIS app's own secret, addressed to THIS app only.

    Claims mirror the existing bsc-portal → QMS hand-off (emp_no, name) so apps that
    already speak that format need almost no change.
    """
    now = datetime.now(timezone.utc)
    ident = identity_for(user, app, external_id)
    payload = {
        "iss": settings.issuer,
        "aud": app.key,
        "sub": ident,
        "emp_no": ident,
        "email": user.email,
        "name": user.name,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.handoff_ttl)).timestamp()),
    }
    return jwt.encode(payload, app.sso_secret, algorithm=ALG)
