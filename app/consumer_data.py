"""Consumer Store data source — reads the live SAP mirror via the BSC MIS pipeline.

The QMS app NEVER talks to SAP directly. Exactly like the stock MIS, a PowerShell job on
the SAP box (C:\\scripts\\refresh_consumer.ps1) reads the Consumer Store warehouse from SAP
Business One (10.10.250.11 / BSC_FINAL_2707) and pushes three CSVs to the bsc-stock-data
GitHub repo — consumer_stock.csv, consumer_movements.csv, consumer_meta.csv. This module
just fetches those CSVs (raw.githubusercontent.com). Because the data lives in a separate
repo that the app host does not watch, a data refresh never redeploys the QMS app.
SAP stays the single source of truth; QMS only reflects it.
"""
import csv
import io
import time

import httpx

from .config import CONSUMER_DATA_BASE, SAP_COMPANY_DB

_BASE = (CONSUMER_DATA_BASE or "").rstrip("/") + "/"

# OINM.TransType -> readable document name (common B1 object types)
TRANS_TYPES = {
    13: "A/R Invoice", 14: "A/R Credit Note", 15: "Delivery", 16: "Return",
    18: "A/P Invoice", 19: "A/P Credit Note", 20: "Goods Receipt PO", 21: "Goods Return",
    59: "Goods Receipt", 60: "Goods Issue", 67: "Inventory Transfer",
    162: "Inventory Revaluation", 202: "Production Order", 310: "Inventory Counting",
}

_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 60  # seconds — the SAP job refreshes every 15 min, so a 1-min cache is plenty


def is_configured() -> bool:
    """True once a data base URL is set (the pipeline writes the CSVs there)."""
    return bool(CONSUMER_DATA_BASE)


async def _csv(name: str, required: bool = True) -> list[dict]:
    """Fetch one CSV from the data repo and parse it into dict rows (60s cached)."""
    if not _BASE.strip("/"):
        raise RuntimeError("CONSUMER_DATA_BASE is not configured")
    url = _BASE + name
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        r = await c.get(url, headers={"Cache-Control": "no-cache"})
    if r.status_code == 404 and not required:
        _cache[url] = (now, [])
        return []
    if r.status_code >= 300:
        raise RuntimeError(f"{name} not reachable ({r.status_code})")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    _cache[url] = (now, rows)
    return rows


def _num(v) -> float:
    try:
        return round(float(str(v).replace(",", "").strip() or 0), 3)
    except (TypeError, ValueError):
        return 0.0


async def meta() -> dict:
    """Warehouse + last-refresh info written by refresh_consumer.ps1 (best-effort)."""
    try:
        rows = await _csv("consumer_meta.csv", required=False)
    except Exception:
        rows = []
    m = rows[0] if rows else {}
    return {
        "warehouse": m.get("warehouse") or None,
        "warehouse_name": m.get("warehouse_name") or None,
        "updated_at": m.get("updated_at") or None,
        "company_db": m.get("company_db") or SAP_COMPANY_DB,
    }


async def stock() -> list[dict]:
    """On-hand stock in the Consumer Store warehouse (from consumer_stock.csv)."""
    rows = await _csv("consumer_stock.csv")
    out = []
    for r in rows:
        oh, com = _num(r.get("OnHand")), _num(r.get("Committed"))
        out.append({
            "ItemCode": r.get("ItemCode", ""),
            "ItemName": r.get("ItemName", ""),
            "OnHand": oh,
            "Committed": com,
            "Available": _num(r.get("Available")) if r.get("Available") not in (None, "") else round(oh - com, 3),
            "OnOrder": _num(r.get("OnOrder")),
            "Uom": r.get("Uom", "") or r.get("InvntryUom", ""),
        })
    return out


async def lookup(q: str, limit: int = 12) -> list[dict]:
    """Search Consumer Store items by code or name — for the Purchase Request picker,
    so a requester can see what the consumable store already holds before raising a PR."""
    q = (q or "").strip().lower()
    if not q:
        return []
    out = []
    for r in await stock():
        if q in str(r["ItemCode"]).lower() or q in str(r["ItemName"]).lower():
            out.append({"code": r["ItemCode"], "name": r["ItemName"],
                        "onhand": r["OnHand"], "available": r["Available"], "uom": r["Uom"]})
            if len(out) >= max(int(limit), 1):
                break
    return out


async def stock_map(codes) -> dict:
    """{item_code: {onhand, available, uom, name}} for the given SAP item codes — used to
    show live store stock beside each linked purchase request."""
    want = {str(c).strip() for c in codes if c}
    if not want:
        return {}
    m = {}
    for r in await stock():
        c = str(r["ItemCode"]).strip()
        if c in want:
            m[c] = {"onhand": r["OnHand"], "available": r["Available"],
                    "uom": r["Uom"], "name": r["ItemName"]}
    return m


async def movements(limit: int = 200) -> list[dict]:
    """Recent stock movements for the Consumer Store warehouse (from consumer_movements.csv)."""
    rows = await _csv("consumer_movements.csv")
    out = []
    for r in rows[: max(int(limit), 1)]:
        tname = (r.get("TransName") or "").strip()
        if not tname:
            try:
                tname = TRANS_TYPES.get(int(float(r.get("TransType"))), str(r.get("TransType") or ""))
            except (TypeError, ValueError):
                tname = str(r.get("TransType") or "")
        out.append({
            "DocDate": r.get("DocDate", ""),
            "ItemCode": r.get("ItemCode", ""),
            "ItemName": r.get("ItemName", ""),
            "TransName": tname,
            "DocNum": r.get("DocNum", "") or r.get("BASE_REF", ""),
            "CardName": r.get("CardName", ""),
            "InQty": _num(r.get("InQty")),
            "OutQty": _num(r.get("OutQty")),
        })
    return out
