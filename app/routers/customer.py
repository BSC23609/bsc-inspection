"""Customer complaint portal — login-gated, reusing the legacy `customers` table.

A customer opens /customer, signs in with their existing account (the same
accounts as the old app), then can raise a complaint and track all of their own
complaints. Submissions land in the same `quality_complaints` table as
source='Customer', status='Open', so they flow into the internal 3-role
workflow. Complaints are tied to the signed-in customer (id + email) so each
customer only ever sees their own.
"""
import asyncio
import json
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import db
from ..auth import require_employee
from ..config import APP_NAME
from .. import customer_auth as CA
from . import complaint as C

router = APIRouter(tags=["customer"])
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
templates = Jinja2Templates(directory=os.path.join(_BASE, "templates"))

# customer-visible status wording (internal workflow status -> public label)
_PUBLIC_STATUS = {
    "Open": "Received",
    "Under Review": "Under review",
    "Vendor Notified": "Under review (with supplier)",
    "Escalated for Re-review": "Re-opened",
    "Pending Sales Review": "In progress",
    "Awaiting Customer Response": "Resolution shared",
    "Resolved": "Resolved",
    "Closed": "Closed",
    "Declined": "Closed",
}


@router.get("/customer", response_class=HTMLResponse)
@router.get("/customer.html", response_class=HTMLResponse)
async def customer_page(request: Request):
    cust = await CA.current_customer(request)
    prof = CA.profile(cust) if cust else None
    return templates.TemplateResponse(
        "customer.html",
        {"request": request, "app_name": APP_NAME,
         "customer": prof, "customer_json": json.dumps(prof)})


# ---- auth ----
class LoginIn(BaseModel):
    identifier: str
    password: str


_LOGIN_ERR = {
    "no_table": "Customer accounts aren't available right now.",
    "schema": "Customer accounts aren't available right now.",
    "nouser": "No account found with those details.",
    "badpw": "Incorrect password. Please try again.",
    "inactive": "This account is inactive. Please contact us.",
    "error": "Couldn't sign in right now. Please try again.",
}


@router.post("/customer/login")
async def customer_login(body: LoginIn):
    u, reason = await CA.authenticate(body.identifier, body.password)
    if not u:
        raise HTTPException(401, _LOGIN_ERR.get(reason, "Sign-in failed."))
    token = await CA.make_token(u)
    resp = JSONResponse({"ok": True})
    CA.set_cookie(resp, token)
    return resp


@router.post("/customer/logout")
async def customer_logout():
    resp = JSONResponse({"ok": True})
    CA.clear_cookie(resp)
    return resp


@router.get("/customer/me")
async def customer_me(request: Request):
    cust = await CA.current_customer(request)
    if not cust:
        return JSONResponse({"authed": False}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"authed": True, "customer": CA.profile(cust)},
                        headers={"Cache-Control": "no-store"})


# ---- complaint intake (authenticated) ----
class PublicComplaintIn(BaseModel):
    contact_name: Optional[str] = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    grade: Optional[str] = ""
    dimensions: Optional[str] = ""
    batch_number: Optional[str] = ""
    invoice_no: Optional[str] = ""
    quantity: Optional[str] = ""
    defect_description: str
    photos: list = []


