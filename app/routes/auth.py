"""Sign in, sign out, change password."""
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import User
from ..security import (clear_session_cookie, current_user, hash_password, issue_session,
                        password_problem, safe_next, set_session_cookie, verify_password)
from ..web import audit, require_user, templates

router = APIRouter()
settings = get_settings()
GENERIC_FAIL = "That ID or password is not right."


def _find_user(db: Session, identifier: str) -> User | None:
    ident = identifier.strip()
    if "@" in ident:
        return db.query(User).filter(func.lower(User.email) == ident.lower()).first()
    return db.query(User).filter(func.upper(User.employee_code) == ident.upper()).first()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", err: str | None = None,
               user: User | None = Depends(current_user)):
    if user and not user.must_reset_password:
        return RedirectResponse(safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next), "err": err})


@router.post("/login")
def login_submit(request: Request, identifier: str = Form(...), password: str = Form(...),
                 next: str = Form("/"), db: Session = Depends(get_db)):
    nxt = safe_next(next)

    def fail(msg: str = GENERIC_FAIL):
        return templates.TemplateResponse(
            request, "login.html", {"next": nxt, "err": msg, "identifier": identifier}, status_code=401)

    user = _find_user(db, identifier or "")
    now = datetime.utcnow()
    if not user or not user.is_active:
        audit(db, request, "login_failed", email=(identifier or "")[:255], reason="unknown_or_inactive")
        db.commit()
        return fail()
    if settings.max_failed_logins and user.locked_until and user.locked_until > now:
        mins = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        return fail(f"Too many wrong attempts. Try again in {mins} minute{'s' if mins > 1 else ''}.")
    # A copied temporary password often carries a space or line break at the end: accept it.
    if not (verify_password(password, user.password_hash)
            or (password != password.strip() and verify_password(password.strip(), user.password_hash))):
        user.failed_logins = (user.failed_logins or 0) + 1
        if settings.max_failed_logins and user.failed_logins >= settings.max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
            user.failed_logins = 0
        audit(db, request, "login_failed", user=user, reason="bad_password")
        db.commit()
        return fail()

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    audit(db, request, "login", user=user)
    db.commit()

    target = f"/change-password?next={quote(nxt)}" if user.must_reset_password else nxt
    resp = RedirectResponse(target, status_code=303)
    set_session_cookie(resp, issue_session(user))
    return resp


@router.get("/logout")
def logout(request: Request, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    if user:
        audit(db, request, "logout", user=user)
        db.commit()
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp)
    return resp


@router.get("/change-password", response_class=HTMLResponse)
def change_password_page(request: Request, next: str = "/", user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "change_password.html",
                                      {"user": user, "next": safe_next(next), "forced": user.must_reset_password})


@router.post("/change-password")
def change_password_submit(request: Request, current: str = Form(""), new_password: str = Form(...),
                           confirm: str = Form(...), next: str = Form("/"),
                           user: User = Depends(require_user), db: Session = Depends(get_db)):
    nxt = safe_next(next)

    def again(msg: str):
        return templates.TemplateResponse(request, "change_password.html",
                                          {"user": user, "next": nxt, "forced": user.must_reset_password, "err": msg},
                                          status_code=400)

    if not verify_password(current, user.password_hash):
        return again("Your current password is not right.")
    if new_password != confirm:
        return again("The two new passwords do not match.")
    if new_password == current:
        return again("Choose a password different from the current one.")
    problem = password_problem(new_password)
    if problem:
        return again(problem)

    user.password_hash = hash_password(new_password)
    user.must_reset_password = False
    user.session_version = (user.session_version or 1) + 1  # signs out every other device
    audit(db, request, "password_changed", user=user)
    db.commit()
    resp = RedirectResponse(nxt, status_code=303)
    set_session_cookie(resp, issue_session(user))
    return resp


# ---------------- forgot password (self-service, one-time code) ----------------
CODE_MINUTES = 10
CODE_MAX_TRIES = 5
RESEND_SECONDS = 60


def _code_hash(code: str) -> str:
    import hashlib
    import hmac
    return hmac.new(settings.session_secret.encode(), code.strip().encode(), hashlib.sha256).hexdigest()


def _forgot(request: Request, step: str = "ask", **ctx):
    status = ctx.pop("status", 200)
    return templates.TemplateResponse(request, "forgot.html", {"step": step, **ctx}, status_code=status)


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request, identifier: str = ""):
    return _forgot(request, identifier=identifier)


