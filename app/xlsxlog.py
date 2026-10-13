"""Helpers for reading the existing OneDrive Excel inspection logs and merging
their rows (read-only history) with the new Neon records in the list views."""


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
    """Pull a YYYY-MM-DD out of an ISO timestamp or date string."""
    s = str(v or "")
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # dd-mmm-yyyy or similar — leave as-is
    return s
