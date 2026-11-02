"""Automatic 'ID in that app' lookup against each app's own employees table."""
import os
import re
import sqlite3

from fastapi.testclient import TestClient

HERE = os.path.dirname(__file__)
BSC_DB = os.path.join(HERE, "_bsc_app.db")
MET_DB = os.path.join(HERE, "_met_app.db")


def _make_app_dbs():
    for p in (BSC_DB, MET_DB):
        if os.path.exists(p):
            os.remove(p)
    c = sqlite3.connect(BSC_DB)  # shaped like bsc-tickets
    c.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, emp_no TEXT, name TEXT, email TEXT, active BOOLEAN, phone TEXT)")
    c.executemany("INSERT INTO employees (emp_no,name,email,active,phone) VALUES (?,?,?,?,?)", [
        ("BSC/119", "Jeeva K", "jeeva@bharatsteels.in", 1, None),
        ("BSC/017", "Shivam Shroff", "shivam@gmail.com", 1, "919500680093"),
        ("BSC/050", "Old Staff", "old@bharatsteels.in", 0, "917395956648"),
        ("BSC/060", "Same Name", None, 1, "9876543210"),
        ("BSC/061", "Same Name", None, 1, "+91 98765 43210"),
        ("BSC/070", "Jeevabharathy S", None, 1, "7395956648"),
    ])
    c.commit()
    c = sqlite3.connect(MET_DB)  # shaped like metfraa-portal
    c.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, employee_code TEXT, name TEXT, email TEXT, is_active BOOLEAN)")
    c.executemany("INSERT INTO employees (employee_code,name,email,is_active) VALUES (?,?,?,?)", [
        ("MET66", "Ravi Kumar", "Ravi@Metfraa.com", 1),
        ("MET12", "Jeeva K", "jeeva@bharatsteels.in", 1),
    ])
    c.commit()


def test_lookup_email_apps():
    """Work Tracker-style users (emp_code may be blank) and MIS-style portal_users (email only)."""
    p = os.path.join(HERE, "_wt_app.db")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, emp_code TEXT, email TEXT, name TEXT, active BOOLEAN)")
    c.executemany("INSERT INTO users (emp_code,email,name,active) VALUES (?,?,?,?)",
                  [("BSC/119", "jeeva@bharatsteels.in", "Jeeva", 1), (None, "ravi@bharatsteels.in", "Ravi", 1)])
    c.execute("CREATE TABLE portal_users (id INTEGER PRIMARY KEY, email TEXT, is_admin BOOLEAN)")
    c.commit()
    os.environ["APPDB_WORKTRACKER"] = f"sqlite:///{p}"
    from app import lookup
    lookup.reset_cache()
    assert lookup.find("worktracker", email="ravi@bharatsteels.in", code=None, name=None).external_id == "ravi@bharatsteels.in"
    assert lookup.find("worktracker", email="jeeva@bharatsteels.in", code=None, name=None).external_id == "BSC/119"
    os.environ.pop("APPDB_WORKTRACKER")
    q = os.path.join(HERE, "_mis_app.db")
    if os.path.exists(q):
        os.remove(q)
    c = sqlite3.connect(q)
    c.execute("CREATE TABLE portal_users (id INTEGER PRIMARY KEY, email TEXT, is_admin BOOLEAN)")
    c.execute("INSERT INTO portal_users (email,is_admin) VALUES ('Ravi@BharatSteels.in', 0)")
    c.commit()
    os.environ["APPDB_MIS"] = f"sqlite:///{q}"
    lookup.reset_cache()
    assert lookup.find("mis", email="ravi@bharatsteels.in", code=None, name=None).external_id == "Ravi@BharatSteels.in"
    os.environ.pop("APPDB_MIS")
    lookup.reset_cache()
    os.remove(p)
    os.remove(q)


def test_lookup_prefers_login_table():
    """ATS keeps a staff register (employees, cannot log in) and login accounts (users)."""
    p = os.path.join(HERE, "_ats_app.db")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE employees (id INTEGER PRIMARY KEY, emp_code TEXT, full_name TEXT, email TEXT, phone TEXT)")
    c.execute("INSERT INTO employees (emp_code,full_name,email,phone) VALUES ('BSC/119','Jeeva','jeeva@bharatsteels.in','7395956648')")
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT, password_hash TEXT, role TEXT, active BOOLEAN, phone TEXT)")
    c.execute("INSERT INTO users (name,email,password_hash,role,active,phone) VALUES ('Hema','hema@bharatsteels.in','x','SUPER_ADMIN',1,'+91 98400 11111')")
    c.commit()
    os.environ["APPDB_ATS"] = f"sqlite:///{p}"
    from app import lookup
    lookup.reset_cache()
    assert lookup.find("ats", email="jeeva@bharatsteels.in", code="BSC/119", name=None) is None  # register only, no login
    assert lookup.find("ats", email="hema@bharatsteels.in", code=None, name=None).external_id == "hema@bharatsteels.in"
    assert lookup.find("ats", email=None, code=None, name=None, phone="9840011111").how == "mobile"
    assert lookup.last_note("ats") == "searched table users by mobile"
    assert "table users (ID column email)" in lookup.status("ats")[1]
    os.environ.pop("APPDB_ATS")
    lookup.reset_cache()
    os.remove(p)


