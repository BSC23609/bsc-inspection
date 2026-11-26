"""Inspection Coverage — SAP side of the reconciliation.

The QMS app NEVER talks to SAP directly. Exactly like the Consumer Store and the
stock MIS, a PowerShell job on the SAP box (C:\\scripts\\insp_extract.ps1) reads SAP
Business One (10.10.250.11 / BSC_FINAL_2707) and pushes three CSVs to the
bsc-stock-data GitHub repo — insp_inward.csv, insp_production.csv, insp_dispatch.csv.
This module just fetches those CSVs (raw.githubusercontent.com) and provides the
coil-number / Sales-Order matching helpers used to reconcile them against the QMS
inspection tables. Nothing here is written back to SAP.

Feeds (columns):
  insp_inward.csv      GRPONo, GRPODate, CoilNo, SapBatch, ItemCode, ItemGroup, Supplier, Qty
  insp_production.csv  ProdDate, MachCode, Process(CTL|Shearing), CoilNo, InItem, Grade, Qty
  insp_dispatch.csv    Customer, DispatchDate, DocType, DispatchDoc, SO_No, NumAtCard
"""
import csv
import io
import re
import time

import httpx

from .config import CONSUMER_DATA_BASE

_BASE = (CONSUMER_DATA_BASE or "").rstrip("/") + "/"
_cache: dict[str, tuple[float, list[dict]]] = {}
_TTL = 120  # seconds; the SAP job refreshes well apart from this


def is_configured() -> bool:
    return bool(CONSUMER_DATA_BASE)


async def _csv(name: str) -> list[dict]:
    if not _BASE.strip("/"):
        raise RuntimeError("CONSUMER_DATA_BASE is not configured")
    url = _BASE + name
    now = time.time()
    hit = _cache.get(url)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        r = await c.get(url, headers={"Cache-Control": "no-cache"})
    if r.status_code == 404:
        _cache[url] = (now, [])
        return []
    if r.status_code >= 300:
        raise RuntimeError(f"{name} not reachable ({r.status_code})")
    # utf-8-sig strips the BOM PowerShell Export-Csv writes, else the FIRST column's
    # header comes back as '﻿ProdDate' and that column reads as empty.
    text = r.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    _cache[url] = (now, rows)
    return rows


async def inward() -> list[dict]:
    return await _csv("insp_inward.csv")


async def production() -> list[dict]:
    return await _csv("insp_production.csv")


async def dispatch() -> list[dict]:
    return await _csv("insp_dispatch.csv")


# ----- matching helpers -------------------------------------------------------

def norm(s) -> str:
    """Uppercase, keep only A-Z/0-9 — so '2619391 / NF076620' -> '2619391NF076620'."""
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def coil_tokens(coil_no) -> set[str]:
    """Candidate match keys for one SAP coil string.

    A coil number may be 'C205821102' (SAIL), '2619391 / NF076620' (JSW),
    '26634477/F2045462-A-A' (dispatched piece) or 'D206052106' (NMDC). The QMS
    inspection may store either the whole thing or just the mill part (NF076620).
    So we emit: the normalised whole, and each slash-separated part, and each
    part with a trailing piece-suffix (…-A-A / …-A) stripped.
    """
    raw = str(coil_no or "")
    toks: set[str] = set()
    whole = norm(raw)
    if len(whole) >= 4:
        toks.add(whole)
    for part in re.split(r"[/]", raw):
        part = part.strip()
        if not part:
            continue
        n = norm(part)
        if len(n) >= 4:
            toks.add(n)
        # strip a trailing piece suffix like '-A' or '-A-B' before normalising
        base = re.sub(r"(-[A-Za-z0-9]{1,2})+$", "", part).strip()
        nb = norm(base)
        if len(nb) >= 4:
            toks.add(nb)
    return toks


def batch_key(batch) -> str:
    """One normalised key for a QMS inspection's batch/coil number."""
    return norm(batch)


def so_key(so) -> str:
    """Normalise a Sales Order reference to digits (PDI 'SO 400577' -> '400577')."""
    d = re.sub(r"\D", "", str(so or ""))
    return d.lstrip("0") or d


# a plate/sheet carries a LENGTH: thickness x width x length (3 numbers). A coil is
# only thickness x width (2 numbers). So anything with a 3rd X-dimension is NOT a coil.
_PLATE_DIM = re.compile(r"\d+(?:\.\d+)?\s*[xX]\s*\d+(?:\.\d+)?\s*[xX]\s*\d+", re.I)


def is_coil_desc(desc) -> bool:
    """True for a mother coil (T x W); False for a cut plate/sheet (T x W x L)."""
    return not _PLATE_DIM.search(str(desc or ""))
