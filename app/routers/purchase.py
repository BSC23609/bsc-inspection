"""Purchase Request module — staff raise material/spare purchase requests; admins
approve, reject or mark them purchased.

Visibility mirrors the rest of the app:
  * Admins see every request (and can change its status).
  * A normal user sees only the requests they themselves submitted.

One submission = one row in purchase_requests. The three on-screen sections
(Requester / Item details / Priority & cost) are just a grouping of that row's
fields, not separate tables.
"""
import os
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse

from .. import db, wati
from .. import consumer_data as cd
from ..auth import require_employee

router = APIRouter(prefix="/purchase")
IST = ZoneInfo("Asia/Kolkata")

# bump on each deploy to confirm the new build is live  (GET /purchase/version)
PURCHASE_BUILD = "2026-11-03-v8-spec-partmake"

STATUSES = ("Pending", "Approved", "Rejected", "Purchased")

# --- WhatsApp approval wiring (all env-driven; set in Vercel, no redeploy of code) ---
# The single approver who receives every new request and taps Approve/Reject in WhatsApp.
APPROVER_NUMBER  = (os.environ.get("PURCHASE_APPROVER_NUMBER") or "").strip()
# The store/purchase desk that is auto-notified when a request is Approved.
STORE_NUMBER     = (os.environ.get("PURCHASE_STORE_NUMBER") or "").strip()
# Approved WATI template names (create these in WATI; see the setup notes).
REQ_TEMPLATE     = (os.environ.get("PURCHASE_REQUEST_TEMPLATE") or "bsc_purchase_request").strip()
STORE_TEMPLATE   = (os.environ.get("PURCHASE_STORE_TEMPLATE") or "bsc_purchase_approved").strip()
# Optional shared secret appended to the WATI webhook URL (?token=...), checked on inbound.
HOOK_TOKEN       = (os.environ.get("PURCHASE_HOOK_TOKEN") or "").strip()

# Which button text maps to which status. WATI sends the button's label as the
# reply text; we match case-insensitively on a keyword so small wording changes
# in the template still resolve.
_BTN_STATUS = [("approve", "Approved"), ("reject", "Rejected"),
               ("purchas", "Purchased")]

_READY = False


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)),
                        headers={"Cache-Control": "no-store"})


async def ensure_tables():
    """Create the purchase_requests table once per warm instance."""
    global _READY
    if _READY:
        return
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS purchase_requests (
            id              SERIAL PRIMARY KEY,
            requester_emp_id INT,
            -- Section 1: Requester
            req_name        TEXT,
            department      TEXT,
            request_date    DATE,
            whatsapp        TEXT,
            -- Section 2: Item details
            item_name       TEXT,
            category        TEXT,
            specification   TEXT,
            quantity        NUMERIC,
            unit            TEXT,
            purpose         TEXT,
            -- Section 3: Priority and cost
            urgency         TEXT,
            required_by     DATE,
            est_cost        NUMERIC,
            preferred_supplier TEXT,
            in_stock        TEXT,
            remarks         TEXT,
            -- workflow
            status          TEXT DEFAULT 'Pending',
            created_at      TIMESTAMPTZ DEFAULT now(),
            decided_by      INT,
            decided_at      TIMESTAMPTZ
        )
        """)
    # Added after v1 shipped — safe on an existing table.
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS location TEXT")
    # v4: WhatsApp approval workflow bookkeeping.
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS approver_notified BOOLEAN DEFAULT FALSE")
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS store_notified BOOLEAN DEFAULT FALSE")
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS wa_detail TEXT")
    # v5: per-request token for the WhatsApp Approve/Reject URL buttons.
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS act_token TEXT")
    # v8: part number / make kept separate from specification.
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS part_make TEXT")
    # SAP item code when the requester picks the item from the Consumer Store (links the
    # request to live warehouse-46 stock). NULL for a free-typed item.
    await db.execute("ALTER TABLE purchase_requests ADD COLUMN IF NOT EXISTS sap_item_code TEXT")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_pr_token ON purchase_requests (act_token)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS ix_pr_requester ON purchase_requests (requester_emp_id)")
    await db.execute(
        "CREATE INDEX IF NOT EXISTS ix_pr_status ON purchase_requests (status)")
    _READY = True


def _is_admin(emp) -> bool:
    return bool(emp and emp.get("is_admin"))


def _num(v):
    """Parse a number from the form; blank -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _s(v, limit=2000):
    return (str(v).strip()[:limit]) if v is not None else ""


