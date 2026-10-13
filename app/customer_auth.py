"""Customer authentication against the legacy `customers` table.

The old Node app's customer accounts live in a `customers` table whose exact
columns we don't control, so this module discovers them at runtime from
information_schema and maps whatever it finds: the login identifier can be an
email / username / customer code / phone, and the password column can be
`password_hash` or `password`. Passwords are verified as bcrypt (the same
scheme employees use), with a plaintext fallback for very old rows.

A customer session is a separate cookie (`bsc_qms_cust`) signed with the SAME
JWT_SECRET as the staff app but with kind='customer', so it never collides with
a staff login.
"""
import time
import jwt
from fastapi import Request, Response

from . import db, auth
from .config import JWT_SECRET, COOKIE_TTL

CUST_COOKIE = "bsc_qms_cust"

# candidate column names, in priority order (lower-cased; camelCase legacy
# columns are matched too, since "CompanyName" -> "companyname")
_ID_COLS = ("id", "customer_id", "customerid", "cid", "cust_id", "custid")
_LOGIN_COLS = ("email", "email_id", "emailid", "username", "user_name", "login",
               "customer_code", "customercode", "code", "cust_code", "custcode",
               "account", "phone", "mobile")
_PASS_COLS = ("password_hash", "passwordhash", "pass_hash", "passhash",
              "password", "pwd", "passwd", "pass")
_COMPANY_COLS = ("company", "company_name", "companyname", "firm", "firm_name",
                 "customer_name", "customername", "name")
_CONTACT_COLS = ("contact_name", "contactname", "contact_person", "contactperson",
                 "contact", "person", "name")
_PHONE_COLS = ("phone", "mobile", "phone_no", "phoneno", "contact_no", "contactno",
               "mobile_no", "mobileno")
_ACTIVE_COLS = ("active", "is_active", "isactive", "enabled", "status")
_TV_COLS = ("token_version", "tokenversion", "tv")

_cols_cache: dict | None = None


async def _cols() -> dict:
    """Map of lower-cased column name -> real column name for `customers`."""
    global _cols_cache
    if _cols_cache is not None:
        return _cols_cache
    try:
        rows = await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND lower(table_name)='customers'")
        _cols_cache = {r["column_name"].lower(): r["column_name"] for r in rows}
    except Exception as e:
        print("[cust-auth] column discovery failed:", e)
        _cols_cache = {}
    return _cols_cache


def _pick(cols: dict, names) -> str | None:
    for n in names:
        if n in cols:
            return cols[n]
    return None


def _pick_all(cols: dict, names) -> list:
    return [cols[n] for n in names if n in cols]


def _is_active(v) -> bool:
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s not in ("0", "false", "no", "n", "inactive", "disabled", "blocked", "suspended")


def _pw_ok(password: str, stored: str) -> bool:
    stored = str(stored or "")
    if not stored:
        return False
    if stored.startswith("$2"):              # bcrypt ($2a/$2b/$2y)
        return auth.verify_pw(password, stored)
    return password == stored                 # legacy plaintext fallback


def _low(u: dict) -> dict:
    return {(k.lower() if isinstance(k, str) else k): v for k, v in u.items()}


def _g(low: dict, names) -> str:
    for n in names:
        v = low.get(n)
        if v not in (None, ""):
            return v
    return ""


def profile(u: dict) -> dict:
    low = _low(u)
    return {
        "id": _g(low, _ID_COLS),
        "company": _g(low, _COMPANY_COLS),
        "contact": _g(low, _CONTACT_COLS),
        "email": _g(low, ("email",)),
        "phone": _g(low, _PHONE_COLS),
    }


_DATE_COLS = ("created_at", "created", "created_on", "createdon", "inserted_at",
              "reg_date", "regdate", "date_added", "dateadded")


