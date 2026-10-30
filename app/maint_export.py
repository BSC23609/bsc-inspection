"""Build the Maintenance Log / Machine History Card workbook from the app's own
recorded data (maint_logs + maint_breakdowns), matching the Bharat Steel layout:
LOG (master log), DASHBOARD (stats), one Machine History Card per machine, plus
Machines and Lists. Returns .xlsx bytes. Pure openpyxl, values computed in Python
(no Excel formulas) so the saved file is correct the moment it opens."""
import io
import re
from datetime import datetime, date
from collections import defaultdict

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ---- Bharat Steel palette ----
NAVY = "1F3B5B"      # header fill
NAVY2 = "2E4A6B"
YELLOW = "FFF2CC"    # breakdown / highlight
GREY = "EDEDED"
GREEN = "E2EFDA"
LINE = "BFBFBF"

WHITE_BOLD = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
HEAD_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="Calibri", size=13, bold=True, color="1F3B5B")
LBL_FONT = Font(name="Calibri", size=9, bold=True, color="404040")
VAL_FONT = Font(name="Calibri", size=10)
BODY_FONT = Font(name="Calibri", size=9)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
_thin = Side(style="thin", color=LINE)
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)


def _fill(hexc):
    return PatternFill("solid", fgColor=hexc)


def _hrs(start, end):
    """Hours between two datetimes, rounded to 2 dp; None if either missing."""
    if not start or not end:
        return None
    try:
        return round((end - start).total_seconds() / 3600.0, 2)
    except Exception:
        return None


def _d(v):
    if isinstance(v, datetime):
        return v
    return None


def _datepart(v):
    v = _d(v)
    return v.date() if v else None


def _timepart(v):
    v = _d(v)
    return v.strftime("%H:%M") if v else ""


def _month(v):
    v = _datepart(v)
    return v.strftime("%b-%y") if v else ""


def _names(ids, emp_by):
    out = []
    for i in (ids or []):
        e = emp_by.get(i)
        if e:
            out.append(e)
    return ", ".join(out)


def _sheet_name(code, name):
    base = f"{(code or '').replace('/', '-')} {(name or '')}".strip()
    base = re.sub(r"[\\/?*\[\]:]", "-", base)
    return base[:31] or "SHEET"


# ------------------------------------------------------------------ events
def _events(logs, breakdowns, assets, employees):
    """Normalise PM completions + breakdowns into one list of log dicts."""
    a_by = {a["id"]: a for a in assets}
    emp_by = {e["id"]: (e.get("name") or e.get("emp_no") or f"#{e['id']}") for e in employees}
    ev = []

    for b in breakdowns:
        a = a_by.get(b.get("asset_id")) or {}
        raised = _d(b.get("raised_at"))
        closed = _d(b.get("closed_at"))
        ag = b.get("assignees")
        if isinstance(ag, str):
            try:
                import json as _j
                ag = _j.loads(ag)
            except Exception:
                ag = []
        who = _names(ag, emp_by) or (emp_by.get(b.get("assignee_emp_id")) or b.get("reported_by") or "")
        ev.append({
            "dt": raised, "code": a.get("code", ""), "name": a.get("name", ""),
            "asset_id": b.get("asset_id"),
            "desc": (b.get("problem") or "").strip() or "Breakdown",
            "start": _timepart(raised), "end": _timepart(closed),
            "hrs": _hrs(raised, closed),
            "category": "Breakdown Maintenance",
            "by": who, "action": (b.get("resolution") or b.get("remarks") or "").strip(),
            "status": "Completed" if (b.get("status") == "closed" or closed) else "Pending",
            "is_breakdown": True,
        })

    # Preventive completions: ONE row per machine per day. A daily checklist can be
    # submitted several times a day (each submit writes a log row); the log should show
    # a single consolidated entry per machine per day — keep the latest submission.
    pm_latest = {}
    for l in logs:
        done = _d(l.get("done_at"))
        if not done:
            continue
        a = a_by.get(l.get("asset_id")) or {}
        st = (l.get("status") or "").strip()
        key = (l.get("asset_id"), done.date())
        sa = _d(l.get("started_at"))
        ea = _d(l.get("ended_at"))
        rec = {
            "dt": done, "code": a.get("code", ""), "name": a.get("name", ""),
            "asset_id": l.get("asset_id"),
            "desc": "Preventive maintenance checklist",
            "start": _timepart(sa), "end": _timepart(ea), "hrs": _hrs(sa, ea),
            "category": "Scheduled Preventive Maintenance",
            "by": emp_by.get(l.get("done_by_emp_id")) or (l.get("done_by") or ""),
            "action": (l.get("remarks") or "").strip() or "Checklist completed",
            "status": "Completed" if st in ("", "done", "completed", "ok") else st.title(),
            "is_breakdown": False,
        }
        prev = pm_latest.get(key)
        if prev is None or done >= prev["dt"]:
            pm_latest[key] = rec
    ev.extend(pm_latest.values())

    ev = [e for e in ev if e["dt"]]
    ev.sort(key=lambda e: e["dt"])
    return ev