@router.post("/forgot", response_class=HTMLResponse)
def forgot_send(request: Request, identifier: str = Form(...), db: Session = Depends(get_db)):
    import secrets

    from .. import notify
    from ..models import PasswordCode

    user = _find_user(db, identifier or "")
    if not user or not user.is_active:
        return _forgot(request, identifier=identifier, status=400,
                       err="We couldn't find an active account with that email or employee code.")
    can_wa = notify.whatsapp_ready() and notify.wa_number(user.phone)
    can_mail = notify.email_ready()
    if not (can_wa or can_mail):
        why = ("There is no mobile number on your account" if notify.whatsapp_ready()
               else "Sending codes is not set up yet")
        return _forgot(request, identifier=identifier, status=400,
                       err=f"{why}, so we can't send you a code. Ask the IT team to reset your password.")

    now = datetime.utcnow()
    last = (db.query(PasswordCode).filter(PasswordCode.user_id == user.id)
            .order_by(PasswordCode.created_at.desc()).first())
    sent_to = ([notify.mask_phone(user.phone) + " on WhatsApp"] if can_wa else []) + \
              ([notify.mask_email(user.email)] if can_mail else [])
    if last and last.created_at and (now - last.created_at).total_seconds() < RESEND_SECONDS:
        return _forgot(request, "verify", identifier=identifier, sent_to=sent_to,
                       note="We sent a code a moment ago. Use that one, or wait a minute to ask again.")

    code = f"{secrets.randbelow(10**6):06d}"
    results = []
    if can_wa:
        results.append(notify.send_whatsapp_code(user.phone, code))
    if can_mail:
        results.append(notify.send_email_code(user.email, user.name, code))
    if not any(ok for ok, _ in results):
        audit(db, request, "password_code_failed", user=user, reason="; ".join(r for _, r in results)[:300])
        db.commit()
        return _forgot(request, identifier=identifier, status=502,
                       err="We couldn't send the code just now. Try again in a minute, or ask the IT team.")
    db.query(PasswordCode).filter(PasswordCode.user_id == user.id).delete()
    db.add(PasswordCode(user_id=user.id, code_hash=_code_hash(code),
                        expires_at=now + timedelta(minutes=CODE_MINUTES)))
    audit(db, request, "password_code_sent", user=user)
    db.commit()
    sent_to = [s for s, (ok, _) in zip(sent_to, results) if ok]
    return _forgot(request, "verify", identifier=identifier, sent_to=sent_to)


@router.post("/forgot/verify", response_class=HTMLResponse)
def forgot_verify(request: Request, identifier: str = Form(...), code: str = Form(...),
                  new_password: str = Form(...), confirm: str = Form(...), db: Session = Depends(get_db)):
    from ..models import PasswordCode

    def again(msg: str, step: str = "verify"):
        return _forgot(request, step, identifier=identifier, err=msg, status=400)

    user = _find_user(db, identifier or "")
    otp = (db.query(PasswordCode).filter(PasswordCode.user_id == user.id)
           .order_by(PasswordCode.created_at.desc()).first()) if user and user.is_active else None
    if not otp:
        return again("That code is wrong or has expired. Ask for a new one.", "ask")
    if otp.expires_at < datetime.utcnow() or otp.attempts >= CODE_MAX_TRIES:
        db.delete(otp)
        db.commit()
        return again("That code has expired. Ask for a new one.", "ask")
    if not __import__("hmac").compare_digest(otp.code_hash, _code_hash(code)):
        otp.attempts += 1
        db.commit()
        return again("That code is not right. Check the latest message and try again.")
    if new_password != confirm:
        return again("The two new passwords do not match.")
    problem = password_problem(new_password)
    if problem:
        return again(problem)

    user.password_hash = hash_password(new_password)
    user.must_reset_password = False
    user.failed_logins, user.locked_until = 0, None
    user.session_version = (user.session_version or 1) + 1
    user.last_login_at = datetime.utcnow()
    db.query(PasswordCode).filter(PasswordCode.user_id == user.id).delete()
    audit(db, request, "password_reset_self", user=user)
    db.commit()
    resp = RedirectResponse("/", status_code=303)
    set_session_cookie(resp, issue_session(user))
    return resp
