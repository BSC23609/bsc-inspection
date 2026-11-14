"""Send a one-time password-reset code by WhatsApp (WATI) and/or email (SMTP).

Settings (Vercel → bsg-hub → Environment Variables), the same names the other group apps use:
    WATI_BASE_URL, WATI_TOKEN, WATI_OTP_TEMPLATE (default otp_password2)      -> WhatsApp
    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_FROM_NAME -> email
"""
from __future__ import annotations

import json
import os
import smtplib
import time
import urllib.error
import urllib.request
from email.mime.text import MIMEText


def _env(name: str, default: str = "") -> str:
    """Settings pasted into Vercel sometimes carry quotes or spaces: drop them."""
    return os.getenv(name, default).strip().strip('"').strip("'").strip()


def wati_base() -> str:
    base = _env("WATI_BASE_URL").rstrip("/")
    for tail in ("/api/v1/sendTemplateMessage", "/api/v1", "/api/v2"):
        if base.endswith(tail):
            base = base[: -len(tail)]
    return base


def whatsapp_ready() -> bool:
    return bool(wati_base() and _env("WATI_TOKEN"))


def email_ready() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip())


def wa_number(raw: str | None) -> str | None:
    """WATI wants digits with country code: 98… -> 9198…"""
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10:
        d = "91" + d
    return d if len(d) >= 11 else None


def mask_phone(raw: str | None) -> str:
    d = wa_number(raw) or ""
    return f"+{d[:2]} ••••• {d[-4:]}" if d else ""


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return (name[:2] + "•" * max(1, len(name) - 2) + "@" + domain) if domain else email


def send_whatsapp_code(phone: str, code: str) -> tuple[bool, str]:
    to = wa_number(phone)
    if not (whatsapp_ready() and to):
        return False, "WhatsApp not set up or no mobile number"
    base = wati_base()
    token = _env("WATI_TOKEN")
    body = json.dumps({
        "template_name": _env("WATI_OTP_TEMPLATE") or "otp_password2",
        "broadcast_name": f"bsg_hub_reset_{int(time.time())}",
        "parameters": [{"name": "1", "value": code}],
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/v1/sendTemplateMessage?whatsappNumber={to}", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 # WATI sits behind a firewall that turns away Python's default "Python-urllib" agent.
                 "User-Agent": "Mozilla/5.0 (compatible; BSG-Hub/1.0)",
                 "Authorization": token if token.lower().startswith("bearer") else f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            text = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200] if exc.fp else ""
        hint = {401: "WATI_TOKEN is wrong or expired", 403: "WATI refused the request (token or firewall)",
                404: "WATI_BASE_URL is wrong"}.get(exc.code, "")
        return False, f"WATI HTTP {exc.code}{' – ' + hint if hint else ''} {detail}".strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"WATI error: {type(exc).__name__}: {exc}"
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    # Same rule as the other group apps: accepted unless WATI explicitly declines.
    if data.get("result") in (False, "false") or data.get("validWhatsAppNumber") is False:
        return False, str(data.get("info") or data.get("message") or text[:200])
    return True, "sent"


def send_email_code(to: str, name: str, code: str) -> tuple[bool, str]:
    if not email_ready():
        return False, "Email not set up"
    sender = os.getenv("SMTP_FROM", "info@bharatsteels.in")
    msg = MIMEText(
        f"Hello {name.split(' ')[0]},\n\nYour Bharat Steel Group password reset code is: {code}\n\n"
        "It works for 10 minutes. If you did not ask for this, ignore this email.\n\n— Bharat Steel Group")
    msg["Subject"] = f"Your password reset code: {code}"
    msg["From"] = f"{os.getenv('SMTP_FROM_NAME', 'Bharat Steel Group')} <{sender}>"
    msg["To"] = to
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.sendmail(sender, [to], msg.as_string())
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP error: {type(exc).__name__}: {str(exc)[:150]}"
    return True, "sent"


def send_alert_email(subject: str, body: str, to: str | None = None) -> tuple[bool, str]:
    """Send an operational alert email (e.g. OneDrive/Graph access down). Recipients come
    from `to`, else the ALERT_EMAIL env (comma/semicolon separated), else SMTP_FROM.
    Best-effort — returns (ok, detail)."""
    if not email_ready():
        return False, "Email not set up"
    sender = os.getenv("SMTP_FROM", "info@bharatsteels.in")
    rcpt = to or os.getenv("ALERT_EMAIL", "") or sender
    recipients = [r.strip() for r in rcpt.replace(";", ",").split(",") if r.strip()]
    if not recipients:
        return False, "No alert recipient"
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = f"{os.getenv('SMTP_FROM_NAME', 'Bharat Steel Group')} <{sender}>"
    msg["To"] = ", ".join(recipients)
    try:
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.sendmail(sender, recipients, msg.as_string())
    except Exception as exc:  # noqa: BLE001
        return False, f"SMTP error: {type(exc).__name__}: {str(exc)[:150]}"
    return True, "sent"
