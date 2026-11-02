"""Tables for the group hub: people, the app catalogue, who can open what, and an audit log."""
from datetime import datetime

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import relationship

from .database import Base

COMPANIES = [
    ("BSC", "Bharat Steel Chennai"),
    ("METFRAA", "Metfraa Steel Buildings"),
    ("CRAYON", "Crayon Roofings & Structures"),
    ("GROUP", "Group-wide"),
]
COMPANY_NAMES = dict(COMPANIES)


class User(Base):
    """One person, one group login. Email is the main ID; employee code also works."""
    __tablename__ = "hub_users"

    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    employee_code = Column(String(64), unique=True, nullable=True, index=True)
    name = Column(String(255), nullable=False)
    company = Column(String(16), nullable=False, default="GROUP")
    phone = Column(String(32), nullable=True)
    department = Column(String(255), nullable=True)

    password_hash = Column(String(255), nullable=False)
    must_reset_password = Column(Boolean, nullable=False, default=True)
    is_superadmin = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    # Bumped on password change / reset / deactivation → every old session cookie stops working.
    session_version = Column(Integer, nullable=False, default=1)

    failed_logins = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    access = relationship("AppAccess", back_populates="user", cascade="all, delete-orphan")


class PasswordCode(Base):
    """A live one-time code for "Forgot password". Only the hash is stored; used codes are deleted."""
    __tablename__ = "hub_password_codes"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("hub_users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class App(Base):
    """An application tile on the launcher."""
    __tablename__ = "hub_apps"

    id = Column(Integer, primary_key=True)
    key = Column(String(48), unique=True, nullable=False, index=True)  # e.g. "metfraa-portal"
    name = Column(String(120), nullable=False)
    description = Column(String(255), nullable=True)
    company = Column(String(16), nullable=False, default="GROUP")
    url = Column(String(512), nullable=False)  # where "link" apps open, and the app's home
    # "sso"  = hub signs the person straight in (app has the SSO receiver installed)
    # "link" = plain link; the app still shows its own login until it is wired up
    launch_mode = Column(String(8), nullable=False, default="link")
    sso_url = Column(String(512), nullable=True)  # e.g. https://app.metfraa.com/auth/sso
    sso_secret = Column(String(128), nullable=True)  # shared with that app only
    # How the app recognises people in the hand-off: "employee_code" or "email".
    identity_field = Column(String(16), nullable=False, default="employee_code")
    icon = Column(String(32), nullable=False, default="grid")
    open_to_all = Column(Boolean, nullable=False, default=False)  # everyone in the group sees it
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=100)
    created_at = Column(DateTime, default=datetime.utcnow)

    access = relationship("AppAccess", back_populates="app", cascade="all, delete-orphan")


class AppAccess(Base):
    """Grants one person one app, optionally with the login ID that app knows them by."""
    __tablename__ = "hub_app_access"
    __table_args__ = (UniqueConstraint("user_id", "app_id", name="uq_access_user_app"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("hub_users.id", ondelete="CASCADE"), nullable=False, index=True)
    app_id = Column(Integer, ForeignKey("hub_apps.id", ondelete="CASCADE"), nullable=False, index=True)
    # Their ID inside that app (e.g. MET66 in Metfraa Portal, 1042 in BSC Tickets).
    # Blank = fall back to the hub employee code (or email, per the app's identity_field).
    external_id = Column(String(128), nullable=True)
    granted_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="access")
    app = relationship("App", back_populates="access")


class AuditLog(Base):
    __tablename__ = "hub_audit"

    id = Column(Integer, primary_key=True)
    at = Column(DateTime, default=datetime.utcnow, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    email = Column(String(255), nullable=True)
    action = Column(String(48), nullable=False)  # login, login_failed, launch, logout, admin_*
    app_key = Column(String(48), nullable=True)
    ip = Column(String(64), nullable=True)
    details = Column(JSON, nullable=True)
    note = Column(Text, nullable=True)