# ------------------------------------------------------------------ writers
def _band(ws, row, col1, col2, text, fill=NAVY, font=HEAD_FONT):
    ws.merge_cells(start_row=row, start_column=col1, end_row=row, end_column=col2)
    c = ws.cell(row, col1, text)
    c.fill = _fill(fill); c.font = font; c.alignment = CENTER


def _log_sheet(ws, events):
    cols = ["SI.NO", "DATE", "MAC. NO.", "MACHINE NAME", "DESCRIPTION OF PROBLEM / WORK",
            "START TIME", "END TIME", "TOTAL HRS", "CATEGORY", "ATTENDED BY",
            "ACTION TAKEN", "STATUS", "MONTH"]
    widths = [7, 12, 12, 26, 38, 10, 10, 9, 24, 18, 34, 12, 9]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    _band(ws, 1, 1, len(cols), "MASTER MAINTENANCE LOG — Bharat Steel (Chennai) Pvt. Ltd.", NAVY, Font(size=12, bold=True, color="FFFFFF"))
    ws.cell(2, 1, "Auto-generated from the QMS maintenance module. One row per completed job / breakdown.").font = Font(size=8, italic=True, color="808080")
    hr = 4
    for i, c in enumerate(cols, 1):
        cell = ws.cell(hr, i, c)
        cell.fill = _fill(NAVY); cell.font = HEAD_FONT; cell.alignment = CENTER; cell.border = BORDER
    r = hr + 1
    for n, e in enumerate(events, 1):
        vals = [n, _datepart(e["dt"]), e["code"], e["name"], e["desc"], e["start"], e["end"],
                e["hrs"] if e["hrs"] is not None else "", e["category"], e["by"], e["action"],
                e["status"], _month(e["dt"])]
        for i, v in enumerate(vals, 1):
            cell = ws.cell(r, i, v)
            cell.font = BODY_FONT; cell.border = BORDER
            cell.alignment = CENTER if i in (1, 2, 3, 6, 7, 8, 12, 13) else LEFT
            if i == 2 and isinstance(v, date):
                cell.number_format = "dd-mmm-yy"
        if e["is_breakdown"]:
            ws.cell(r, 9).fill = _fill(YELLOW)
        r += 1
    ws.freeze_panes = f"A{hr+1}"
    ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(cols))}{max(hr, r-1)}"


def _stat_grid(ws, row, pairs, col1=1, per=4):
    """Draw label/value stat tiles. pairs = [(label, value), ...]."""
    c = col1
    for label, value in pairs:
        ws.merge_cells(start_row=row, start_column=c, end_row=row, end_column=c + 1)
        lc = ws.cell(row, c, label); lc.fill = _fill(GREY); lc.font = LBL_FONT; lc.alignment = CENTER; lc.border = BORDER
        ws.merge_cells(start_row=row + 1, start_column=c, end_row=row + 1, end_column=c + 1)
        vc = ws.cell(row + 1, c, value); vc.font = Font(size=12, bold=True, color="1F3B5B"); vc.alignment = CENTER; vc.border = BORDER
        c += 2
        if c > col1 + per * 2 - 1:
            c = col1; row += 2


def _summary(events):
    tot = len(events)
    bd = [e for e in events if e["is_breakdown"]]
    pm = [e for e in events if not e["is_breakdown"]]
    bd_hrs = round(sum(e["hrs"] or 0 for e in bd), 2)
    pm_hrs = round(sum(e["hrs"] or 0 for e in pm), 2)
    down = round(sum(e["hrs"] or 0 for e in events), 2)
    by_machine = defaultdict(float)
    for e in events:
        by_machine[e["code"] or e["name"]] += (e["hrs"] or 0)
    worst = max(by_machine, key=by_machine.get) if by_machine else "—"
    return {"jobs": tot, "breakdowns": len(bd), "bd_hrs": bd_hrs, "pm_jobs": len(pm),
            "pm_hrs": pm_hrs, "down": down, "worst": worst}