@router.post("/customer/submit")
async def customer_submit(body: PublicComplaintIn, request: Request):
    cust = await CA.current_customer(request)
    if not cust:
        raise HTTPException(401, "Please sign in first")
    prof = CA.profile(cust)
    await C.ensure_table()

    company = (prof.get("company") or "").strip() or "Customer"
    defect = (body.defect_description or "").strip()
    if not defect:
        raise HTTPException(400, "Please describe the issue")
    email = (body.email or "").strip() or (prof.get("email") or "").strip()
    phone = (body.phone or "").strip() or str(prof.get("phone") or "").strip()
    contact = (body.contact_name or "").strip() or (prof.get("contact") or "").strip() or company

    cno = await C._next_number()
    photos = (body.photos or [])[:6]
    assets = await C._handle_assets(cno, {}, photos)
    await db.execute(
        "INSERT INTO quality_complaints (complaint_no,source,filed_by,qc_date,customer_name,"
        "customer_email,grade,dimensions,batch_number,invoice_no,quantity,defect_description,"
        "status,payload,created_by) VALUES "
        "($1,'Customer',$2,CURRENT_DATE,$3,$4,$5,$6,$7,$8,$9,$10,'Open',$11,$12)",
        cno, contact, company, email, (body.grade or "").strip(),
        (body.dimensions or "").strip(), (body.batch_number or "").strip(),
        (body.invoice_no or "").strip(), (body.quantity or "").strip(), defect,
        json.dumps({"assets": assets, "phone": phone, "via": "customer-portal",
                    "customer_id": str(prof.get("id") or "")}),
        f"Customer portal · {company}")
    rep = {f: "" for f in C._FIELDS}
    rep.update({"complaint_no": cno, "source": "Customer", "filed_by": contact,
                "customer_name": company, "customer_email": email,
                "grade": body.grade or "", "dimensions": body.dimensions or "",
                "batch_number": body.batch_number or "", "invoice_no": body.invoice_no or "",
                "quantity": body.quantity or "", "defect_description": defect,
                "status": "Open", "created_by": contact, "assets": assets})

    async def _bg():
        await C._write_pdf(cno, rep)
        await C._append_csv(rep, f"Customer portal · {company}")
    asyncio.create_task(_bg())
    return {"ok": True, "ref": cno}


@router.get("/customer/mine")
async def customer_mine(request: Request):
    cust = await CA.current_customer(request)
    if not cust:
        raise HTTPException(401, "Please sign in first")
    prof = CA.profile(cust)
    await C.ensure_table()
    cid = str(prof.get("id") or "")
    email = (prof.get("email") or "").strip()
    rows = await db.fetch(
        "SELECT complaint_no, status, defect_description, customer_message, resolution, "
        "qc_date, created_at, updated_at FROM quality_complaints "
        "WHERE (payload->>'customer_id' = $1 AND $1 <> '') "
        "   OR (customer_email <> '' AND lower(customer_email) = lower($2) AND $2 <> '') "
        "ORDER BY created_at DESC NULLS LAST, id DESC LIMIT 100",
        cid, email)
    out = []
    for r in rows:
        d = dict(r)
        st = d.get("status") or "Open"
        out.append({
            "ref": d["complaint_no"],
            "status": _PUBLIC_STATUS.get(st, st),
            "defect": d.get("defect_description") or "",
            "message": d.get("customer_message") or d.get("resolution") or "",
            "date": str(d.get("qc_date") or "")[:10],
            "updated": str(d.get("updated_at") or d.get("created_at") or "")[:16],
        })
    return JSONResponse({"items": out}, headers={"Cache-Control": "no-store"})


@router.get("/customer/_schema")
async def customer_schema(emp: dict = Depends(require_employee)):
    """Staff-only: what the login layer detected about the `customers` table."""
    return JSONResponse(await CA.status(), headers={"Cache-Control": "no-store"})


@router.get("/customer/track")
async def customer_track(ref: str = Query(...)):
    await C.ensure_table()
    row = await db.fetchrow(
        "SELECT complaint_no, customer_name, status, defect_description, resolution, "
        "customer_message, created_at, updated_at FROM quality_complaints WHERE complaint_no=$1",
        ref.strip())
    if not row:
        raise HTTPException(404, "No complaint found with that reference")
    d = dict(row)
    status = d.get("status") or "Open"
    return JSONResponse({
        "ref": d["complaint_no"], "company": d.get("customer_name"),
        "status": _PUBLIC_STATUS.get(status, status),
        "defect": d.get("defect_description"),
        "message": d.get("customer_message") or d.get("resolution") or "",
        "updated": str(d.get("updated_at") or d.get("created_at") or "")[:16],
    }, headers={"Cache-Control": "no-store"})
