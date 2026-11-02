"""End-to-end checks for the group hub. Run: pytest -q (uses a throwaway SQLite file)."""
import csv
import io
import os
import re
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

os.environ.update(
    DATABASE_URL="sqlite:///./test_hub.db", COOKIE_SECURE="false",
    SESSION_SECRET="test-session-secret-0123456789abcdef0123",
    BOOTSTRAP_ADMIN_EMAIL="ai@bharatsteels.in", BOOTSTRAP_ADMIN_PASSWORD="Start1234",
)
if os.path.exists("test_hub.db"):
    os.remove("test_hub.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import App, User  # noqa: E402

ADMIN_PW = "Admin12345"


def login(c, ident, pw, nxt="/"):
    return c.post("/login", data={"identifier": ident, "password": pw, "next": nxt}, follow_redirects=False)


def set_pw(c, cur, new):
    return c.post("/change-password", data={"current": cur, "new_password": new, "confirm": new, "next": "/"},
                  follow_redirects=False)


@pytest.fixture(scope="module")
def admin():
    with TestClient(app) as c:
        r = login(c, "ai@bharatsteels.in", "Start1234")
        assert r.status_code == 303 and r.headers["location"].startswith("/change-password")
        # forced change: every other page bounces back to change-password
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/change-password")
        assert set_pw(c, "Start1234", ADMIN_PW).status_code == 303
        assert c.get("/").status_code == 200
        yield c


def db_app(key):
    db = SessionLocal()
    try:
        a = db.query(App).filter_by(key=key).one()
        db.expunge(a)
        return a
    finally:
        db.close()


def make_sso(admin, key, sso_url):
    a = db_app(key)
    form = {"key": a.key, "name": a.name, "company": a.company, "url": a.url, "launch_mode": "sso",
            "sso_url": sso_url, "identity_field": a.identity_field, "icon": a.icon, "sort_order": "10",
            "is_active": "on", "description": a.description or ""}
    r = admin.post(f"/admin/apps/{a.id}", data=form, follow_redirects=False)
    assert r.status_code == 303, r.text
    return db_app(key)


def create_person(admin, email, name, code, grants: dict[str, str]):
    """grants: app key -> external id ('' = none)."""
    form = {"email": email, "name": name, "employee_code": code, "company": "METFRAA"}
    for key, ext in grants.items():
        a = db_app(key)
        form[f"app_{a.id}"] = "on"
        form[f"ext_{a.id}"] = ext
    r = admin.post("/admin/users/new", data=form)
    assert r.status_code == 200, r.text
    m = re.search(r'class="cred"[^>]*>([^<]+)<', r.text)
    assert m, "temporary password should be shown once"
    return m.group(1)


def test_requires_login_and_blocks_open_redirect():
    with TestClient(app) as c:
        r = c.get("/launch/metfraa-portal", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/launch/metfraa-portal"
        r = c.get("/login?next=//evil.com")
        assert 'value="/"' in r.text and "evil" not in r.text


def test_seeded_catalogue(admin):
    keys = {a.key for a in SessionLocal().query(App).all()}
    assert keys == {"bsc-portal", "qms", "metfraa-portal", "worktracker", "ats", "mis", "sales-trainer", "it-assets"}
    assert db_app("metfraa-portal").launch_mode == "link"  # until the app has its secret
    assert db_app("qms").url == "https://bsc-inspection.vercel.app"
    assert db_app("qms").sso_url == "https://bsc-inspection.vercel.app/auth/sso"
    assert db_app("sales-trainer").sso_url == "https://crayon-web.vercel.app/api/sso"
    assert db_app("sales-trainer").identity_field == "employee_code"
    assert db_app("mis").identity_field == "email"


def test_link_mode_opens_app(admin):
    r = admin.get("/launch/qms", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://bsc-inspection.vercel.app"


def test_full_sso_flow_for_a_new_person(admin):
    mp = make_sso(admin, "metfraa-portal", "https://app.metfraa.com/auth/sso")
    bp = make_sso(admin, "bsc-portal", "https://app.bharatsteels.in/auth/sso")
    pw = create_person(admin, "ravi@metfraa.com", "Ravi Kumar", "met66",
                       {"metfraa-portal": "", "bsc-portal": "BSC/006"})

    with TestClient(app) as c:
        # login by employee code, case-insensitive
        r = login(c, "MET66", pw)
        assert r.headers["location"].startswith("/change-password")
        assert set_pw(c, pw, "Ravi2026pw").status_code == 303
        home = c.get("/").text
        assert "Metfraa Portal" in home and "BSC Portal" in home
        assert "Work Tracker" not in home  # not granted

        # Metfraa: no in-app ID set -> falls back to the hub employee code (upper-cased)
        r = c.get("/launch/metfraa-portal?next=/ehs", follow_redirects=False)
        loc = urlparse(r.headers["location"])
        assert f"{loc.scheme}://{loc.netloc}{loc.path}" == "https://app.metfraa.com/auth/sso"
        qs = parse_qs(loc.query)
        assert qs["next"] == ["/ehs"]
        claims = jwt.decode(qs["token"][0], mp.sso_secret, algorithms=["HS256"],
                            audience="metfraa-portal", issuer="bsg-hub")
        assert claims["sub"] == "MET66" and claims["email"] == "ravi@metfraa.com"
        assert 0 < claims["exp"] - time.time() <= 61

        # BSC: in-app ID overrides the code
        r = c.get("/launch/bsc-portal", follow_redirects=False)
        tok = parse_qs(urlparse(r.headers["location"]).query)["token"][0]
        claims = jwt.decode(tok, bp.sso_secret, algorithms=["HS256"], audience="bsc-portal", issuer="bsg-hub")
        assert claims["sub"] == "BSC/006" and claims["emp_no"] == "BSC/006"

        # A token for one app is useless at another (different secret AND audience)
        with pytest.raises(jwt.PyJWTError):
            jwt.decode(tok, mp.sso_secret, algorithms=["HS256"], audience="metfraa-portal", issuer="bsg-hub")

        # Not granted -> 403 page, no token
        r = c.get("/launch/worktracker", follow_redirects=False)
        assert r.status_code == 403 and "token" not in r.headers.get("location", "")
        # Non-admins can't reach admin
        assert c.get("/admin/users").status_code == 403


def test_disable_and_password_change_kill_sessions(admin):
    pw = create_person(admin, "kill@metfraa.com", "Kill Switch", "MET900", {"metfraa-portal": ""})
    with TestClient(app) as c:
        login(c, "kill@metfraa.com", pw)
        set_pw(c, pw, "Another2026")
        assert c.get("/").status_code == 200
        old_cookie = c.cookies.get("bsg_session")

        # changing the password elsewhere invalidates the old cookie
        with TestClient(app) as other:
            login(other, "kill@metfraa.com", "Another2026")
            set_pw(other, "Another2026", "Third2026pw")
        c.cookies.set("bsg_session", old_cookie)
        assert c.get("/", follow_redirects=False).status_code == 303

        login(c, "kill@metfraa.com", "Third2026pw")
        assert c.get("/").status_code == 200
        uid = SessionLocal().query(User).filter_by(email="kill@metfraa.com").one().id
        admin.post(f"/admin/users/{uid}/toggle-active", follow_redirects=False)
        r = c.get("/launch/metfraa-portal", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        assert login(c, "kill@metfraa.com", "Third2026pw").status_code == 401


def test_no_lockout_after_wrong_passwords():
    with TestClient(app) as c:
        for _ in range(8):
            r = login(c, "ai@bharatsteels.in", "wrong")
            assert r.status_code == 401 and "Too many" not in r.text
        assert login(c, "ai@bharatsteels.in", ADMIN_PW).status_code == 303


def test_open_to_all_and_hidden_apps(admin):
    pw = create_person(admin, "plain@bharatsteels.in", "Plain User", "", {})
    a = db_app("mis")
    form = {"key": a.key, "name": a.name, "company": a.company, "url": a.url, "launch_mode": "link",
            "identity_field": "employee_code", "icon": a.icon, "sort_order": "5", "is_active": "on",
            "open_to_all": "on"}
    admin.post(f"/admin/apps/{a.id}", data=form)
    with TestClient(app) as c:
        login(c, "plain@bharatsteels.in", pw)
        set_pw(c, pw, "Plain2026pw")
        assert "MIS" in c.get("/").text
        form.pop("is_active")
        admin.post(f"/admin/apps/{a.id}", data=form)
        assert "MIS" not in c.get("/").text
        assert c.get("/launch/mis", follow_redirects=False).status_code == 404


def test_csv_import(admin):
    body = ("email,employee_code,name,company,department,phone,apps\n"
            "anita@bharatsteels.in,BSC104,Anita S,BSC,Accounts,,bsc-portal=BSC/104;qms\n"
            "ravi@metfraa.com,MET66,Ravi Kumar,METFRAA,,,worktracker\n"
            ",X1,No Email,BSC,,,\n"
            "dup@bharatsteels.in,BSC104,Dup Code,BSC,,,\n"
            "bad@bharatsteels.in,,Bad App,BSC,,,nope\n")
    r = admin.post("/admin/users-import", files={"file": ("p.csv", body, "text/csv")})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    by = {row["email"]: row for row in rows}
    assert by["anita@bharatsteels.in"]["result"] == "CREATED" and by["anita@bharatsteels.in"]["temporary_password"]
    assert by["ravi@metfraa.com"]["result"].startswith("EXISTS") and by["ravi@metfraa.com"]["temporary_password"] == ""
    assert by[""]["result"] == "SKIPPED"
    assert "already used" in by["dup@bharatsteels.in"]["notes"]
    assert "unknown app" in by["bad@bharatsteels.in"]["notes"]
    db = SessionLocal()
    anita = db.query(User).filter_by(email="anita@bharatsteels.in").one()
    assert {(g.app.key, g.external_id) for g in anita.access} == {("bsc-portal", "BSC/104"), ("qms", None)}
    ravi = db.query(User).filter_by(email="ravi@metfraa.com").one()
    assert "worktracker" in {g.app.key for g in ravi.access}
    db.close()


def test_rotating_secret_invalidates_old_tokens(admin):
    before = db_app("metfraa-portal")
    admin.post(f"/admin/apps/{before.id}/rotate-secret")
    after = db_app("metfraa-portal")
    assert after.sso_secret != before.sso_secret
    tok = admin.get("/launch/metfraa-portal", follow_redirects=False).headers["location"].split("token=")[1].split("&")[0]
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(tok, before.sso_secret, algorithms=["HS256"], audience="metfraa-portal", issuer="bsg-hub")


def test_status_filter_and_list_toggle(admin):
    create_person(admin, "idle@bharatsteels.in", "Idle Person", "BSC900", {})
    uid = SessionLocal().query(User).filter_by(email="idle@bharatsteels.in").one().id
    r = admin.post(f"/admin/users/{uid}/toggle-active", data={"back": "/admin/users?status=active"},
                   follow_redirects=False)
    assert r.headers["location"] == "/admin/users?status=active"
    assert "Idle Person" not in admin.get("/admin/users?status=active").text
    page = admin.get("/admin/users?status=inactive").text
    assert "Idle Person" in page and "Make active" in page
    r = admin.post(f"/admin/users/{uid}/toggle-active", data={"back": "https://evil.com"}, follow_redirects=False)
    assert r.headers["location"] == f"/admin/users/{uid}?saved=1"
    assert "Idle Person" in admin.get("/admin/users?status=active").text


def test_reset_password_then_login_with_pasted_spaces(admin):
    create_person(admin, "paste@bharatsteels.in", "Paste Person", "BSC/777", {})
    uid = SessionLocal().query(User).filter_by(email="paste@bharatsteels.in").one().id
    r = admin.post(f"/admin/users/{uid}/reset-password")
    pw = re.search(r'class="cred">([^<]+)<', r.text).group(1)
    with TestClient(app) as c:
        assert login(c, "bsc/777", " " + pw + "\n").headers["location"].startswith("/change-password")
    with TestClient(app) as c:
        assert login(c, "BSC/777", pw.lower()).status_code == 401  # case still matters


def test_common_starting_password(admin):
    pw = create_person(admin, "common@bharatsteels.in", "Common Start", "BSC/888", {})
    assert pw == "Bsg@1234"
    with TestClient(app) as c:
        assert login(c, "BSC/888", "Bsg@1234").headers["location"].startswith("/change-password")
        r = set_pw(c, "Bsg@1234", "Bsg@1234")
        assert r.status_code == 400
        r = c.post("/change-password", data={"current": "Bsg@1234", "new_password": "Bsg@1234", "confirm": "Bsg@1234", "next": "/"})
        assert r.status_code == 400
        assert set_pw(c, "Bsg@1234", "MyOwn2026").status_code == 303


def test_forgot_password_by_whatsapp_code(admin, monkeypatch):
    from app import notify
    sent = {}
    monkeypatch.setenv("WATI_BASE_URL", "https://wati.example")
    monkeypatch.setenv("WATI_TOKEN", "t")
    monkeypatch.setattr(notify, "send_whatsapp_code", lambda phone, code: sent.update(phone=phone, code=code) or (True, "sent"))
    form = {"email": "forgot@bharatsteels.in", "name": "Forgot Me", "employee_code": "BSC/555", "company": "BSC",
            "phone": "73959 56648"}
    assert admin.post("/admin/users/new", data=form).status_code == 200
    with TestClient(app) as c:
        assert 'href="/forgot' in c.get("/login").text
        r = c.post("/forgot", data={"identifier": "nobody@x.in"})
        assert r.status_code == 400 and "find an active account" in r.text
        r = c.post("/forgot", data={"identifier": "bsc/555"})
        assert r.status_code == 200 and "+91 ••••• 6648" in r.text and len(sent["code"]) == 6
        code = sent["code"]
        # asking again within a minute does not send a second code
        sent.clear()
        assert "a moment ago" in c.post("/forgot", data={"identifier": "BSC/555"}).text and not sent
        bad = "000000" if code != "000000" else "111111"
        r = c.post("/forgot/verify", data={"identifier": "BSC/555", "code": bad, "new_password": "Fresh2026", "confirm": "Fresh2026"})
        assert r.status_code == 400 and "not right" in r.text
        r = c.post("/forgot/verify", data={"identifier": "BSC/555", "code": code, "new_password": "Fresh2026",
                                           "confirm": "Fresh2026"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/").status_code == 200  # signed in, no forced change
        # the code is single use
        r = c.post("/forgot/verify", data={"identifier": "BSC/555", "code": code, "new_password": "Other2026", "confirm": "Other2026"})
        assert r.status_code == 400
    with TestClient(app) as c:
        assert login(c, "forgot@bharatsteels.in", "Fresh2026").headers["location"] == "/"


def test_forgot_code_burns_after_five_wrong_tries(admin, monkeypatch):
    from app import notify
    sent = {}
    monkeypatch.setenv("WATI_BASE_URL", "https://wati.example")
    monkeypatch.setenv("WATI_TOKEN", "t")
    monkeypatch.setattr(notify, "send_whatsapp_code", lambda phone, code: sent.update(code=code) or (True, "sent"))
    admin.post("/admin/users/new", data={"email": "guess@bharatsteels.in", "name": "Guess", "employee_code": "BSC/556",
                                         "company": "BSC", "phone": "9840011111"})
    with TestClient(app) as c:
        c.post("/forgot", data={"identifier": "BSC/556"})
        bad = "000000" if sent["code"] != "000000" else "111111"
        for _ in range(5):
            c.post("/forgot/verify", data={"identifier": "BSC/556", "code": bad, "new_password": "Fresh2026", "confirm": "Fresh2026"})
        r = c.post("/forgot/verify", data={"identifier": "BSC/556", "code": sent["code"], "new_password": "Fresh2026", "confirm": "Fresh2026"})
        assert r.status_code == 400 and "expired" in r.text


def test_forgot_without_any_channel_explains():
    with TestClient(app) as c:
        r = c.post("/forgot", data={"identifier": "ai@bharatsteels.in"})
        assert r.status_code == 400 and "not set up yet" in r.text


def test_reset_pw_from_list_with_typed_password(admin):
    create_person(admin, "rpw@bharatsteels.in", "Reset Me", "BSC/321", {})
    uid = SessionLocal().query(User).filter_by(email="rpw@bharatsteels.in").one().id
    assert "Reset PW" in admin.get("/admin/users").text
    r = admin.post(f"/admin/users/{uid}/reset-password", data={"password": "abc"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/users?err=pw"
    r = admin.post(f"/admin/users/{uid}/reset-password", data={"password": "Bsc@1234x"})
    assert "Bsc@1234x" in r.text
    with TestClient(app) as c:
        assert login(c, "BSC/321", "Bsc@1234x").headers["location"].startswith("/change-password")
        assert c.post(f"/admin/users/{uid}/reset-password", data={"password": "Hack12345"}, follow_redirects=False).status_code in (303, 403)
    with TestClient(app) as c:  # a non-admin cannot reset anyone
        login(c, "BSC/321", "Bsc@1234x"); set_pw(c, "Bsc@1234x", "Mine2026pw")
        assert c.post(f"/admin/users/{uid}/reset-password", data={"password": "Hack12345"}).status_code == 403
    r = admin.post(f"/admin/users/{uid}/reset-password", data={"password": ""})
    assert "Bsg@1234" in r.text
