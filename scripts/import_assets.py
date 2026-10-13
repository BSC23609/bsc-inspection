#!/usr/bin/env python3
"""Import the equipment master-list Excel into the Neon `maint_assets` table.

Runs from GitHub Actions (or locally). Reads the .xlsx, auto-detects the header
row and columns (machine no -> code, description -> name, location, make, model,
capacity -> notes), de-duplicates codes, and upserts into Neon.

Usage:
    python scripts/import_assets.py data/MASTER_LIST_OF_EQUIPMENTS.xlsx
    python scripts/import_assets.py <file.xlsx> --mode update   # overwrite existing by code
    python scripts/import_assets.py <file.xlsx> --dry           # parse only, no DB write

Env:
    DATABASE_URL   Neon connection string (same one the app uses)
    IMPORT_MODE    'skip' (default) or 'update' — overridden by --mode
"""
import os
import re
import sys
import ssl
import asyncio
from urllib.parse import urlsplit, urlunsplit

import openpyxl

# ---------------- column mapping (same logic as the app importer) ----------------
FIELD_RX = [
    ("code",          re.compile(r"(machine|m/?c|asset|equip).*\bno\b|^no\.?$|\bcode\b|\btag\b", re.I)),
    ("name",          re.compile(r"desc|name|equipment|particular|\bitem\b", re.I)),
    ("location",      re.compile(r"location|shed|area|place|site", re.I)),
    ("make",          re.compile(r"make|manufacturer|brand|supplier", re.I)),
    ("model",         re.compile(r"model", re.I)),
    ("capacity",      re.compile(r"capacit|rating|\bsize\b", re.I)),
    ("serial_no",     re.compile(r"serial", re.I)),
    ("running_hours", re.compile(r"hour|hrs|meter|\brun\b", re.I)),
]
TYPE_MAP = [("CTL", "CTL Line"), ("EOT", "EOT Crane"), ("GANDRY", "Gantry Crane"),
            ("GANTRY", "Gantry Crane"), ("TROLLEY", "Transfer Trolley"), ("COMPRESSOR", "Compressor"),
            ("LEVELLER", "Leveller"), ("LEVELER", "Leveller"), ("IRON WORKER", "Iron Worker"),
            ("SHEAR", "Shearing"), ("CRANE", "Crane"), ("WELD", "Welding"), ("PUMP", "Pump"),
            ("GEN-SET", "DG Set"), ("GENSET", "DG Set")]


def _clean(x):
    return re.sub(r"\s+", " ", str(x)).strip() if x is not None else ""


def _norm_code(m):
    m = re.sub(r"\s+", " ", str(m or "")).strip()
    return re.sub(r"\s*-\s*", "-", m)


def _guess_type(name):
    u = (name or "").upper()
    for k, v in TYPE_MAP:
        if k in u:
            return v
    return ""


def parse_assets(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    aoa = [[("" if c is None else str(c)) for c in row] for row in ws.iter_rows(values_only=True)]
    # detect header row = row with most distinct field matches (first 15 rows)
    best_row, best_n, best_map = -1, 0, {}
    for r in range(min(len(aoa), 15)):
        row, used, cmap, n = aoa[r], set(), {}, 0
        for field, rx in FIELD_RX:
            for c in range(len(row)):
                if c in used or not row[c].strip():
                    continue
                if rx.search(row[c]):
                    cmap[field] = c
                    used.add(c)
                    n += 1
                    break
        if n > best_n:
            best_row, best_n, best_map = r, n, cmap
    if best_row < 0 or ("name" not in best_map and "code" not in best_map):
        raise SystemExit("Could not detect a header row (need Machine No. / Description / Location).")

    m, seen, out = best_map, {}, []
    for r in range(best_row + 1, len(aoa)):
        row = aoa[r]
        def cell(f):
            return _clean(row[m[f]]) if f in m and m[f] < len(row) else ""
        name = cell("name")
        if not name:
            continue
        code = _norm_code(cell("code")) or f"ASSET-{len(out) + 1}"
        base, k = code, seen.get(code.lower(), 0)
        if k:
            code = f"{base}-{k + 1}"
        seen[base.lower()] = k + 1
        cap = cell("capacity")
        out.append({
            "code": code, "name": name, "atype": _guess_type(name),
            "location": cell("location"), "make": cell("make"), "model": cell("model"),
            "serial_no": cell("serial_no"),
            "running_hours": cell("running_hours") or 0,
            "notes": ("Capacity: " + cap) if cap else "",
        })
    return out


# ---------------- Neon (asyncpg, SSL no-verify like the app) ----------------
def _dsn_ssl(url):
    parts = urlsplit(url)
    dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return dsn, ctx


DDL = """CREATE TABLE IF NOT EXISTS maint_assets (
    id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, atype TEXT, location TEXT,
    make TEXT, model TEXT, serial_no TEXT, running_hours NUMERIC DEFAULT 0,
    hours_updated_at TIMESTAMPTZ, active BOOLEAN DEFAULT true, notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now())"""


def _hours(v):
    try:
        f = float(v)
        return f
    except Exception:
        return 0


async def load_to_neon(assets, mode):
    import asyncpg
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    dsn, ctx = _dsn_ssl(url)
    con = await asyncpg.connect(dsn, ssl=ctx)
    added = updated = skipped = 0
    try:
        await con.execute(DDL)
        for a in assets:
            vals = (a["code"], a["name"], a["atype"], a["location"], a["make"],
                    a["model"], a["serial_no"], _hours(a["running_hours"]), a["notes"])
            if mode == "update":
                await con.execute("""
                    INSERT INTO maint_assets (code,name,atype,location,make,model,serial_no,running_hours,notes,hours_updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now())
                    ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name, atype=EXCLUDED.atype,
                      location=EXCLUDED.location, make=EXCLUDED.make, model=EXCLUDED.model,
                      serial_no=EXCLUDED.serial_no, running_hours=EXCLUDED.running_hours, notes=EXCLUDED.notes""", *vals)
                updated += 1  # inserted-or-updated
            else:
                tag = await con.execute("""
                    INSERT INTO maint_assets (code,name,atype,location,make,model,serial_no,running_hours,notes,hours_updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,now())
                    ON CONFLICT (code) DO NOTHING""", *vals)
                if tag.endswith("1"):
                    added += 1
                else:
                    skipped += 1
    finally:
        await con.close()
    return added, updated, skipped


def main():
    args = [a for a in sys.argv[1:]]
    dry = "--dry" in args
    mode = os.environ.get("IMPORT_MODE", "skip")
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]
    paths = [a for a in args if not a.startswith("--") and a != mode]
    path = paths[0] if paths else "data/MASTER_LIST_OF_EQUIPMENTS.xlsx"
    if not os.path.exists(path):
        raise SystemExit(f"File not found: {path}")

    assets = parse_assets(path)
    print(f"Parsed {len(assets)} assets from {path} (mode={mode})")
    for a in assets:
        print(f"  {a['code']:<11} | {a['name'][:36]:<36} | {a['atype']:<15} | {a['location']:<14} | {a['make'][:16]}")
    if dry:
        print("\n--dry: no database changes made.")
        return
    added, updated, skipped = asyncio.run(load_to_neon(assets, mode))
    print(f"\nDone. mode={mode}  added={added}  upserted={updated}  skipped={skipped}")


if __name__ == "__main__":
    main()