def _dashboard(ws, events):
    ws.column_dimensions["A"].width = 26
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 16
    _band(ws, 1, 1, 8, "MAINTENANCE DASHBOARD — Bharat Steel (Chennai) Pvt. Ltd.", NAVY, Font(size=12, bold=True, color="FFFFFF"))
    ws.cell(2, 1, "All figures are computed live from the maintenance log.").font = Font(size=8, italic=True, color="808080")
    s = _summary(events)
    _stat_grid(ws, 4, [
        ("TOTAL JOBS LOGGED", s["jobs"]), ("TOTAL BREAKDOWNS", s["breakdowns"]),
        ("TOTAL B/D HOURS", s["bd_hrs"]), ("PM JOBS", s["pm_jobs"]),
    ])
    _stat_grid(ws, 7, [
        ("PM HOURS", s["pm_hrs"]), ("TOTAL DOWNTIME HRS", s["down"]),
        ("WORST MACHINE", s["worst"]), ("", ""),
    ])
    _band(ws, 10, 1, 4, "SECTION 1 — BREAKDOWNS BY MONTH", NAVY2)
    hdr = ["MONTH", "NO. OF BREAKDOWNS", "TOTAL B/D HRS", "AVG HRS PER BREAKDOWN"]
    for i, h in enumerate(hdr, 1):
        cell = ws.cell(11, i, h); cell.fill = _fill(GREY); cell.font = LBL_FONT; cell.alignment = CENTER; cell.border = BORDER
    months = defaultdict(lambda: [0, 0.0])
    for e in events:
        if e["is_breakdown"]:
            m = _month(e["dt"])
            months[m][0] += 1
            months[m][1] += (e["hrs"] or 0)
    r = 12
    for m in sorted(months, key=lambda x: datetime.strptime(x, "%b-%y") if x else datetime.min):
        n, h = months[m]
        row = [m, n, round(h, 2), round(h / n, 2) if n else 0]
        for i, v in enumerate(row, 1):
            cell = ws.cell(r, i, v); cell.font = BODY_FONT; cell.alignment = CENTER; cell.border = BORDER
        r += 1
    if not months:
        ws.cell(12, 1, "No breakdowns logged yet.").font = Font(size=9, italic=True, color="808080")


