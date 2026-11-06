"""Consumer Store — live, READ-ONLY mirror of the SAP Business One Consumer Store
warehouse. Serves the on-hand stock and the recent stock movements as JSON for the
Consumer Store page (templates/consumer_store.html). The page itself is served by
pages.py at /consumer.

Data comes through the same pipeline as the stock MIS: a PowerShell job on the SAP box
(refresh_consumer.ps1) reads SAP and pushes consumer_*.csv to the bsc-stock-data repo;
app/consumer_data.py reads those CSVs. Nothing here is written back to SAP.
"""
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from ..auth import require_employee
from .. import consumer_data as cd

router = APIRouter(prefix="/consumer")


def _json(obj):
    return JSONResponse(json.loads(json.dumps(obj, default=str)))


@router.get("/status")
async def consumer_status(emp: dict = Depends(require_employee)):
    """Whether the SAP mirror is live, which warehouse it reflects, and when it last refreshed."""
    if not cd.is_configured():
        return _json({"configured": False})
    m = await cd.meta()
    # The pipeline is "live" once the CSVs are reachable; use the stock file as the probe.
    try:
        rows = await cd.stock()
        reachable = True
    except Exception:
        reachable = False
    return _json({"configured": reachable,
                  "warehouse": m.get("warehouse"),
                  "warehouse_name": m.get("warehouse_name"),
                  "updated_at": m.get("updated_at"),
                  "company_db": m.get("company_db")})


@router.get("/stock")
async def consumer_stock(emp: dict = Depends(require_employee)):
    if not cd.is_configured():
        return _json({"ok": False, "configured": False, "rows": []})
    try:
        rows = await cd.stock()
    except Exception as e:
        raise HTTPException(502, f"Could not read Consumer Store stock: {e}")
    return _json({"ok": True, "configured": True, "count": len(rows), "rows": rows})


@router.get("/movements")
async def consumer_movements(limit: int = 200, emp: dict = Depends(require_employee)):
    if not cd.is_configured():
        return _json({"ok": False, "configured": False, "rows": []})
    try:
        rows = await cd.movements(min(max(int(limit), 1), 500))
    except Exception as e:
        raise HTTPException(502, f"Could not read Consumer Store movements: {e}")
    return _json({"ok": True, "configured": True, "count": len(rows), "rows": rows})
