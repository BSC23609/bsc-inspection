"""Environment configuration. Reuses the SAME variables as the current Node QMS,
so the FastAPI app shares its database, auth cookie and OneDrive integration."""
import os

DATABASE_URL  = os.environ.get("DATABASE_URL", "")
JWT_SECRET    = os.environ.get("JWT_SECRET", "dev-insecure-change-me")
COOKIE_NAME   = "bsc_qms_token"          # identical to the Node app -> sessions are shared
COOKIE_TTL    = 365 * 24 * 60 * 60       # 1 year, matches the current app

# Microsoft Graph (OneDrive uploads + Excel/CSV logs) — same app registration
TENANT_ID     = os.environ.get("TENANT_ID", "")
CLIENT_ID     = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
USER_ID       = os.environ.get("USER_ID", "")   # OneDrive owner (UPN or object id)

# OneDrive base folder (keep in step with the Node app's "BSC Inspections/...")
ONEDRIVE_BASE = "BSC Inspections"

APP_NAME      = "Bharat Steel QMS"

# Admin panel password — same default as the Node app; override with env in prod
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Bsc@123")
SETTINGS_PATH  = f"{ONEDRIVE_BASE}/Config/settings.json"

# WATI (WhatsApp) — used to send password-reset OTPs to staff mobiles.
# Reuses the SAME variables the legacy app already set on Vercel
# (WATI_API_ENDPOINT / WATI_ACCESS_TOKEN / WATI_TEMPLATE_NAME), with the
# newer names as optional overrides.
WATI_BASE_URL      = (os.environ.get("WATI_API_ENDPOINT")
                      or os.environ.get("WATI_BASE_URL", "")).rstrip("/")
WATI_TOKEN         = (os.environ.get("WATI_ACCESS_TOKEN")
                      or os.environ.get("WATI_TOKEN", ""))
# Prefer a dedicated OTP template if set; otherwise fall back to the existing one.
WATI_OTP_TEMPLATE  = (os.environ.get("WATI_OTP_TEMPLATE")
                      or os.environ.get("WATI_TEMPLATE_NAME", ""))
WATI_OTP_BROADCAST = os.environ.get("WATI_OTP_BROADCAST", "") or "bsc_qms_otp"
WATI_OTP_PARAM     = os.environ.get("WATI_OTP_PARAM", "1")   # template body param name

# Maintenance WhatsApp reminders (approved WATI templates; set when ready)
WATI_MAINT_DUE_TEMPLATE      = os.environ.get("WATI_MAINT_DUE_TEMPLATE", "")
WATI_MAINT_OVERDUE_TEMPLATE  = os.environ.get("WATI_MAINT_OVERDUE_TEMPLATE", "")
MAINT_SUPERVISOR_MOBILE      = os.environ.get("MAINT_SUPERVISOR_MOBILE", "")  # overdue escalation

# Shared agent token for protected cron endpoints (reminders) — matches the Node app
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "BSC@123")