def _card(ws, asset, events):
    code = asset.get("code", ""); name = asset.get("name", "")
    for i, w in enumerate([7, 13, 26, 10, 10, 10, 30, 12, 24], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    _band(ws, 1, 1, 9, "MACHINE HISTORY CARD", NAVY, Font(size=12, bold=True, color="FFFFFF"))
    # header block
    ws.cell(3, 1, "MACHINE NAME").font = LBL_FONT
    ws.merge_cells("B3:D3"); ws.cell(3, 2, name).font = VAL_FONT
    ws.cell(3, 5, "MACHINE NUMBER").font = LBL_FONT
    ws.cell(3, 6, code).font = VAL_FONT
    ws.cell(3, 7, "CAPACITY").font = LBL_FONT
    ws.cell(3, 8, asset.get("notes", "") or "").font = VAL_FONT
    ws.cell(4, 1, "LOCATION").font = LBL_FONT
    ws.merge_cells("B4:D4"); ws.cell(4, 2, asset.get("location", "") or "").font = VAL_FONT
    ws.cell(4, 5, "MAKE").font = LBL_FONT
    ws.cell(4, 6, asset.get("make", "") or "").font = VAL_FONT
    # summary
    mine = [e for e in events if e.get("asset_id") == asset["id"]]
    s = _summary(mine)
    _band(ws, 6, 1, 9, "BREAKDOWN & MAINTENANCE SUMMARY", NAVY2)
    _stat_grid(ws, 7, [("TOTAL BREAKDOWNS", s["breakdowns"]), ("TOTAL B/D HOURS", s["bd_hrs"]),
                       ("PM JOBS", s["pm_jobs"]), ("PM HOURS", s["pm_hrs"])])
    last = max((e["dt"] for e in mine), default=None)
    _stat_grid(ws, 10, [("TOTAL DOWNTIME HRS", s["down"]),
                        ("LAST JOB LOGGED", last.strftime("%d-%b-%y") if last else "—"),
                        ("", ""), ("", "")])
    # record log
    _band(ws, 13, 1, 9, "RECORD LOG FOR THIS MACHINE", NAVY)
    cols = ["SI.NO", "DATE", "DESCRIPTION OF PROBLEM / WORK", "FROM", "TO", "TOTAL HRS",
            "CORRECTIVE ACTION TAKEN", "STATUS", "CATEGORY"]
    for i, c in enumerate(cols, 1):
        cell = ws.cell(14, i, c); cell.fill = _fill(GREY); cell.font = LBL_FONT; cell.alignment = CENTER; cell.border = BORDER
    r = 15
    for n, e in enumerate(mine, 1):
        row = [n, _datepart(e["dt"]), e["desc"], e["start"], e["end"],
               e["hrs"] if e["hrs"] is not None else "", e["action"], e["status"], e["category"]]
        for i, v in enumerate(row, 1):
            cell = ws.cell(r, i, v); cell.font = BODY_FONT; cell.border = BORDER
            cell.alignment = CENTER if i in (1, 2, 4, 5, 6, 8) else LEFT
            if i == 2 and isinstance(v, date):
                cell.number_format = "dd-mmm-yy"
        if e["is_breakdown"]:
            ws.cell(r, 9).fill = _fill(YELLOW)
        r += 1
    if not mine:
        ws.cell(15, 1, "No jobs logged for this machine yet.").font = Font(size=9, italic=True, color="808080")
    ws.freeze_panes = "A15"


def _machines_sheet(ws, assets, events):
    ws.column_dimensions["A"].width = 12
    ws.column_dimensions["B"].width = 34
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["D"].width = 12
    ws.column_dimensions["E"].width = 10
    _band(ws, 1, 1, 5, "MACHINE MASTER", NAVY)
    hdr = ["MAC. NO.", "MACHINE NAME", "HISTORY CARD SHEET", "IN SERVICE?", "RECORDS"]
    for i, h in enumerate(hdr, 1):
        cell = ws.cell(2, i, h); cell.fill = _fill(GREY); cell.font = LBL_FONT; cell.alignment = CENTER; cell.border = BORDER
    counts = defaultdict(int)
    for e in events:
        counts[e.get("asset_id")] += 1
    r = 3
    for a in assets:
        row = [a.get("code", ""), a.get("name", ""), _sheet_name(a.get("code"), a.get("name")),
               "Yes" if a.get("active", True) else "No", counts.get(a["id"], 0)]
        for i, v in enumerate(row, 1):
            cell = ws.cell(r, i, v); cell.font = BODY_FONT; cell.border = BORDER
            cell.alignment = LEFT if i == 2 else CENTER
        r += 1


def _readme(ws):
    ws.column_dimensions["A"].width = 110
    _band(ws, 1, 1, 1, "MAINTENANCE LOG / MACHINE HISTORY CARD", NAVY, Font(size=12, bold=True, color="FFFFFF"))
    lines = [
        "",
        "This workbook is generated automatically every day by the Bharat Steel QMS maintenance module",
        "(qms.bharatsteels.in) and saved to OneDrive. Do not type into it by hand — your edits will be",
        "overwritten on the next daily refresh.",
        "",
        "LOG        — every completed maintenance job and breakdown, newest data appended each day.",
        "DASHBOARD  — live totals and a breakdown-by-month table.",
        "Per machine— one 'Machine History Card' sheet per machine, with its own summary and record log.",
        "Machines   — the machine master with a record count for each.",
        "",
        "Source of truth: the maintenance database. To add a record, complete the job in the app",
        "(Maintenance Board → Complete, or Raise Breakdown) — it will appear here on the next refresh.",
    ]
    for i, t in enumerate(lines, 3):
        ws.cell(i, 1, t).font = Font(size=10) if t else BODY_FONT


# ------------------------------------------------------------------ main
def build(logs, breakdowns, assets, employees) -> bytes:
    assets = [dict(a) for a in assets]
    logs = [dict(l) for l in logs]
    breakdowns = [dict(b) for b in breakdowns]
    employees = [dict(e) for e in employees]
    events = _events(logs, breakdowns, assets, employees)

    wb = openpyxl.Workbook()
    _readme(wb.active); wb.active.title = "README"
    _dashboard(wb.create_sheet("DASHBOARD"), events)
    _log_sheet(wb.create_sheet("LOG"), events)
    used = set()
    for a in sorted(assets, key=lambda x: str(x.get("code") or "")):
        nm = _sheet_name(a.get("code"), a.get("name"))
        base = nm; k = 2
        while nm in used:
            nm = f"{base[:28]}-{k}"; k += 1
        used.add(nm)
        _card(wb.create_sheet(nm), a, events)
    _machines_sheet(wb.create_sheet("Machines"), assets, events)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
