"""Authentication — deliberately identical on the wire to the current Node app:
same cookie name, same HS256 JWT_SECRET, same bcrypt password hashes, same
`employees` table. A user already signed into the Node app is signed into this one,
and vice-versa."""
import time
import bcrypt
import jwt
from fastapi import Request, HTTPException, Response
from . import db
from .config import JWT_SECRET, COOKIE_NAME, COOKIE_TTL


# ---- passwords (bcrypt, compatible with bcryptjs $2a/$2b hashes) ----
def verify_pw(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


# ---- JWT (same claims as jsonwebtoken: sub, kind, tv, exp) ----
def make_token(user: dict, kind: str) -> str:
    payload = {
        "sub": user["id"],
        "kind": kind,
        "tv": user.get("token_version", 0) or 0,
        "exp": int(time.time()) + COOKIE_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        # verify_sub=False keeps numeric `sub` claims valid — the Node app
        # (jsonwebtoken) signs sub as a number, and PyJWT >= 2.10 would otherwise
        # reject it. Harmless on the pinned 2.8.0, which ignores the extra option.
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"],
                          options={"verify_sub": False})
    except Exception:
        return None


def set_cookie(resp: Response, token: str, embedded: bool = False):
    if embedded:
        # Opened in a tab inside the group hub: the browser only keeps a SameSite=None, Partitioned cookie
        # there, and sends it only while the hub is the page around QMS (no other site can use it).
        resp.headers.append("set-cookie", f"{COOKIE_NAME}={token}; Max-Age={COOKIE_TTL}; Path=/; HttpOnly; "
                                          "Secure; SameSite=None; Partitioned")
        return
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_TTL, httponly=True,
                    secure=True, samesite="lax", path="/")


def clear_cookie(resp: Response):
    resp.delete_cookie(COOKIE_NAME, path="/")


# ---- user loading ----
async def _load_employee(uid: int) -> dict | None:
    row = await db.fetchrow("SELECT * FROM employees WHERE id=$1", uid)
    return dict(row) if row else None


async def current_employee(request: Request) -> dict | None:
    """Return the signed-in employee dict, or None. Safe to call anywhere."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    p = decode_token(token)
    if not p or p.get("kind") != "employee":
        return None
    u = await _load_employee(p.get("sub"))
    if not u or not u.get("active"):
        return None
    if (u.get("token_version") or 0) != (p.get("tv") or 0):
        return None
    return u


def _hub_signin_url(request: Request) -> str | None:
    """Where to send a browser tab that opened a QMS link (a PDF, a download) without a QMS session.

    This happens when QMS runs inside the group hub's tab and a link opens in a new browser tab: the new tab
    doesn't carry the in-hub sign-in. The hub signs the person in again with one click and comes straight
    back to the same link. Only for page/link opens (not background API calls), and only if group sign-in is on.
    """
    import os
    from urllib.parse import quote
    if not os.getenv("GROUP_SSO_SECRET") or request.method != "GET":
        return None
    if request.headers.get("sec-fetch-mode") != "navigate" and "text/html" not in request.headers.get("accept", ""):
        return None
    hub = os.getenv("GROUP_HUB_URL", "https://app.thebharatsteelgroup.com").rstrip("/")
    here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return f"{hub}/launch/{os.getenv('GROUP_SSO_APP_KEY', 'qms')}?next={quote(here, safe='')}"


async def require_employee(request: Request) -> dict:
    """FastAPI dependency for JSON APIs — 401 when not an active employee."""
    u = await current_employee(request)
    if not u:
        url = _hub_signin_url(request)
        if url:   # a link opened in a new tab: sign in through the group hub and come back here
            raise HTTPException(status_code=303, detail="sign_in", headers={"Location": url})
        raise HTTPException(status_code=401, detail="auth_required")
    return u


async def authenticate_employee(identifier: str, password: str) -> dict | None:
    row = await db.fetchrow("SELECT * FROM employees WHERE lower(emp_no)=lower($1)",
                            (identifier or "").strip())
    if not row:
        return None
    u = dict(row)
    if not u.get("active") or not verify_pw(password, u.get("password_hash", "")):
        return None
    return u
