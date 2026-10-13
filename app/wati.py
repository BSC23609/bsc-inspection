"""WATI WhatsApp sender — used to deliver password-reset OTPs to staff mobiles.

WATI declines (bad number, template not approved, out-of-window) return a JSON
body with result=false rather than raising, so callers get (ok, detail) and
log the detail instead of trusting an exception.
"""
import re
import httpx

from .config import (WATI_BASE_URL, WATI_TOKEN, WATI_OTP_TEMPLATE,
                     WATI_OTP_BROADCAST, WATI_OTP_PARAM)


def normalize_number(raw) -> str:
    """Return a WhatsApp number as country-code + number, digits only (e.g. 9198...)."""
    d = re.sub(r"\D", "", str(raw or ""))
    if not d:
        return ""
    if len(d) == 10:                 # bare 10-digit Indian mobile
        return "91" + d
    if len(d) == 11 and d.startswith("0"):
        return "91" + d[1:]
    if len(d) == 12 and d.startswith("91"):
        return d
    if len(d) == 13 and d.startswith("091"):
        return d[1:]
    return d


def _auth() -> str:
    t = (WATI_TOKEN or "").strip()
    if not t:
        return ""
    return t if t.lower().startswith("bearer ") else "Bearer " + t


def configured() -> bool:
    return bool(WATI_BASE_URL and WATI_TOKEN and WATI_OTP_TEMPLATE)


async def send_otp(number: str, code: str):
    """Send the OTP via an approved template. Returns (ok: bool, detail: str)."""
    if not configured():
        return False, "WATI is not configured (set WATI_BASE_URL / WATI_TOKEN / WATI_OTP_TEMPLATE)."
    num = normalize_number(number)
    if not num:
        return False, "No valid mobile number."
    url = f"{WATI_BASE_URL}/api/v1/sendTemplateMessage"
    body = {
        "template_name": WATI_OTP_TEMPLATE,
        "broadcast_name": WATI_OTP_BROADCAST or WATI_OTP_TEMPLATE,
        "parameters": [{"name": WATI_OTP_PARAM, "value": str(code)}],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, params={"whatsappNumber": num},
                             headers={"Authorization": _auth(),
                                      "Content-Type": "application/json"},
                             json=body)
            j = r.json() if r.content else {}
    except Exception as e:
        return False, f"send error: {e}"
    # WATI success bodies carry result=true; declines carry result=false (+ info/errors)
    if isinstance(j, dict) and j.get("result") is True:
        return True, "sent"
    if r.status_code < 300 and isinstance(j, dict) and j.get("result") is None and not j.get("errors"):
        return True, "sent"   # tolerant of minor body shape differences on 2xx
    return False, (str(j)[:300] or f"HTTP {r.status_code}")
