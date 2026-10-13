"""Monthly Diesel Generator Log PDF (landscape, paper-sheet style) via ReportLab."""
import os
import io
from datetime import datetime
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                Spacer, Image)

BRAND = colors.HexColor("#b45309")
DARK = colors.HexColor("#7c3f08")
HEAD = colors.HexColor("#fdebd2")
ALT = colors.HexColor("#fff9f1")
GRID = colors.HexColor("#c9ccd1")
MUT = colors.HexColor("#6b7280")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO = os.path.join(_BASE, "static", "bsc-logo.png")


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def dg_minutes(start, stop):
    if not start or not stop:
        return None
    a, b = str(start).split(":"), str(stop).split(":")
    if len(a) < 2 or len(b) < 2:
        return None
    try:
        m1 = int(a[0]) * 60 + int(a[1])
        m2 = int(b[0]) * 60 + int(b[1])
    except ValueError:
        return None
    d = m2 - m1
    if d < 0:
        d += 1440
    return d


def fmt_min(mins):
    if mins is None:
        return ""
    mins = round(mins)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _fmt(v):
    if v is None or v == "":
        return ""
    return str(v)


def build_dg_pdf(rows, dg_set: str, month: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=18, rightMargin=18, topMargin=16, bottomMargin=20)
    styles = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, textColor=BRAND, alignment=1),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUT, alignment=1),
        "right": ParagraphStyle("r", fontName="Helvetica-Bold", fontSize=9.5, textColor=DARK, alignment=2),
        "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=7.8, leading=9),
        "hd": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=7.8, textColor=DARK, alignment=1, leading=9),
    }
    story = []

    # ---- header band: logo + centered title + right meta ----
    title_block = [Paragraph("DIESEL GENERATOR LOG", styles["title"]),
                   Paragraph("Bharat Steel (Chennai) Pvt. Ltd.", styles["sub"])]
    logo_cell = ""
    if os.path.exists(_LOGO):
        try:
            logo_cell = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo_cell = ""
    meta = Paragraph(f"{dg_set}<br/>{month}", styles["right"])
    head = Table([[logo_cell, title_block, meta]], colWidths=[120, None, 120])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
    ]))
    story.append(head)
    story.append(Spacer(1, 8))

    cols = ["Date", "Start Time", "End Time", "Run Hrs", "Fuel (L)", "Diesel (L)",
            "Start Load (kWh)", "End Load (kWh)", "Units (kWh)", "Remarks", "Sign"]
    widths = [66, 52, 52, 50, 46, 52, 62, 62, 50, 240, 60]

    data = [[Paragraph(c, styles["hd"]) for c in cols]]
    tf = tu = tm = 0.0
    for r in rows:
        mins = r.get("running_minutes")
        mins = int(mins) if mins not in (None, "") else dg_minutes(r.get("start_at"), r.get("stop_at"))
        if mins is not None:
            tm += mins
        ks, ke = _num(r.get("kwh_start")), _num(r.get("kwh_end"))
        units = _num(r.get("units"))
        if units is None and ks is not None and ke is not None:
            units = ke - ks
        if units is not None:
            tu += units
        fu = _num(r.get("fuel_added"))
        if fu is not None:
            tf += fu
        data.append([
            Paragraph(_fmt(str(r.get("log_date"))[:10]), styles["cell"]),
            Paragraph(_fmt(r.get("start_at")), styles["cell"]),
            Paragraph(_fmt(r.get("stop_at")), styles["cell"]),
            Paragraph(fmt_min(mins), styles["cell"]),
            Paragraph(_fmt(r.get("fuel_added")), styles["cell"]),
            Paragraph(_fmt(r.get("diesel_level")), styles["cell"]),
            Paragraph(_fmt(r.get("kwh_start")), styles["cell"]),
            Paragraph(_fmt(r.get("kwh_end")), styles["cell"]),
            Paragraph(_fmt(round(units, 2) if units is not None else ""), styles["cell"]),
            Paragraph(_fmt(r.get("remarks")), styles["cell"]),
            Paragraph(_fmt(r.get("sign")), styles["cell"]),
        ])

    total_style = ParagraphStyle("tot", fontName="Helvetica-Bold", fontSize=8, textColor=DARK)
    data.append([
        Paragraph(f"TOTAL · {len(rows)} entr" + ("y" if len(rows) == 1 else "ies"), total_style),
        "", "", Paragraph(fmt_min(tm), total_style),
        Paragraph(str(round(tf, 2)), total_style), "", "", "",
        Paragraph(str(round(tu, 2)), total_style), "", "",
    ])

    t = Table(data, colWidths=widths, repeatRows=1)
    ts = [
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (8, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#fff7ed")),
        ("SPAN", (0, -1), (2, -1)),
    ]
    for i in range(1, len(rows) + 1):
        if i % 2 == 0:
            ts.append(("BACKGROUND", (0, i), (-1, i), ALT))
    t.setStyle(TableStyle(ts))
    story.append(t)
    story.append(Spacer(1, 6))
    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Paragraph(f"Generated {gen} · {len(rows)} entries",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5,
                                          textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()
