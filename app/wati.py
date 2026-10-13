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


def send_url() -> str:
    """Build the sendTemplateMessage URL, tolerant of how WATI_API_ENDPOINT is stored
    (bare host, host+/api/v1, or the full endpoint)."""
    base = (WATI_BASE_URL or "").rstrip("/")
    if not base:
        return ""
    low = base.lower()
    if "sendtemplatemessage" in low:
        return base
    if low.endswith("/api/v1"):
        return base + "/sendTemplateMessage"
    return base + "/api/v1/sendTemplateMessage"


def status() -> dict:
    """Non-secret view of the WATI config, for diagnostics."""
    host = ""
    try:
        from urllib.parse import urlsplit
        host = urlsplit(WATI_BASE_URL).netloc or WATI_BASE_URL
    except Exception:
        host = WATI_BASE_URL
    return {
        "configured": configured(),
        "endpoint_set": bool(WATI_BASE_URL),
        "endpoint_host": host,
        "token_set": bool(WATI_TOKEN),
        "template": WATI_OTP_TEMPLATE,
        "broadcast": WATI_OTP_BROADCAST,
        "param": WATI_OTP_PARAM,
        "send_url": send_url(),
    }


async def send_otp(number: str, code: str):
    """Send the OTP via an approved template. Returns (ok: bool, detail: str)."""
    if not configured():
        return False, "WATI is not configured (need endpoint, token and a template name)."
    num = normalize_number(number)
    if not num:
        return False, "No valid mobile number."
    url = send_url()
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
