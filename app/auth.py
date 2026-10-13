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


def set_cookie(resp: Response, token: str):
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


async def require_employee(request: Request) -> dict:
    """FastAPI dependency for JSON APIs — 401 when not an active employee."""
    u = await current_employee(request)
    if not u:
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