def _date(v):
    """Accept YYYY-MM-DD from <input type=date>; blank -> None."""
    s = _s(v, 20)
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _ref(rid) -> str:
    return "PR-%s" % rid


def _tidy(v) -> str:
    """WhatsApp rejects a template parameter containing a newline/tab or 4+ spaces."""
    s = " ".join(str(v if v is not None else "").split())
    return s or "-"


def _req_params(r: dict) -> dict:
    """NAMED template parameters (gatepass style). The WATI template body must use
    these variable names: {{ref}} {{requester}} {{item}} {{qty}} {{category}}
    {{location}} {{urgency}} {{purpose}} {{cost}}. The two URL buttons use {{token}},
    which the caller adds for the approver message only."""
    qty = r.get("quantity")
    qty_s = ("%g %s" % (float(qty), r.get("unit") or "")).strip() if qty not in (None, "") else "-"
    who = _tidy(r.get("req_name") or r.get("requester_name") or "")
    dept = _tidy(r.get("department") or "")
    cost = r.get("est_cost")
    cost_s = ("Rs %g" % float(cost)) if cost not in (None, "") else "-"
    return {
        "ref": _ref(r.get("id")),
        "requester": who + (" (" + dept + ")" if dept and dept != "-" else ""),
        "item": _tidy(r.get("item_name")),
        "qty": _tidy(qty_s),
        "category": _tidy(r.get("category") or "-"),
        "location": _tidy(r.get("location") or "-"),
        "urgency": _tidy(r.get("urgency") or "Normal"),
        "purpose": _tidy(r.get("purpose") or "-"),
        "cost": _tidy(cost_s),
    }


async def _get_row(pid) -> dict:
    r = await db.fetchrow(
        "SELECT p.*, e.name AS requester_name, e.emp_no AS requester_emp_no "
        "FROM purchase_requests p LEFT JOIN employees e ON e.id=p.requester_emp_id "
        "WHERE p.id=$1", int(pid))
    return dict(r) if r else None


async def _store_note(row) -> str:
    """Live Consumer Store (SAP warehouse 46) availability for a request linked by its
    EXACT SAP item code — a short line for the WhatsApp approval message. '' when the
    request isn't linked or the store can't be read."""
    code = (row.get("sap_item_code") or "").strip()
    if not code:
        return ""
    try:
        m = await cd.stock_map([code])
    except Exception:
        return ""
    s = m.get(code)
    if not s:
        return ""
    av = s.get("available") or 0
    uom = (s.get("uom") or "").strip()
    try:
        av = float(av)
    except (TypeError, ValueError):
        av = 0
    return ("In store: %g %s available" % (av, uom)).strip() if av > 0 else "Not currently in store"


async def _notify_approver(pid):
    """Send the new request to the single approver's WhatsApp (Approve/Reject buttons
    live on the approved WATI template). Never raises — a WATI failure must not fail
    the submit; the detail is stored for the diagnostics endpoint."""
    if not APPROVER_NUMBER:
        return False, "no approver number configured (set PURCHASE_APPROVER_NUMBER)"
    row = await _get_row(pid)
    if not row:
        return False, "row not found"
    # Named body params + {{token}} for the two dynamic URL buttons (Approve,
    # Reject) — both buttons reference the same {{token}}, gatepass style.
    params = _req_params(row)
    note = await _store_note(row)
    if note:   # fold live store stock into the item line (no WATI template change needed)
        params["item"] = _tidy((params.get("item") or "") + " — " + note)
    tok = row.get("act_token")
    if tok:
        params["token"] = tok
    try:
        ok, detail = await wati.send_template(REQ_TEMPLATE, APPROVER_NUMBER, params)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "send error: %s" % e
    try:
        await db.execute(
            "UPDATE purchase_requests SET approver_notified=$1, wa_detail=$2 WHERE id=$3",
            bool(ok), str(detail)[:500], int(pid))
    except Exception:
        pass
    return ok, detail


