"""Settings for the Bharat Steel Group app hub — all from environment variables."""
import os
from functools import lru_cache


class Settings:
    def __init__(self) -> None:
        self.database_url: str = os.getenv("DATABASE_URL", "sqlite:///./hub.db")
        # Neon gives postgres:// or postgresql:// — SQLAlchemy 2 + psycopg3 wants postgresql+psycopg://
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql+psycopg://" + self.database_url[len("postgres://"):]
        elif self.database_url.startswith("postgresql://"):
            self.database_url = "postgresql+psycopg://" + self.database_url[len("postgresql://"):]

        self.session_secret: str = os.getenv("SESSION_SECRET", "dev-only-change-me")
        self.session_days: int = int(os.getenv("SESSION_DAYS", "30"))
        # Cookies must be Secure in production; tests/local http set COOKIE_SECURE=false.
        self.cookie_secure: bool = os.getenv("COOKIE_SECURE", "true").lower() != "false"
        self.public_url: str = os.getenv("PUBLIC_URL", "https://app.thebharatsteelgroup.com").rstrip("/")
        # Hand-off tokens live for this many seconds — long enough for one redirect.
        self.handoff_ttl: int = int(os.getenv("HANDOFF_TTL_SECONDS", "60"))
        self.issuer: str = "bsg-hub"

        # First-run superadmin (only used when the users table is empty).
        self.bootstrap_email: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip().lower()
        self.bootstrap_password: str = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
        self.bootstrap_name: str = os.getenv("BOOTSTRAP_ADMIN_NAME", "Group Admin")

        # Starting password for every new or reset account. Each person must change it at first sign-in.
        # Change it any time with COMMON_PASSWORD in Vercel (then redeploy).
        self.common_password: str = os.getenv("COMMON_PASSWORD", "").strip() or "Bsg@1234"

        # Sign-in lock after this many wrong passwords in a row. 0 = never lock (the default).
        self.max_failed_logins: int = int(os.getenv("MAX_FAILED_LOGINS", "0") or 0)
        self.lockout_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
