"""First-run data and the group's app catalogue (all editable later in Admin → Apps)."""
from sqlalchemy.orm import Session

from .config import get_settings
from .models import App, AuditLog, User
from .security import hash_password, new_app_secret

# The group's apps. Keys never change once people are linked to them (grants, "ID in that app"
# and the APPDB_<KEY> lookup setting all hang off the key). Each app starts as a plain link and is
# switched to "Single sign-on" in Admin → Apps once that app has its receiver and secret deployed.
CATALOGUE_VERSION = "catalogue_2026_11_02"
CATALOGUE = [
    dict(key="bsc-portal", name="Tickets", company="BSC", icon="ticket", sort_order=10,
         description="BSC Portal: Tickets, Outpass, Expense, OT, Canteen",
         url="https://app.bharatsteels.in", identity_field="employee_code"),
    dict(key="qms", name="QMS", company="BSC", icon="shield", sort_order=20,
         description="Inspections, generator logs and plant checks",
         url="https://bsc-inspection.vercel.app", identity_field="employee_code"),
    dict(key="metfraa-portal", name="Metfraa Portal", company="METFRAA", icon="grid", sort_order=10,
         description="KPI, Site Visits, Expense, EHS, Gate Pass, Plant",
         url="https://app.metfraa.com", identity_field="employee_code"),
    dict(key="worktracker", name="Work Tracker", company="GROUP", icon="clipboard", sort_order=10,
         description="Task allocation, deadlines and reminders",
         url="https://worktracker-eta-one.vercel.app", identity_field="email"),
    dict(key="ats", name="ATS", company="GROUP", icon="users", sort_order=20,
         description="Recruitment and job applications",
         url="https://ats-kohl-rho.vercel.app", identity_field="email"),
    dict(key="mis", name="MIS", company="GROUP", icon="chart", sort_order=30,
         description="MIS reports and dashboards",
         url="https://mis-portal-rho.vercel.app", identity_field="email"),
    dict(key="sales-trainer", name="Sales Trainer", company="GROUP", icon="chat", sort_order=40,
         description="Sales-call insights and training",
         url="https://crayon-web.vercel.app", identity_field="employee_code",
         sso_path="/api/sso"),  # Sales AI Suite: Emp No login, receiver at /api/sso
    dict(key="it-assets", name="IT Assets & Vault", company="GROUP", icon="lock", sort_order=50,
         description="Asset register and password vault",
         url="https://it-valute.vercel.app", identity_field="email"),
]


def apply_catalogue(db: Session) -> None:
    """Bring the app list in line with CATALOGUE, once per CATALOGUE_VERSION.

    Updates names/addresses of known apps, adds new ones and hides (never deletes) apps that are
    no longer listed. Leaves alone what admins set per app: sign-in mode, secret, 'open to everyone',
    and every person's access.
    """
    if db.query(AuditLog).filter(AuditLog.action == CATALOGUE_VERSION).first():
        return
    keys = set()
    for entry in CATALOGUE:
        entry = dict(entry)
        has_sso = entry.pop("sso", True)
        sso_path = entry.pop("sso_path", "/auth/sso")
        keys.add(entry["key"])
        app = db.query(App).filter(App.key == entry["key"]).first()
        sso_url = entry["url"].rstrip("/") + sso_path if has_sso else None
        if app:
            for field, value in entry.items():
                setattr(app, field, value)
            app.sso_url = sso_url
            if not has_sso:
                app.launch_mode = "link"
            app.is_active = True
            if not app.sso_secret:
                app.sso_secret = new_app_secret()
        else:
            db.add(App(launch_mode="link", sso_url=sso_url, sso_secret=new_app_secret(), **entry))
    for app in db.query(App).filter(App.key.notin_(keys)).all():
        app.is_active = False
    db.add(AuditLog(action=CATALOGUE_VERSION, note=f"{len(CATALOGUE)} apps"))


def bootstrap(db: Session) -> None:
    s = get_settings()
    if db.query(User).count() == 0 and s.bootstrap_email and s.bootstrap_password:
        db.add(User(
            email=s.bootstrap_email, name=s.bootstrap_name, company="GROUP",
            password_hash=hash_password(s.bootstrap_password),
            must_reset_password=True, is_superadmin=True,
        ))
        db.add(AuditLog(action="bootstrap_admin", email=s.bootstrap_email))
    apply_catalogue(db)
    db.commit()