async def _notify_store(pid):
    """Auto-send an approved request's details to the store/purchase desk. Guarded by
    store_notified so it fires once per request."""
    if not STORE_NUMBER:
        return False, "no store number configured (set PURCHASE_STORE_NUMBER)"
    row = await _get_row(pid)
    if not row:
        return False, "row not found"
    if row.get("store_notified"):
        return True, "already notified"
    params = _req_params(row)
    note = await _store_note(row)
    if note:
        params["item"] = _tidy((params.get("item") or "") + " — " + note)
    try:
        ok, detail = await wati.send_template(STORE_TEMPLATE, STORE_NUMBER, params)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, "send error: %s" % e
    if ok:
        try:
            await db.execute("UPDATE purchase_requests SET store_notified=TRUE WHERE id=$1", int(pid))
        except Exception:
            pass
    return ok, detail


async def _apply_status(pid, status, decided_by=None):
    """Set a request's status; on Approved, auto-notify the store exactly once.
    Returns (ok, detail). Shared by the admin dropdown and the WhatsApp webhook."""
    res = await db.execute(
        "UPDATE purchase_requests SET status=$1, decided_by=$2, decided_at=now() WHERE id=$3",
        status, decided_by, int(pid))
    if res.endswith("0"):
        return False, "not found"
    store = ""
    if status == "Approved":
        sok, sdetail = await _notify_store(pid)
        store = ("store: " + ("sent" if sok else sdetail))
    return True, store


def _result_page(title: str, msg: str, ok: bool = True) -> str:
    colour = "#0a7d55" if ok else "#c23438"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>%s</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;"
        "background:#f4f6f9;margin:0;padding:0'>"
        "<div style='max-width:460px;margin:12vh auto;background:#fff;border-radius:16px;"
        "box-shadow:0 10px 30px rgba(8,24,40,.12);padding:28px 24px;text-align:center'>"
        "<div style='width:56px;height:56px;border-radius:50%%;margin:0 auto 14px;"
        "background:%s;color:#fff;font-size:30px;line-height:56px'>%s</div>"
        "<h2 style='margin:0 0 8px;color:#0f1b2d'>%s</h2>"
        "<p style='margin:0 0 18px;color:#51607a;font-size:14px'>%s</p>"
        "<a href='/purchase' style='display:inline-block;background:#0f1b2d;color:#fff;"
        "text-decoration:none;padding:10px 18px;border-radius:9px;font-weight:600;font-size:14px'>"
        "Open Purchase Requests</a></div></body></html>"
        % (title, colour, "✓" if ok else "!", title, msg))


async def _act_by_token(token: str, status: str):
    """Apply a decision from a WhatsApp URL button (token identifies the exact
    request, so correlation is precise and no login is needed)."""
    await ensure_tables()
    row = await db.fetchrow(
        "SELECT id, status FROM purchase_requests WHERE act_token=$1", (token or "").strip())
    if not row:
        return HTMLResponse(_result_page("Link not valid",
                            "This approval link is invalid or has expired.", ok=False),
                            status_code=404)
    ref = _ref(row["id"])
    if row["status"] == status:
        extra = " The store was already notified." if status == "Approved" else ""
        return HTMLResponse(_result_page("Already " + status,
                            "%s is already marked %s.%s" % (ref, status, extra)))
    ok, detail = await _apply_status(row["id"], status, None)
    if not ok:
        return HTMLResponse(_result_page("Not found", "That request no longer exists.", ok=False),
                            status_code=404)
    note = " The store has been notified on WhatsApp." if status == "Approved" else ""
    return HTMLResponse(_result_page(status, "%s has been marked %s.%s" % (ref, status, note)))


