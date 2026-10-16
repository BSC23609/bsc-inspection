"""OneDrive write-through for every QMS module.

WHY THIS EXISTS: the first FastAPI build did its OneDrive work (PDF upload, Excel
row, CSV register) in `asyncio.create_task(...)` *after* returning the response.
On Vercel the function is frozen the moment the response is sent, so those tasks
were cut off and nothing reached OneDrive. Every submit now AWAITS its OneDrive
work through `Steps`, records the outcome in Neon (`onedrive_sync`) and returns
it to the page, so a failure is visible and can be re-run from /onedrive-sync.
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db

IST = ZoneInfo("Asia/Kolkata")
STEP_TIMEOUT = 45          # seconds per OneDrive step
_ensured = False


async def ensure():
    global _ensured
    if _ensured:
        return
    await db.execute("""
        CREATE TABLE IF NOT EXISTS onedrive_sync (
            module TEXT NOT NULL, ref TEXT NOT NULL, ok BOOLEAN DEFAULT false,
            saved TEXT, errors TEXT, attempts INTEGER DEFAULT 0,
            first_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (module, ref))
    """)
    _ensured = True


class Steps:
    """Runs each OneDrive step, collecting what was saved and what failed —
    one failing step (e.g. Excel locked) never stops the others (e.g. the PDF)."""

    def __init__(self, module: str, ref):
        self.module, self.ref = module, str(ref)
        self.saved, self.skipped, self.errors = [], [], []

    async def run(self, label: str, coro, timeout: int = STEP_TIMEOUT):
        try:
            r = await asyncio.wait_for(coro, timeout)
            if r == "exists":
                self.skipped.append(f"{label} (already there)")
            elif r == "none":
                self.skipped.append(f"{label} (nothing to write)")
            else:
                self.saved.append(label)
            return r
        except asyncio.TimeoutError:
            self.errors.append(f"{label}: timed out after {timeout}s")
        except Exception as e:
            self.errors.append(f"{label}: {e}")
        print(f"[onedrive] {self.module} {self.ref} — {self.errors[-1]}")
        return None

    def skip(self, label: str):
        self.skipped.append(label)

    def fail(self, label: str, msg: str):
        self.errors.append(f"{label}: {msg}")
        print(f"[onedrive] {self.module} {self.ref} — {label}: {msg}")

    def result(self) -> dict:
        return {"ok": not self.errors, "saved": self.saved,
                "skipped": self.skipped, "errors": self.errors}


async def record(steps: Steps) -> dict:
    """Persist the outcome so failed/missed records show up on /onedrive-sync."""
    res = steps.result()
    try:
        await ensure()
        await db.execute(
            "INSERT INTO onedrive_sync (module,ref,ok,saved,errors,attempts,updated_at) "
            "VALUES ($1,$2,$3,$4,$5,1,now()) ON CONFLICT (module,ref) DO UPDATE SET "
            "ok=EXCLUDED.ok, saved=EXCLUDED.saved, errors=EXCLUDED.errors, "
            "attempts=onedrive_sync.attempts+1, updated_at=now()",
            steps.module, steps.ref, res["ok"], "; ".join(steps.saved + steps.skipped),
            "; ".join(steps.errors))
    except Exception as e:
        print("[onedrive] couldn't record sync status:", e)
    return res


# ---------------- shared helpers ----------------
def legacy_safe(s) -> str:
    """Node: batch.replace(/[^a-zA-Z0-9\\-_]/g, '_')."""
    return re.sub(r"[^a-zA-Z0-9\-_]", "_", str(s or "")) or "NOBATCH"


def ist_date_str(when) -> str:
    """dd-mm-yyyy in IST (the Node file-name date)."""
    w = when or datetime.now(timezone.utc)
    if w.tzinfo is None:
        w = w.replace(tzinfo=timezone.utc)
    return w.astimezone(IST).strftime("%d-%m-%Y")


def legacy_file_name(batch, when, suffix: str) -> str:
    """Same name the Node app used: <batch>_(dd-mm-yyyy)_<suffix>."""
    return f"{legacy_safe(batch)}_({ist_date_str(when)})_{suffix}"


def iso_z(when=None) -> str:
    """2026-10-12T07:15:57.202Z — the Timestamp format already in the Excel logs."""
    w = when or datetime.now(timezone.utc)
    if w.tzinfo is None:
        w = w.replace(tzinfo=timezone.utc)
    return w.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def as_obj(v, default):
    """JSONB columns are sometimes stored double-encoded (a JSON string)."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return default
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                return default
    return v if v is not None else default


def csv_cell(v) -> str:
    v = "" if v is None else str(v)
    return '"' + v.replace('"', '""') + '"' if any(ch in v for ch in ',"\n') else v


def csv_line(values) -> str:
    return ",".join(csv_cell(x) for x in values) + "\n"


def norm(v) -> str:
    return str(v or "").strip().lower()
