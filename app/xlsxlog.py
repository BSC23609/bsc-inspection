"""Helpers for reading the existing OneDrive Excel inspection logs and merging
their rows (read-only history) with the new Neon records in the list views."""
from datetime import datetime, timedelta

# Excel's 1900 date system epoch (1899-12-30 absorbs the 1900 leap-year bug).
_EXCEL_EPOCH = datetime(1899, 12, 30)


def excel_serial_to_date(n) -> str:
    try:
        return (_EXCEL_EPOCH + timedelta(days=float(n))).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return str(n)


def header_map(cols):
    """Lower-cased header -> column index."""
    m = {}
    for i, c in enumerate(cols or []):
        key = str(c or "").strip().lower()
        if key and key not in m:
            m[key] = i
    return m


def cell(row, hmap, *names):
    """First non-empty cell whose header matches one of `names` (case-insensitive)."""
    for n in names:
        i = hmap.get(str(n).strip().lower())
        if i is not None and i < len(row):
            v = row[i]
            if v is not None and str(v) != "":
                return str(v)
    return ""


def cell_at(row, idx):
    if idx is not None and 0 <= idx < len(row):
        v = row[idx]
        return "" if v is None else str(v)
    return ""


def date_part(v):
    """Return YYYY-MM-DD from an ISO string, or from an Excel serial-date number."""
    s = str(v or "").strip()
    if not s:
        return ""
    # Date cells read from the workbook come back as Excel serial numbers.
    try:
        f = float(s)
        if 20000 <= f <= 90000:  # plausible modern date range (~1954–2146)
            return excel_serial_to_date(f)
    except ValueError:
        pass
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # dd-mmm-yyyy or similar — leave as-is
    return s