def _decide_page(row: dict) -> str:
    ref = _ref(row.get("id"))
    tok = row.get("act_token")
    qty = row.get("quantity")
    qty_s = ("%g %s" % (float(qty), row.get("unit") or "")).strip() if qty not in (None, "") else "-"
    cost = row.get("est_cost")
    cost_s = ("Rs %g" % float(cost)) if cost not in (None, "") else "-"
    rowsdef = [
        ("Requested by", (row.get("req_name") or row.get("requester_name") or "-")),
        ("Item", row.get("item_name") or "-"),
        ("Quantity", qty_s),
        ("Category", row.get("category") or "-"),
        ("Location", row.get("location") or "-"),
        ("Urgency", row.get("urgency") or "Normal"),
        ("Purpose / machine", row.get("purpose") or "-"),
        ("Estimated cost", cost_s),
        ("Specification", row.get("specification") or "-"),
    ]
    import html as _h
    cells = "".join(
        "<tr><td style='padding:7px 10px;color:#8a93a3;font-size:12px;white-space:nowrap;"
        "vertical-align:top'>%s</td><td style='padding:7px 10px;color:#0f1b2d;font-size:13px;"
        "font-weight:600'>%s</td></tr>" % (_h.escape(str(k)), _h.escape(str(v)))
        for k, v in rowsdef)
    status = row.get("status") or "Pending"
    pill = ("<span style='font-size:12px;font-weight:700;padding:3px 10px;border-radius:20px;"
            "background:#fff3e0;color:#9a6a1a'>%s</span>" % _h.escape(status))

    def btn(action, label, bg):
        return ("<a href='/purchase/%s/%s' style='flex:1;text-align:center;background:%s;color:#fff;"
                "text-decoration:none;padding:13px 10px;border-radius:10px;font-weight:700;"
                "font-size:14px'>%s</a>" % (action, tok, bg, label))

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>%s</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f4f6f9;margin:0'>"
        "<div style='max-width:480px;margin:5vh auto;background:#fff;border-radius:16px;"
        "box-shadow:0 10px 30px rgba(8,24,40,.12);overflow:hidden'>"
        "<div style='padding:18px 20px;border-bottom:1px solid #eef1f5;display:flex;align-items:center;"
        "justify-content:space-between'><div><div style='font-size:12px;color:#8a93a3;font-weight:700;"
        "text-transform:uppercase;letter-spacing:.4px'>Purchase Request</div>"
        "<div style='font-size:18px;font-weight:800;color:#0f1b2d'>%s</div></div>%s</div>"
        "<table style='width:100%%;border-collapse:collapse'>%s</table>"
        "<div style='padding:16px 20px;border-top:1px solid #eef1f5'>"
        "<div style='font-size:12px;color:#8a93a3;margin-bottom:10px'>Choose a decision:</div>"
        "<div style='display:flex;gap:10px'>%s%s%s</div></div></div></body></html>"
        % (ref, ref, pill, cells,
           btn("approve", "Approve", "#0a7d55"),
           btn("reject", "Reject", "#c23438"),
           btn("purchased", "Purchased", "#1d4e74")))


@router.get("/decide/{token}")
async def decide(token: str):
    """Page opened by the single dynamic URL button in WhatsApp — shows the exact
    request and its Approve / Reject / Purchased actions."""
    await ensure_tables()
    row = await db.fetchrow(
        "SELECT p.*, e.name AS requester_name FROM purchase_requests p "
        "LEFT JOIN employees e ON e.id=p.requester_emp_id WHERE p.act_token=$1", (token or "").strip())
    if not row:
        return HTMLResponse(_result_page("Link not valid",
                            "This request link is invalid or has expired.", ok=False), status_code=404)
    return HTMLResponse(_decide_page(dict(row)))


@router.get("/approve/{token}")
async def act_approve(token: str):
    return await _act_by_token(token, "Approved")


@router.get("/reject/{token}")
async def act_reject(token: str):
    return await _act_by_token(token, "Rejected")


@router.get("/purchased/{token}")
async def act_purchased(token: str):
    return await _act_by_token(token, "Purchased")