def test_lookup_fills_ids():
    _make_app_dbs()
    os.environ["APPDB_BSC_PORTAL"] = f"sqlite:///{BSC_DB}"
    os.environ["APPDB_METFRAA_PORTAL"] = f"sqlite:///{MET_DB}"
    from app import lookup
    from app.database import SessionLocal
    from app.main import app
    from app.models import App, User
    lookup.reset_cache()

    # direct lookups
    assert lookup.find("bsc-portal", email="JEEVA@bharatsteels.in ", code=None, name=None).external_id == "BSC/119"
    assert lookup.find("metfraa-portal", email="ravi@metfraa.com", code=None, name=None).how == "email"
    assert lookup.find("bsc-portal", email=None, code="bsc/017", name=None).external_id == "BSC/017"
    assert lookup.find("bsc-portal", email="old@bharatsteels.in", code=None, name=None) is None  # inactive
    assert lookup.find("bsc-portal", email=None, code=None, name="Same Name", allow_name=True) is None  # ambiguous
    assert lookup.find("bsc-portal", email=None, code=None, name="shivam  shroff", allow_name=True).how == "name"
    assert lookup.find("bsc-portal", email=None, code=None, name="Shivam Shroff") is None  # name only on request
    assert lookup.find("bsc-portal", email=None, code=None, name="jeevabharathy", allow_name=True).external_id == "BSC/070"
    assert lookup.find("bsc-portal", email=None, code=None, name="jeeva", allow_name=True) is None  # too loose
    assert lookup.find("qms", email="jeeva@bharatsteels.in", code=None, name=None) is None  # not configured
    m = lookup.find("bsc-portal", email=None, code=None, name=None, phone="+91 73959 56648")
    assert m.external_id == "BSC/070" and m.how == "mobile"  # the inactive BSC/050 with the same number is ignored
    assert lookup.find("bsc-portal", email=None, code=None, name=None, phone="9500680093").external_id == "BSC/017"
    assert lookup.find("bsc-portal", email=None, code=None, name=None, phone="9876543210") is None  # shared number
    assert lookup.find("bsc-portal", email=None, code=None, name=None, phone="12345") is None
    assert lookup.status("bsc-portal")[0] == "ok" and lookup.status("qms")[0] == "off"

    with TestClient(app) as c:
        c.post("/login", data={"identifier": "ai@bharatsteels.in", "password": "Admin12345", "next": "/"})
        assert c.get("/admin/users").status_code == 200

        db = SessionLocal()
        ids = {a.key: a.id for a in db.query(App).all()}
        db.close()

        # 'Find in apps' button
        r = c.post("/admin/lookup-ids", data={"name": "Jeeva K", "email": "jeeva@bharatsteels.in", "employee_code": ""})
        res = r.json()
        assert res[str(ids["bsc-portal"])] == {"state": "found", "id": "BSC/119", "name": "Jeeva K", "how": "email"}
        assert res[str(ids["metfraa-portal"])]["id"] == "MET12"
        assert res[str(ids["qms"])]["state"] == "off"

        # Saving with ticks but blank IDs fills them from the app databases
        form = {"email": "jeeva@bharatsteels.in", "name": "Jeeva K", "employee_code": "", "company": "BSC",
                f"app_{ids['bsc-portal']}": "on", f"ext_{ids['bsc-portal']}": "",
                f"app_{ids['metfraa-portal']}": "on", f"ext_{ids['metfraa-portal']}": ""}
        r = c.post("/admin/users/new", data=form)
        assert r.status_code == 200 and re.search(r'class="cred">', r.text)
        db = SessionLocal()
        jeeva = db.query(User).filter_by(email="jeeva@bharatsteels.in").one()
        assert {g.app.key: g.external_id for g in jeeva.access} == {"bsc-portal": "BSC/119", "metfraa-portal": "MET12"}
        db.close()

        # A typed ID is kept as typed
        form = {"email": "shivam@gmail.com", "name": "Shivam", "employee_code": "", "company": "BSC",
                f"app_{ids['bsc-portal']}": "on", f"ext_{ids['bsc-portal']}": "BSC/999"}
        c.post("/admin/users/new", data=form)
        db = SessionLocal()
        shivam = db.query(User).filter_by(email="shivam@gmail.com").one()
        assert shivam.access[0].external_id == "BSC/999"
        db.close()

        # 'Match everyone': ravi (in the earlier tests, METFRAA, has MET66) gets BSC? no; Metfraa ID filled
        r = c.post("/admin/users-match")
        assert r.status_code == 200 and "Matched with apps" in r.text
        db = SessionLocal()
        ravi = db.query(User).filter_by(email="ravi@metfraa.com").one()
        assert {g.app.key: g.external_id for g in ravi.access}.get("metfraa-portal") == "MET66"
        db.close()

        # The app page shows the lookup status
        assert "Connected: 6 people" in c.get(f"/admin/apps/{ids['bsc-portal']}").text
        assert "APPDB_QMS" in c.get(f"/admin/apps/{ids['qms']}").text

    for k in ("APPDB_BSC_PORTAL", "APPDB_METFRAA_PORTAL"):
        os.environ.pop(k)
    lookup.reset_cache()
    for p in (BSC_DB, MET_DB):
        os.remove(p)


def test_lookup_sales_users_table(tmp_path, monkeypatch):
    from app import lookup
    import sqlite3
    p = tmp_path / "sales.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE sales_users (emp_no TEXT PRIMARY KEY, emp_key TEXT, name TEXT, mobile TEXT, active BOOLEAN, pw_hash TEXT)")
    c.execute("INSERT INTO sales_users VALUES ('BSC/119','BSC119','Jeeva','917395956648',1,'x')")
    c.commit(); c.close()
    monkeypatch.setenv("APPDB_SALES_TRAINER", f"sqlite:///{p}")
    lookup.reset_cache()
    assert lookup.find("sales-trainer", email=None, code="bsc/119", name=None).external_id == "BSC/119"
    assert lookup.find("sales-trainer", email="jeeva@bharatsteels.in", code=None, name=None, phone="7395956648").how == "mobile"
