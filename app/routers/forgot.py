"""Staff password reset over WhatsApp.

Flow: the employee enters their code or mobile -> a 6-digit OTP is sent to their
registered mobile via WATI -> they enter the OTP and a new password. Codes are
stored hashed (bcrypt), expire in 10 minutes, are single-use, rate-limited on
resend, and capped at 5 wrong attempts. A successful reset bumps token_version,
signing the account out everywhere (same as an admin reset)."""
import re
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import db, wati
from ..auth import hash_pw, verify_pw

router = APIRouter(tags=["forgot"])

OTP_TTL = 600          # seconds (10 minutes)
MAX_ATTEMPTS = 5
RESEND_GAP = 45        # seconds between OTP requests
_ensured = False


async def ensure_table():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id SERIAL PRIMARY KEY, kind TEXT DEFAULT 'employee', user_id INT,
            code_hash TEXT, mobile TEXT, expires_at TIMESTAMPTZ,
            attempts INT DEFAULT 0, used BOOLEAN DEFAULT false,
            created_at TIMESTAMPTZ DEFAULT now())
    """)
    await db.execute("CREATE INDEX IF NOT EXISTS ix_pr_user ON password_resets (user_id, created_at DESC)")
    _ensured = True


def _mask(num) -> str:
    d = re.sub(r"\D", "", str(num or ""))
    return ("•••••" + d[-4:]) if len(d) >= 4 else "•••••"


async def _find_emp(identifier: str):
    ident = (identifier or "").strip()
    if not ident:
        return None
    digits = re.sub(r"\D", "", ident)
    tail = digits[-10:] if len(digits) >= 10 else ""
    row = await db.fetchrow(
        "SELECT id, emp_no, name, COALESCE(mobile,'') AS mobile, COALESCE(active,true) AS active "
        "FROM employees WHERE lower(emp_no)=lower($1) "
        "   OR ($2 <> '' AND regexp_replace(COALESCE(mobile,''),'\\D','','g') LIKE '%'||$2) "
        "LIMIT 1",
        ident, tail)
    return dict(row) if row else None


class StartIn(BaseModel):
    identifier: str


@router.post("/forgot/start")
async def forgot_start(body: StartIn):
    await ensure_table()
    emp = await _find_emp(body.identifier)
    if not emp or not emp["active"]:
        raise HTTPException(404, "No active account found with that employee code or mobile number.")
    if not emp["mobile"]:
        raise HTTPException(400, "No mobile number is on file for this account. Please ask an admin to add one.")
    # resend rate-limit
    last = await db.fetchrow(
        "SELECT created_at FROM password_resets WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1", emp["id"])
    if last and last["created_at"]:
        age = (datetime.now(timezone.utc) - last["created_at"]).total_seconds()
        if age < RESEND_GAP:
            raise HTTPException(429, f"Please wait {int(RESEND_GAP - age)}s before requesting another code.")
    code = "%06d" % secrets.randbelow(1000000)
    exp = datetime.now(timezone.utc) + timedelta(seconds=OTP_TTL)
    await db.execute(
        "INSERT INTO password_resets (kind,user_id,code_hash,mobile,expires_at) "
        "VALUES ('employee',$1,$2,$3,$4)", emp["id"], hash_pw(code), emp["mobile"], exp)
    ok, detail = await wati.send_otp(emp["mobile"], code)
    if not ok:
        print("[forgot] WATI send failed:", detail)
        raise HTTPException(502, "Couldn't send the WhatsApp code right now. Please try again shortly or contact an admin.")
    return JSONResponse({"ok": True, "masked": _mask(emp["mobile"]), "ttl": OTP_TTL},
                        headers={"Cache-Control": "no-store"})


class VerifyIn(BaseModel):
    identifier: str
    code: str
    new_password: str


@router.post("/forgot/verify")
async def forgot_verify(body: VerifyIn):
    await ensure_table()
    emp = await _find_emp(body.identifier)
    if not emp:
        raise HTTPException(404, "Account not found.")
    if len((body.new_password or "")) < 4:
        raise HTTPException(400, "Choose a password of at least 4 characters.")
    row = await db.fetchrow(
        "SELECT * FROM password_resets WHERE user_id=$1 AND used=false ORDER BY created_at DESC LIMIT 1",
        emp["id"])
    if not row:
        raise HTTPException(400, "No active reset request. Please start again.")
    r = dict(row)
    if r.get("expires_at") and r["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(400, "This code has expired. Please request a new one.")
    if (r.get("attempts") or 0) >= MAX_ATTEMPTS:
        raise HTTPException(429, "Too many incorrect attempts. Please request a new code.")
    if not verify_pw((body.code or "").strip(), r.get("code_hash") or ""):
        await db.execute("UPDATE password_resets SET attempts=COALESCE(attempts,0)+1 WHERE id=$1", r["id"])
        raise HTTPException(400, "Incorrect code. Please try again.")
    await db.execute(
        "UPDATE employees SET password_hash=$1, token_version=COALESCE(token_version,0)+1 WHERE id=$2",
        hash_pw(body.new_password), emp["id"])
    await db.execute("UPDATE password_resets SET used=true WHERE user_id=$1", emp["id"])
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
