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

BRAND = colors.HexColor("#0069aa")   # Bharat Steel logo blue
DARK = colors.HexColor("#005483")
HEAD = colors.HexColor("#e2f0fb")
ALT = colors.HexColor("#f4f9fe")
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
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eaf4fc")),
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


# ============================================================
#  Coil Inward Inspection — single-record report (portrait A4)
# ============================================================
def _yn(v):
    s = str(v or "").strip().lower()
    if s in ("y", "yes", "true", "1"):
        return "Yes"
    if s in ("n", "no", "false", "0"):
        return "No"
    return "-"


def build_coil_pdf(r: dict) -> bytes:
    """One Coil Inward record as a clean, brand-blue A4 report."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=34, rightMargin=34, topMargin=26, bottomMargin=30)
    st = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, textColor=BRAND, alignment=1, leading=18),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUT, alignment=1),
        "meta": ParagraphStyle("m", fontName="Helvetica-Bold", fontSize=9, textColor=DARK, alignment=2, leading=12),
        "sec": ParagraphStyle("se", fontName="Helvetica-Bold", fontSize=9.5, textColor="white", leading=12),
        "lab": ParagraphStyle("l", fontName="Helvetica", fontSize=8.5, textColor=MUT, leading=11),
        "val": ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.HexColor("#0e1524"), leading=12),
    }
    story = []

    logo = ""
    if os.path.exists(_LOGO):
        try:
            logo = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo = ""
    wheels = str(r.get("wheels_india") or "").strip().lower() in ("yes", "y", "true", "1")
    rep_title = "MOTHER COIL INSPECTION REPORT" if wheels else "COIL INWARD INSPECTION"
    sub = "Wheels India" if wheels else "Mother Coil Inspection Report"
    title_block = [Paragraph(rep_title, st["title"]), Paragraph(sub, st["sub"])]
    rid = r.get("id")
    meta = Paragraph(f"Report #{rid}<br/>{str(r.get('log_date') or '')[:10]}", st["meta"])
    head = Table([[logo, title_block, meta]], colWidths=[130, None, 130])
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.1, BRAND),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 9),
    ]))
    story.append(head)
    story.append(Spacer(1, 12))

    def section(title, pairs):
        bar = Table([[Paragraph(title, st["sec"])]], colWidths=[doc.width])
        bar.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), BRAND),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(bar)
        data, row = [], []
        for i, (lab, val) in enumerate(pairs):
            cell = [Paragraph(lab, st["lab"]), Spacer(1, 1), Paragraph(_fmt(val) or "-", st["val"])]
            row.append(cell)
            if len(row) == 2:
                data.append(row); row = []
        if row:
            row.append("")
            data.append(row)
        if data:
            t = Table(data, colWidths=[doc.width / 2.0] * 2)
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, GRID),
                ("LINEAFTER", (0, 0), (0, -1), 0.4, GRID),
                ("BOX", (0, 0), (-1, -1), 0.4, GRID),
            ]))
            story.append(t)
        story.append(Spacer(1, 12))

    section("VEHICLE &amp; COIL IDENTITY", [
        ("Vehicle Number", r.get("vehicle_number")),
        ("Batch Number", r.get("batch_number")),
        ("Make of Coil", r.get("make_of_coil")),
        ("Grade", r.get("grade")),
        ("Coil ID (mm)", r.get("coil_id")),
        ("Coil Weight (T)", r.get("coil_weight")),
    ])
    section("DIMENSIONS", [
        ("Width (mm)", r.get("width")),
        ("Thickness (mm)", r.get("thickness")),
        ("Actual Thickness (mm)", r.get("actual_thickness")),
        ("Actual Width (mm)", r.get("actual_width")),
    ])
    section("PHYSICAL CONDITION", [
        ("Identification Sticker", _yn(r.get("id_sticker"))),
        ("Edge Damage - Inner", _yn(r.get("edge_inner"))),
        ("Edge Damage - Outer", _yn(r.get("edge_outer"))),
        ("Scratch Mark", _yn(r.get("scratch"))),
        ("Strapping", _yn(r.get("strapping"))),
        ("Rust on Surface", _yn(r.get("rust"))),
        ("Any Other Damages", r.get("other_damages")),
        ("Wheels India Coil", "Yes" if wheels else "No"),
    ])
    section("INSPECTOR &amp; REMARKS", [
        ("Inspected By", r.get("inspected_by")),
        ("Remarks", r.get("remarks")),
    ])

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 6))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()


# ============================================================
#  CTL Quality Inspection — single-record report (portrait A4)
# ============================================================
def _mp_cell(rec):
    if not rec:
        return ""
    init = "" if rec.get("initial") is None else str(rec.get("initial")).strip()
    adjs = [a for a in (rec.get("adjustments") or [])
            if a and ((str(a.get("value") or "").strip()) or (str(a.get("time") or "").strip()))]
    parts = []
    if init:
        parts.append("Set: " + init)
    for i, a in enumerate(adjs):
        v = str(a.get("value") or "").strip()
        t = str(a.get("time") or "").strip()
        cell = (v + " @ " + t) if (v and t) else (v or ("@ " + t if t else ""))
        parts.append(f"Adj{i+1}: " + cell)
    return "\n".join(parts)


def build_quality_pdf(d: dict, rec_id, mp_spec) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=30, rightMargin=30,
                            topMargin=24, bottomMargin=28)
    st = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=14.5, textColor=BRAND, alignment=1, leading=17),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUT, alignment=1),
        "meta": ParagraphStyle("m", fontName="Helvetica-Bold", fontSize=9, textColor=DARK, alignment=2, leading=12),
        "sec": ParagraphStyle("se", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.white, leading=12),
        "lab": ParagraphStyle("l", fontName="Helvetica", fontSize=8, textColor=MUT, leading=10),
        "val": ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#0e1524"), leading=11),
        "hd": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=7.6, textColor=DARK, alignment=1, leading=9),
        "cell": ParagraphStyle("c", fontName="Helvetica", fontSize=7.6, leading=9),
    }
    story = []
    logo = ""
    if os.path.exists(_LOGO):
        try:
            logo = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo = ""
    title_block = [Paragraph("CTL QUALITY INSPECTION", st["title"]),
                   Paragraph("Cut-to-Length Quality Report", st["sub"])]
    meta = Paragraph(f"Report #{rec_id}<br/>{str(d.get('date') or '')[:10]} {d.get('time') or ''}", st["meta"])
    head = Table([[logo, title_block, meta]], colWidths=[125, None, 125])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "LEFT"),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.1, BRAND), ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    story.append(head)
    story.append(Spacer(1, 10))
    W = doc.width

    def bar(title):
        t = Table([[Paragraph(title, st["sec"])]], colWidths=[W])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BRAND), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)

    def pairs(items):
        data, row = [], []
        for lab, val in items:
            row.append([Paragraph(lab, st["lab"]), Spacer(1, 1), Paragraph(_fmt(val) or "-", st["val"])])
            if len(row) == 3:
                data.append(row); row = []
        if row:
            while len(row) < 3:
                row.append("")
            data.append(row)
        if data:
            t = Table(data, colWidths=[W / 3.0] * 3)
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("LINEBELOW", (0, 0), (-1, -1), 0.4, GRID),
                                   ("BOX", (0, 0), (-1, -1), 0.4, GRID), ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID)]))
            story.append(t)
        story.append(Spacer(1, 10))

    bar("HEADER &amp; COIL IDENTITY")
    pairs([("Customer", d.get("customer_name")), ("Coil Number", d.get("coil_number")),
           ("Batch Number", d.get("batch_number")), ("Make", d.get("make")),
           ("Grade", d.get("coil_grade")), ("Thickness (mm)", d.get("coil_thickness")),
           ("Width (mm)", d.get("coil_width")), ("Weight (T)", d.get("coil_weight")),
           ("Machine", d.get("machine_name"))])

    # Machine params progression
    mp_by = {p.get("key"): p for p in (d.get("machine_params") or []) if p and p.get("key")}
    mp_rows = [[Paragraph("Parameter", st["hd"]), Paragraph("Initial / Adjustments", st["hd"])]]
    any_mp = False
    for key, label in mp_spec:
        cell = _mp_cell(mp_by.get(key))
        if cell:
            any_mp = True
        mp_rows.append([Paragraph(label, st["cell"]),
                        Paragraph((cell or "-").replace("\n", "<br/>"), st["cell"])])
    if any_mp:
        bar("MACHINE SETTING PARAMETERS")
        t = Table(mp_rows, colWidths=[W * 0.32, W * 0.68], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story.append(t)
        story.append(Spacer(1, 10))

    # Sheet measurements by cut group
    groups = d.get("cut_groups") or []
    if groups:
        bar("SHEET MEASUREMENTS")
        cols = ["Sheet", "Thk", "Width", "Length", "Diag 1", "Diag 2", "Speed", "Remarks"]
        for g in groups:
            story.append(Paragraph(f"Cut length: <b>{_fmt(g.get('cut_length')) or '-'} mm</b>", st["val"]))
            story.append(Spacer(1, 3))
            data = [[Paragraph(c, st["hd"]) for c in cols]]
            for s in (g.get("sheets") or []):
                data.append([Paragraph(_fmt(s.get(k)), st["cell"]) for k in
                             ("sheet_no", "thickness", "width", "length", "d1", "d2", "line_speed", "remarks")])
            t = Table(data, colWidths=[W*0.09, W*0.1, W*0.12, W*0.12, W*0.12, W*0.12, W*0.1, W*0.23], repeatRows=1)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
            story.append(t)
            story.append(Spacer(1, 6))
        story.append(Spacer(1, 4))

    # Processed quantity
    pq = d.get("processed_qty") or {}
    pq_rows = [[Paragraph(h, st["hd"]) for h in ("Size", "Length (mm)", "No. Sheets", "Weight (T)")]]
    for i in range(1, 21):
        s = pq.get(f"size_{i}") or {}
        if any(_fmt(s.get(k)) for k in ("length", "nos", "weight_t")):
            pq_rows.append([Paragraph(f"Size {i}", st["cell"]), Paragraph(_fmt(s.get("length")), st["cell"]),
                            Paragraph(_fmt(s.get("nos")), st["cell"]), Paragraph(_fmt(s.get("weight_t")), st["cell"])])
    if len(pq_rows) > 1:
        bar("PROCESSED QUANTITY")
        t = Table(pq_rows, colWidths=[W*0.22, W*0.26, W*0.26, W*0.26], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                               ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        story.append(Spacer(1, 10))

    bar("FINAL MEASUREMENTS &amp; CHECKLIST")
    pairs([("First Bit", d.get("first_bit")), ("Last Bit", d.get("last_bit")),
           ("Defective", d.get("defective")), ("Balance Wt (T)", d.get("balance_wt")),
           ("Coil Verified", d.get("coil_verified")), ("Blade Clearance", d.get("blade_clearance")),
           ("Bur", d.get("bur")), ("Cutting Finish", d.get("cutting_finish")),
           ("Scalling", d.get("scalling")), ("Pit Marks", d.get("pit_marks")),
           ("Waviness", d.get("waviness")), ("Center Bow", d.get("center_bow")),
           ("Cutting Bow", d.get("cutting_bow")), ("Surface Defects", d.get("surface_defects")),
           ("Sheets Measured", d.get("sheets_measured")), ("Out of Tolerance", d.get("oot_count")),
           ("Tolerance Profile", d.get("tolerance_profile"))])

    # Rejections
    if str(d.get("rejection_flag") or "").lower() == "yes" and (d.get("rejections") or []):
        bar("REJECTIONS")
        rr = [[Paragraph(h, st["hd"]) for h in ("Size", "Qty", "Type(s)")]]
        for r in d.get("rejections"):
            rr.append([Paragraph(_fmt(r.get("size")), st["cell"]), Paragraph(_fmt(r.get("qty")), st["cell"]),
                       Paragraph(", ".join(str(t) for t in (r.get("types") or [])), st["cell"])])
        t = Table(rr, colWidths=[W*0.25, W*0.2, W*0.55], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        story.append(Spacer(1, 10))

    bar("SIGN-OFF")
    pairs([("Operated By", d.get("operator")), ("Inspected By", d.get("inspector")),
           ("Remarks", d.get("remarks"))])

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()
