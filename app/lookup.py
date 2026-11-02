"""Find a person inside another group app's own database, so nobody types "ID in that app" by hand.

Each app's database address is kept in the hub's Vercel settings, never in the hub database:
    APPDB_BSC_PORTAL      -> the BSC Portal's Neon connection string
    APPDB_METFRAA_PORTAL  -> the Metfraa Portal's Neon connection string
(the name is APPDB_ + the app's key in capitals, dashes as underscores).

Every query runs inside a READ ONLY transaction: the hub can look, never change.
The people table is recognised automatically: employees / users / app_users / staff / portal_users, with
employee_code / emp_no (or the email itself), name and an active flag.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

ID_COLUMNS = ("employee_code", "emp_no", "emp_code", "code")
ACTIVE_COLUMNS = ("is_active", "active")
NAME_COLUMNS = ("name", "full_name", "display_name")
PASSWORD_COLUMNS = ("password_hash", "pass_hash", "pw_hash", "hashed_password", "password")
PHONE_COLUMNS = ("phone", "mobile", "whatsapp", "phone_number", "mobile_no")
TABLES = ("employees", "users", "app_users", "staff", "portal_users", "sales_users")
_engines: dict[str, Engine] = {}
_schemas: dict[str, "Schema | None"] = {}
_notes: dict[str, str] = {}  # what the last search in each app did, shown next to "Not found"


@dataclass
class Schema:
    table: str
    id_col: str
    email_col: str | None
    name_col: str | None
    active_col: str | None
    phone_col: str | None = None


@dataclass
class Match:
    external_id: str
    name: str
    how: str  # "email", "code", "mobile" or "name"


def env_name(app_key: str) -> str:
    return "APPDB_" + app_key.upper().replace("-", "_")


def is_configured(app_key: str) -> bool:
    return bool(os.getenv(env_name(app_key), "").strip())


def _url(raw: str) -> str:
    raw = raw.strip().strip('"').strip("'")
    for prefix in ("postgres://", "postgresql://"):
        if raw.startswith(prefix):
            return "postgresql+psycopg://" + raw[len(prefix):]
    return raw


def _engine(app_key: str) -> Engine | None:
    raw = os.getenv(env_name(app_key), "").strip()
    if not raw:
        return None
    if app_key not in _engines:
        args = {"connect_timeout": 8} if raw.startswith("postgres") else {}
        _engines[app_key] = create_engine(_url(raw), poolclass=NullPool, connect_args=args)
    return _engines[app_key]


def _read_only(conn) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET TRANSACTION READ ONLY"))


def _schema(app_key: str, eng: Engine) -> Schema | None:
    if app_key in _schemas:
        return _schemas[app_key]
    insp = inspect(eng)
    found = None
    tables = set(insp.get_table_names())
    present = [t for t in TABLES if t in tables]
    cols_of = {t: {c["name"].lower() for c in insp.get_columns(t)} for t in present}
    # Prefer the table people actually sign in with (it has a password column). Some apps also keep
    # a staff register (e.g. ATS "employees" = hired candidates) whose rows cannot log in.
    present.sort(key=lambda t: 0 if cols_of[t] & set(PASSWORD_COLUMNS) else 1)
    for table in present:
        cols = cols_of[table]
        # Apps that sign people in by email (no employee code) are identified by the email itself.
        id_col = next((c for c in ID_COLUMNS if c in cols), "email" if "email" in cols else None)
        if id_col:
            found = Schema(
                table=table, id_col=id_col,
                email_col="email" if "email" in cols else None,
                name_col=next((c for c in NAME_COLUMNS if c in cols), None),
                active_col=next((c for c in ACTIVE_COLUMNS if c in cols), None),
                phone_col=next((c for c in PHONE_COLUMNS if c in cols), None),
            )
            break
    _schemas[app_key] = found
    return found


def status(app_key: str) -> tuple[str, str]:
    """('ok'|'off'|'error', message) for the Admin → Apps page."""
    eng = _engine(app_key)
    if eng is None:
        return "off", f"Not set up. Add {env_name(app_key)} in the hub's Vercel settings to fill IDs automatically."
    try:
        sch = _schema(app_key, eng)
        if not sch:
            return "error", "Connected, but this database has no recognisable employees table."
        with eng.begin() as conn:
            _read_only(conn)
            n = conn.execute(text(f"SELECT COUNT(*) FROM {sch.table}")).scalar()
        return "ok", f"Connected: {n} people in table {sch.table} (ID column {sch.id_col})."
    except Exception as exc:  # noqa: BLE001
        _engines.pop(app_key, None)
        _schemas.pop(app_key, None)
        return "error", f"Could not connect: {type(exc).__name__}. Check {env_name(app_key)}."


def find(app_key: str, *, email: str | None, code: str | None, name: str | None,
         allow_name: bool = False, phone: str | None = None) -> Match | None:
    """Look the person up by email, then employee code, then mobile number, then (optionally) name."""
    eng = _engine(app_key)
    if eng is None:
        return None
    try:
        sch = _schema(app_key, eng)
        if not sch:
            _notes[app_key] = "no people table recognised in this database"
            return None
        by = [w for w, ok in (("email", email and sch.email_col), ("employee code", code),
                               ("mobile", phone and sch.phone_col)) if ok]
        _notes[app_key] = f"searched table {sch.table} by {', '.join(by) or 'nothing'}"
        # A person with no employee code in an email-login app is known by their email.
        ext = (f"COALESCE(NULLIF(TRIM({sch.id_col}), ''), {sch.email_col})"
               if sch.email_col and sch.id_col != sch.email_col else sch.id_col)
        cols = f"{ext} AS ext, " + (f"{sch.name_col} AS nm" if sch.name_col else "'' AS nm")
        active = f" AND {sch.active_col} = TRUE" if sch.active_col else ""
        tries: list[tuple[str, str, str]] = []
        if email and sch.email_col:
            tries.append(("email", f"LOWER(TRIM({sch.email_col})) = :v", email.strip().lower()))
        if code:
            tries.append(("code", f"UPPER(TRIM({sch.id_col})) = :v", code.strip().upper()))
        with eng.begin() as conn:
            _read_only(conn)
            for how, where, value in tries:
                rows = conn.execute(
                    text(f"SELECT {cols} FROM {sch.table} WHERE {where}{active} LIMIT 2"), {"v": value}
                ).fetchall()
                if len(rows) == 1 and rows[0].ext:
                    return Match(external_id=str(rows[0].ext).strip(), name=rows[0].nm or "", how=how)
            if phone and sch.phone_col:
                m = _phone_match(conn, sch, cols, active, phone)
                if m:
                    return m
            if allow_name and name and sch.name_col:
                return _name_match(conn, sch, cols, active, name)
        return None
    except Exception as exc:  # noqa: BLE001 — lookup is a convenience; never block saving a person
        _notes[app_key] = f"could not search: {type(exc).__name__} — check {env_name(app_key)}"
        _engines.pop(app_key, None)
        _schemas.pop(app_key, None)
        return None


def _letters(v: str) -> str:
    return "".join(ch for ch in (v or "").lower() if ch.isalnum())


def _last10(v) -> str:
    digits = "".join(ch for ch in str(v or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


def _phone_match(conn, sch: Schema, cols: str, active: str, phone: str) -> Match | None:
    """Mobile numbers are stored many ways (9444085016, 919444085016, +91 94440 85016): compare the
    last 10 digits. Only a single unambiguous hit counts."""
    want = _last10(phone)
    if not want:
        return None
    where = active.replace(" AND ", " WHERE ", 1) if active else ""
    rows = conn.execute(text(f"SELECT {cols}, {sch.phone_col} AS ph FROM {sch.table}{where}")).fetchall()
    hits = [r for r in rows if r.ext and _last10(r.ph) == want]
    if len(hits) == 1:
        return Match(external_id=str(hits[0].ext).strip(), name=hits[0].nm or "", how="mobile")
    return None


def _name_match(conn, sch: Schema, cols: str, active: str, name: str) -> Match | None:
    """Tolerant name match, used only by the 'Find in apps' button (the admin sees and confirms it).

    'jeevabharathy' matches 'Jeevabharathy S' / 'JEEVA BHARATHY' / 'Jeevabharathy.S': spaces, dots and a
    trailing initial are ignored. Only a single unambiguous hit counts.
    """
    want = _letters(name)
    if len(want) < 5:
        return None
    where = active.replace(" AND ", " WHERE ", 1) if active else ""
    rows = conn.execute(text(f"SELECT {cols} FROM {sch.table}{where}")).fetchall()
    exact = [r for r in rows if r.ext and _letters(r.nm) == want]
    if not exact and len(want) >= 7:
        # allow one extra initial (up to 2 letters) on either side: "Jeevabharathy S", "S Jeevabharathy"
        exact = [r for r in rows if r.ext and len(_letters(r.nm)) - len(want) in (1, 2)
                 and (_letters(r.nm).startswith(want) or _letters(r.nm).endswith(want))]
    if len(exact) == 1:
        return Match(external_id=str(exact[0].ext).strip(), name=exact[0].nm or "", how="name")
    return None


def last_note(app_key: str) -> str:
    return _notes.get(app_key, "")


def reset_cache() -> None:
    _notes.clear()
    _engines.clear()
    _schemas.clear()