@router.get("/version")
async def version():
    return _json({"build": PURCHASE_BUILD})


@router.get("/list")
async def pr_list(emp: dict = Depends(require_employee)):
    """Admins get every request; a normal user gets only their own."""
    await ensure_tables()
    if _is_admin(emp):
        rows = await db.fetch(
            "SELECT p.*, e.name AS requester_name, e.emp_no AS requester_emp_no "
            "FROM purchase_requests p "
            "LEFT JOIN employees e ON e.id = p.requester_emp_id "
            "ORDER BY p.created_at DESC, p.id DESC")
    else:
        rows = await db.fetch(
            "SELECT p.*, e.name AS requester_name, e.emp_no AS requester_emp_no "
            "FROM purchase_requests p "
            "LEFT JOIN employees e ON e.id = p.requester_emp_id "
            "WHERE p.requester_emp_id = $1 "
            "ORDER BY p.created_at DESC, p.id DESC", emp.get("id"))
    out = [dict(r) for r in rows]
    # Attach live Consumer Store (SAP warehouse 46) stock to any request that was linked
    # to a store item, so the requester/approver sees what's already on hand.
    try:
        smap = await cd.stock_map([r.get("sap_item_code") for r in out])
        for r in out:
            code = (r.get("sap_item_code") or "").strip()
            if code and code in smap:
                r["sap_stock"] = smap[code]
    except Exception:
        pass
    return _json({"is_admin": _is_admin(emp), "statuses": list(STATUSES), "rows": out})


@router.post("/create")
async def pr_create(body: dict, emp: dict = Depends(require_employee)):
    """Submit a new purchase request. The submitter is always recorded as the
    requester so the 'own requests only' filter is enforced server-side."""
    await ensure_tables()
    item = _s(body.get("item_name"), 400)
    if not item:
        raise HTTPException(400, "Item name / description is required")
    spec = _s(body.get("specification"), 1000)
    if not spec:
        raise HTTPException(400, "Specification / model is required")
    rid = await db.fetchval(
        """
        INSERT INTO purchase_requests
            (requester_emp_id, req_name, department, request_date, whatsapp,
             item_name, category, specification, part_make, quantity, unit, purpose, location,
             urgency, required_by, est_cost, preferred_supplier, in_stock, remarks,
             sap_item_code, status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,'Pending')
        RETURNING id
        """,
        emp.get("id"),
        _s(body.get("req_name"), 200) or (emp.get("name") or emp.get("emp_no") or ""),
        _s(body.get("department"), 200),
        _date(body.get("request_date")),
        _s(body.get("whatsapp"), 40),
        item,
        _s(body.get("category"), 60),
        spec,
        _s(body.get("part_make"), 400),
        _num(body.get("quantity")),
        _s(body.get("unit"), 40),
        _s(body.get("purpose"), 600),
        _s(body.get("location"), 60),
        _s(body.get("urgency"), 120),
        _date(body.get("required_by")),
        _num(body.get("est_cost")),
        _s(body.get("preferred_supplier"), 200),
        _s(body.get("in_stock"), 10),
        _s(body.get("remarks"), 1000),
        _s(body.get("sap_item_code"), 80),
    )
    # Unique token that the WhatsApp Approve/Reject URL buttons carry.
    await db.execute("UPDATE purchase_requests SET act_token=$1 WHERE id=$2",
                     uuid.uuid4().hex, int(rid))
    # Fire the approver's WhatsApp. Never blocks or fails the submit.
    wa_ok, wa_detail = await _notify_approver(rid)
    return _json({"ok": True, "id": rid, "ref": _ref(rid),
                  "approver_notified": bool(wa_ok), "wa_detail": wa_detail})


@router.post("/status")
async def pr_status(body: dict, emp: dict = Depends(require_employee)):
    """Admin-only: move a request between Pending / Approved / Rejected / Purchased."""
    await ensure_tables()
    if not _is_admin(emp):
        raise HTTPException(403, "Only admins can change a request's status")
    pid = body.get("id")
    status = _s(body.get("status"), 20)
    if not pid or status not in STATUSES:
        raise HTTPException(400, "Bad id or status")
    ok, detail = await _apply_status(pid, status, emp.get("id"))
    if not ok:
        raise HTTPException(404, "Request not found")
    return _json({"ok": True, "id": pid, "status": status, "detail": detail})


