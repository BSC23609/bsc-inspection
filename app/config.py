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