async def list_all() -> list:
    """All customer accounts, normalized, for the admin Customers list."""
    cols = await _cols()
    if not cols:
        return []
    try:
        rows = await db.fetch("SELECT * FROM customers")
    except Exception as e:
        print("[cust-auth] list failed:", e)
        return []
    active_col = _pick(cols, _ACTIVE_COLS)
    date_col = _pick(cols, _DATE_COLS)
    out = []
    for r in rows:
        u = dict(r)
        low = _low(u)
        p = profile(u)
        created = low.get(date_col.lower()) if date_col else None
        out.append({
            "id": p["id"],
            "company": p["company"] or "",
            "contact": p["contact"] or "",
            "email": p["email"] or "",
            "phone": str(p["phone"] or ""),
            "active": _is_active(low.get(active_col.lower())) if active_col else True,
            "created": (str(created)[:10] if created else ""),
        })
    out.sort(key=lambda x: str(x["company"] or x["contact"] or x["email"] or "").lower())
    return out


async def status() -> dict:
    """Diagnostic — what the auth layer detected about the customers table."""
    cols = await _cols()
    return {
        "table_found": bool(cols),
        "columns": sorted(cols.values()),
        "id_col": _pick(cols, _ID_COLS),
        "login_cols": _pick_all(cols, _LOGIN_COLS),
        "password_col": _pick(cols, _PASS_COLS),
        "company_col": _pick(cols, _COMPANY_COLS),
        "active_col": _pick(cols, _ACTIVE_COLS),
    }


async def authenticate(identifier: str, password: str):
    """Return (customer_dict, None) on success, else (None, reason)."""
    cols = await _cols()
    if not cols:
        return None, "no_table"
    login_cols = _pick_all(cols, _LOGIN_COLS)
    pass_col = _pick(cols, _PASS_COLS)
    if not login_cols or not pass_col:
        return None, "schema"
    ident = (identifier or "").strip()
    if not ident:
        return None, "nouser"
    where = " OR ".join(f'lower("{c}"::text)=lower($1)' for c in login_cols)
    try:
        row = await db.fetchrow(f"SELECT * FROM customers WHERE {where} LIMIT 1", ident)
    except Exception as e:
        print("[cust-auth] login query failed:", e)
        return None, "error"
    if not row:
        return None, "nouser"
    u = dict(row)
    low = _low(u)
    if not _pw_ok(password, low.get(pass_col.lower())):
        return None, "badpw"
    active_col = _pick(cols, _ACTIVE_COLS)
    if active_col and not _is_active(low.get(active_col.lower())):
        return None, "inactive"
    return u, None


async def make_token(u: dict) -> str:
    cols = await _cols()
    id_col = _pick(cols, _ID_COLS)
    tv_col = _pick(cols, _TV_COLS)
    low = _low(u)
    payload = {
        "sub": low.get(id_col.lower()) if id_col else low.get("id"),
        "kind": "customer",
        "tv": (low.get(tv_col.lower()) if tv_col else 0) or 0,
        "exp": int(time.time()) + COOKIE_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


async def current_customer(request: Request) -> dict | None:
    token = request.cookies.get(CUST_COOKIE)
    if not token:
        return None
    p = auth.decode_token(token)
    if not p or p.get("kind") != "customer":
        return None
    cols = await _cols()
    id_col = _pick(cols, _ID_COLS)
    if not id_col:
        return None
    try:
        row = await db.fetchrow(
            f'SELECT * FROM customers WHERE "{id_col}"::text=$1', str(p.get("sub")))
    except Exception:
        return None
    if not row:
        return None
    u = dict(row)
    low = _low(u)
    tv_col = _pick(cols, _TV_COLS)
    if tv_col and (low.get(tv_col.lower()) or 0) != (p.get("tv") or 0):
        return None
    active_col = _pick(cols, _ACTIVE_COLS)
    if active_col and not _is_active(low.get(active_col.lower())):
        return None
    return u


def set_cookie(resp: Response, token: str):
    resp.set_cookie(CUST_COOKIE, token, max_age=COOKIE_TTL, httponly=True,
                    secure=True, samesite="lax", path="/")


def clear_cookie(resp: Response):
    resp.delete_cookie(CUST_COOKIE, path="/")
