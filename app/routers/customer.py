"""Public customer complaint portal (no login). A customer opens /customer.html,
files a complaint and gets a reference number. Submissions land in the same
`quality_complaints` table as source='Customer', status='Open', so they flow into
the internal 3-role workflow. A customer can track status by reference."""
import asyncio
import json
import os
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .. import db
from ..config import APP_NAME
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
    return templates.TemplateResponse("customer.html", {"request": request, "app_name": APP_NAME})


class PublicComplaintIn(BaseModel):
    company: str
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
async def customer_submit(body: PublicComplaintIn):
    await C.ensure_table()
    company = (body.company or "").strip()
    defect = (body.defect_description or "").strip()
    if not company:
        raise HTTPException(400, "Please enter your company name")
    if not defect:
        raise HTTPException(400, "Please describe the issue")
    cno = await C._next_number()
    # cap photos defensively for a public endpoint
    photos = (body.photos or [])[:6]
    assets = await C._handle_assets(cno, {}, photos)
    filed = (body.contact_name or "").strip() or company
    await db.execute(
        "INSERT INTO quality_complaints (complaint_no,source,filed_by,qc_date,customer_name,"
        "customer_email,grade,dimensions,batch_number,invoice_no,quantity,defect_description,"
        "status,payload,created_by) VALUES "
        "($1,'Customer',$2,CURRENT_DATE,$3,$4,$5,$6,$7,$8,$9,$10,'Open',$11,$12)",
        cno, filed, company, (body.email or "").strip(), (body.grade or "").strip(),
        (body.dimensions or "").strip(), (body.batch_number or "").strip(),
        (body.invoice_no or "").strip(), (body.quantity or "").strip(), defect,
        json.dumps({"assets": assets, "phone": (body.phone or "").strip(), "via": "customer-portal"}),
        f"Customer portal · {company}")
    rep = {f: "" for f in C._FIELDS}
    rep.update({"complaint_no": cno, "source": "Customer", "filed_by": filed,
                "customer_name": company, "customer_email": (body.email or "").strip(),
                "grade": body.grade or "", "dimensions": body.dimensions or "",
                "batch_number": body.batch_number or "", "invoice_no": body.invoice_no or "",
                "quantity": body.quantity or "", "defect_description": defect,
                "status": "Open", "created_by": filed, "assets": assets})

    async def _bg():
        await C._write_pdf(cno, rep)
        await C._append_csv(rep, f"Customer portal · {company}")
    asyncio.create_task(_bg())
    return {"ok": True, "ref": cno}


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
