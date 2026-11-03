"""Purchase Request module — staff raise material/spare purchase requests; admins
approve, reject or mark them purchased.

Visibility mirrors the rest of the app:
  * Admins see every request (and can change its status).
  * A normal user sees only the requests they themselves submitted.

One submission = one row in purchase_requests. The three on-screen sections
(Requester / Item details / Priority & cost) are just a grouping of that row's
fields, not separate tables.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import db
from ..auth import require_employee

router = APIRouter(prefix="/purchase")
IST = ZoneInfo("Asia/Kolkata")

# bump on each deploy to confirm the new build is live  (GET /purchase/version)
PURCHASE_BUILD = "2026-11-03-v3-location"

STATUSES = ("Pending", "Approved", "Rejected", "Purchased")

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
    return _json({"is_admin": _is_admin(emp), "statuses": list(STATUSES),
                  "rows": [dict(r) for r in rows]})


@router.post("/create")
async def pr_create(body: dict, emp: dict = Depends(require_employee)):
    """Submit a new purchase request. The submitter is always recorded as the
    requester so the 'own requests only' filter is enforced server-side."""
    await ensure_tables()
    item = _s(body.get("item_name"), 400)
    if not item:
        raise HTTPException(400, "Item name / description is required")
    rid = await db.fetchval(
        """
        INSERT INTO purchase_requests
            (requester_emp_id, req_name, department, request_date, whatsapp,
             item_name, category, specification, quantity, unit, purpose, location,
             urgency, required_by, est_cost, preferred_supplier, in_stock, remarks,
             status)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,'Pending')
        RETURNING id
        """,
        emp.get("id"),
        _s(body.get("req_name"), 200) or (emp.get("name") or emp.get("emp_no") or ""),
        _s(body.get("department"), 200),
        _date(body.get("request_date")),
        _s(body.get("whatsapp"), 40),
        item,
        _s(body.get("category"), 60),
        _s(body.get("specification"), 1000),
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
    )
    return _json({"ok": True, "id": rid})


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
    res = await db.execute(
        "UPDATE purchase_requests SET status=$1, decided_by=$2, decided_at=now() WHERE id=$3",
        status, emp.get("id"), int(pid))
    if res.endswith("0"):
        raise HTTPException(404, "Request not found")
    return _json({"ok": True, "id": pid, "status": status})