# ---------------------------------------------------------------------------
# WhatsApp webhook — WATI posts here when the approver taps an Approve / Reject /
# Purchased button on the request message. Configure this URL in WATI:
#   https://qms.bharatsteels.in/purchase/wati-hook?token=<PURCHASE_HOOK_TOKEN>
# ---------------------------------------------------------------------------
def _hook_sender(payload: dict) -> str:
    for k in ("waId", "whatsappNumber", "senderNumber", "number", "wa_id", "from"):
        v = payload.get(k)
        if v:
            return wati.normalize_number(v)
    return ""


def _hook_button_text(payload: dict) -> str:
    # WATI shapes vary by account/version — check the common places.
    for k in ("text", "buttonText", "body"):
        v = payload.get(k)
        if v:
            return str(v)
    for parent in ("buttonReply", "interactive", "button", "listReply"):
        node = payload.get(parent)
        if isinstance(node, dict):
            for k in ("text", "title", "payload", "body"):
                if node.get(k):
                    return str(node[k])
    return ""


def _status_from_text(text: str) -> str:
    t = (text or "").strip().lower()
    for key, st in _BTN_STATUS:
        if key in t:
            return st
    for st in STATUSES:          # also accept the plain status word
        if st.lower() in t:
            return st
    return ""


@router.post("/wati-hook")
async def wati_hook(request: Request):
    """Receive the approver's button tap. Tolerant of WATI's payload shapes."""
    await ensure_tables()
    if HOOK_TOKEN and request.query_params.get("token") != HOOK_TOKEN:
        raise HTTPException(403, "bad token")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    sender = _hook_sender(payload)
    status = _status_from_text(_hook_button_text(payload))
    # Only the configured approver may decide; ignore anything else (delivery
    # receipts, other inbound messages) quietly so WATI doesn't keep retrying.
    if not status or (APPROVER_NUMBER and sender and
                      wati.normalize_number(APPROVER_NUMBER) != sender):
        return _json({"ok": True, "ignored": True})
    # Correlate to a request: the most recent one still Pending that was sent to
    # the approver. (Single approver + prompt action makes this reliable; the web
    # register remains the source of truth and an admin can correct any mismatch.)
    row = await db.fetchrow(
        "SELECT id FROM purchase_requests WHERE status='Pending' AND approver_notified=TRUE "
        "ORDER BY created_at DESC, id DESC LIMIT 1")
    if not row:
        row = await db.fetchrow(
            "SELECT id FROM purchase_requests WHERE status='Pending' ORDER BY created_at DESC, id DESC LIMIT 1")
    if not row:
        return _json({"ok": True, "no_pending": True})
    ok, detail = await _apply_status(row["id"], status, None)
    return _json({"ok": ok, "id": row["id"], "status": status, "detail": detail})


@router.get("/wati-config")
async def wati_config(emp: dict = Depends(require_employee)):
    """Admin diagnostics: is the approval wiring configured? Numbers are masked."""
    if not _is_admin(emp):
        raise HTTPException(403, "Admins only")
    def _mask(n):
        d = wati.normalize_number(n)
        return (d[:4] + "***" + d[-2:]) if len(d) >= 6 else ("set" if d else "")
    return _json({
        "wati": wati.status().get("configured", False),
        "approver_number_set": bool(APPROVER_NUMBER), "approver_number": _mask(APPROVER_NUMBER),
        "store_number_set": bool(STORE_NUMBER), "store_number": _mask(STORE_NUMBER),
        "request_template": REQ_TEMPLATE, "store_template": STORE_TEMPLATE,
        "hook_token_set": bool(HOOK_TOKEN),
        "webhook_url": "/purchase/wati-hook" + ("?token=<set>" if HOOK_TOKEN else ""),
    })
