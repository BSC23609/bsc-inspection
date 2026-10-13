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


# ============================================================
#  Shearing Quality Inspection — single-record report (portrait A4)
# ============================================================
def build_shearing_pdf(d: dict, rec_id, mp_spec) -> bytes:
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
    title_block = [Paragraph("SHEARING QUALITY INSPECTION", st["title"]),
                   Paragraph("Shearing Line Quality Report", st["sub"])]
    meta = Paragraph(f"Report #{rec_id}<br/>{str(d.get('date') or '')[:10]}", st["meta"])
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
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("BOX", (0, 0), (-1, -1), 0.4, GRID),
                                   ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID)]))
            story.append(t)
        story.append(Spacer(1, 10))

    bar("HEADER &amp; IDENTITY")
    pairs([("Customer", d.get("customer_name")), ("Batch Number", d.get("batch_number")),
           ("Grade", d.get("grade")), ("Make", d.get("make")), ("Type", d.get("type")),
           ("Process", d.get("process")), ("Input Size", d.get("input_size")),
           ("Coil Thickness (mm)", d.get("coil_thickness")), ("Operator", d.get("operator"))])

    # Machine params
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

    # Output sizes
    osz = [x for x in (d.get("output_sizes") or []) if _fmt(x)]
    if osz:
        bar("OUTPUT SIZES")
        story.append(Paragraph(", ".join(str(x) for x in osz), st["val"]))
        story.append(Spacer(1, 10))

    # Sheet measurements
    sm = d.get("measurements") or []
    if sm:
        bar("SHEET MEASUREMENTS")
        cols = ["Sheet", "Thk", "Width 1", "Width 2", "Diag 1", "Diag 2", "Remarks"]
        data = [[Paragraph(c, st["hd"]) for c in cols]]
        for s in sm:
            data.append([Paragraph(_fmt(s.get(k)), st["cell"]) for k in
                         ("sheet_no", "thickness", "width1", "width2", "diag1", "diag2", "remarks")])
        t = Table(data, colWidths=[W*0.1, W*0.11, W*0.14, W*0.14, W*0.14, W*0.14, W*0.23], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                               ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        story.append(Spacer(1, 10))

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

    bar("QUALITY CHECKS")
    pairs([("Burr", d.get("burr")), ("Blade Clearance", d.get("blade_clearance")),
           ("Cutting Finish", d.get("cutting_finish")), ("Surface Condition", d.get("surface_condition")),
           ("Bow / Bend", d.get("bow_bend")), ("Taper Cutting", d.get("taper_cutting")),
           ("QC Name", d.get("qc_name")), ("Tolerance Profile", d.get("tolerance_profile")),
           ("Rejections?", d.get("rejection_flag"))])

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

    # Reworks
    if str(d.get("has_reworks") or "").lower() == "yes" and (d.get("reworks") or []):
        bar("REWORKS")
        rw = [[Paragraph(h, st["hd"]) for h in ("Defect", "Qty", "Required", "Actual", "Var", "Action", "Disposition")]]
        for r in d.get("reworks"):
            rw.append([Paragraph(_fmt(r.get("defect_code")), st["cell"]), Paragraph(_fmt(r.get("rework_qty")), st["cell"]),
                       Paragraph(_fmt(r.get("required")), st["cell"]), Paragraph(_fmt(r.get("actual")), st["cell"]),
                       Paragraph(_fmt(r.get("variation")), st["cell"]), Paragraph(_fmt(r.get("action_taken")), st["cell"]),
                       Paragraph(_fmt(r.get("disposition")), st["cell"])])
        t = Table(rw, colWidths=[W*0.14, W*0.08, W*0.12, W*0.12, W*0.09, W*0.25, W*0.2], repeatRows=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID), ("BACKGROUND", (0, 0), (-1, 0), HEAD),
                               ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 3),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)
        story.append(Spacer(1, 10))

    bar("SIGN-OFF")
    pairs([("QC Name", d.get("qc_name")), ("Remarks", d.get("remarks")),
           ("Overall Observation", d.get("overall_observation"))])

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()


# ============================================================
#  Pre-Delivery Inspection (PDI) — report PDF (portrait A4)
# ============================================================
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_pdi_pdf(rep: dict, tol: dict) -> bytes:
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
        "flag": ParagraphStyle("fl", fontName="Helvetica-Bold", fontSize=8.5, textColor=colors.HexColor("#c23438"), leading=11),
    }
    tTh = _f((tol or {}).get("thickness")) or 0.5
    tW = _f((tol or {}).get("width")) or 5
    tL = _f((tol or {}).get("length")) or 10
    story = []
    logo = ""
    if os.path.exists(_LOGO):
        try:
            logo = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo = ""
    title_block = [Paragraph("PRE-DELIVERY INSPECTION", st["title"]),
                   Paragraph("BSCQMS-PRD-009 · REV 01", st["sub"])]
    meta = Paragraph(f"{rep.get('report_no') or '-'}<br/>{str(rep.get('report_date') or '')[:10]}", st["meta"])
    head = Table([[logo, title_block, meta]], colWidths=[125, None, 140])
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

    def pairs(items, cols=3):
        data, row = [], []
        for lab, val in items:
            row.append([Paragraph(lab, st["lab"]), Spacer(1, 1), Paragraph(_fmt(val) or "-", st["val"])])
            if len(row) == cols:
                data.append(row); row = []
        if row:
            while len(row) < cols:
                row.append("")
            data.append(row)
        if data:
            t = Table(data, colWidths=[W / float(cols)] * cols)
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("BOX", (0, 0), (-1, -1), 0.4, GRID),
                                   ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID)]))
            story.append(t)
        story.append(Spacer(1, 10))

    bar("REPORT DETAILS")
    pairs([("Report No", rep.get("report_no")), ("Date", rep.get("report_date")),
           ("Customer", rep.get("customer"))])

    for idx, it in enumerate(rep.get("items") or []):
        sp = it.get("spec") or {}
        ob = it.get("obs") or {}
        ch = it.get("checks") or {}
        bar(f"ITEM {idx + 1}" + (f"  —  SO {it.get('so')}" if it.get("so") else ""))
        pairs([("Date", it.get("date")), ("SO No", it.get("so")), ("Slip No", it.get("slip")),
               ("Make", it.get("make")), ("Qty (Nos)", it.get("qty")), ("Weight (T)", it.get("wt"))])
        pairs([("Spec Thickness", sp.get("th")), ("Spec Width", sp.get("wd")), ("Spec Length", sp.get("len")),
               ("Obs Thickness", ob.get("th")), ("Obs Width", ob.get("wd")), ("Obs Length", ob.get("len"))])
        pairs([("Waviness", ch.get("waviness")), ("Line / Scratch", ch.get("line")),
               ("Water / Oil", ch.get("water")), ("Colour", ch.get("colour")), ("Sticker", ch.get("sticker"))])
        oot = []
        a, b = _f(sp.get("th")), _f(ob.get("th"))
        if a is not None and b is not None and abs(b - a) > tTh:
            oot.append("Thickness")
        a, b = _f(sp.get("wd")), _f(ob.get("wd"))
        if a is not None and b is not None and abs(b - a) > tW:
            oot.append("Width")
        a, b = _f(sp.get("len")), _f(ob.get("len"))
        if a is not None and b is not None and abs(b - a) > tL:
            oot.append("Length")
        notok = [k for k in ("waviness", "line", "water", "colour", "sticker")
                 if str(ch.get(k) or "").upper() == "NOT OK"]
        if oot or notok:
            msg = "⚠  Out of tolerance: " + (", ".join(oot) or "none")
            if notok:
                msg += "   |   NOT OK: " + ", ".join(notok)
            t = Table([[Paragraph(msg, st["flag"])]], colWidths=[W])
            t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fdecec")),
                                   ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#f0c0c0")),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
            story.append(t)
            story.append(Spacer(1, 10))

    bar("SIGN-OFF")
    pairs([("Inspected By", rep.get("inspected_by"))], cols=3)

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()


# ============================================================
#  8D Report — problem solving D1..D8 (portrait A4)
# ============================================================
NAVY = colors.HexColor("#1d4e74")
NAVY_HEAD = colors.HexColor("#e7eef6")


def build_8d_pdf(rep: dict) -> bytes:
    d = rep.get("details") or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=32, rightMargin=32,
                            topMargin=24, bottomMargin=28)
    st = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, textColor=NAVY, alignment=1, leading=18),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUT, alignment=1),
        "meta": ParagraphStyle("m", fontName="Helvetica-Bold", fontSize=9, textColor=NAVY, alignment=2, leading=12),
        "sec": ParagraphStyle("se", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.white, leading=12),
        "lab": ParagraphStyle("l", fontName="Helvetica", fontSize=8, textColor=MUT, leading=10),
        "val": ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#0e1524"), leading=11),
        "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#1f2937"), leading=13),
    }
    story = []
    logo = ""
    if os.path.exists(_LOGO):
        try:
            logo = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo = ""
    title_block = [Paragraph("8D PROBLEM SOLVING REPORT", st["title"]),
                   Paragraph("Eight Disciplines · D1–D8", st["sub"])]
    meta = Paragraph(f"{rep.get('report_no') or '-'}<br/>{str(rep.get('report_date') or '')[:10]}", st["meta"])
    head = Table([[logo, title_block, meta]], colWidths=[125, None, 140])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "LEFT"),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.1, NAVY), ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    story.append(head)
    story.append(Spacer(1, 10))
    W = doc.width

    def bar(title):
        t = Table([[Paragraph(title, st["sec"])]], colWidths=[W])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)

    def pairs(items, cols=3):
        data, row = [], []
        for lab, val in items:
            row.append([Paragraph(lab, st["lab"]), Spacer(1, 1), Paragraph(_fmt(val) or "-", st["val"])])
            if len(row) == cols:
                data.append(row); row = []
        if row:
            while len(row) < cols:
                row.append("")
            data.append(row)
        if data:
            t = Table(data, colWidths=[W / float(cols)] * cols)
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("BOX", (0, 0), (-1, -1), 0.4, GRID),
                                   ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID)]))
            story.append(t)
        story.append(Spacer(1, 10))

    def block(title, text):
        bar(title)
        body = _fmt(text) or "—"
        box = Table([[Paragraph(body.replace("\n", "<br/>"), st["body"])]], colWidths=[W])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, GRID), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfcfe"))]))
        story.append(box)
        story.append(Spacer(1, 10))

    bar("REPORT DETAILS")
    pairs([("Report No", rep.get("report_no")), ("Date", rep.get("report_date")), ("Customer", rep.get("customer")),
           ("Part", rep.get("part")), ("NCR / Complaint Ref", rep.get("ncr_ref")), ("Priority", rep.get("priority")),
           ("Status", rep.get("status"))])

    bar("D1 · TEAM")
    pairs([("Champion", d.get("d1_champion")), ("Team Leader", d.get("d1_leader"))], cols=2)
    if _fmt(d.get("d1_members")):
        block("D1 · Team Members", d.get("d1_members"))

    bar("D2 · PROBLEM DESCRIPTION")
    pairs([("Where", d.get("d2_where")), ("When", d.get("d2_when")), ("How Detected", d.get("d2_detected")),
           ("Quantity Affected", d.get("d2_qty"))], cols=2)
    block("D2 · Problem Statement", d.get("d2_problem"))

    block("D3 · Interim Containment Actions", d.get("d3_containment"))
    block("D4 · Root Cause Analysis", d.get("d4_root_cause"))
    if _fmt(d.get("d4_why")):
        block("D4 · Why-Why Analysis", d.get("d4_why"))
    if _fmt(d.get("d4_escape")):
        block("D4 · Escape Point", d.get("d4_escape"))
    block("D5 · Permanent Corrective Actions", d.get("d5_corrective"))
    block("D6 · Implement &amp; Validate", d.get("d6_implement"))
    block("D7 · Prevent Recurrence", d.get("d7_prevent"))
    bar("D8 · RECOGNIZE TEAM &amp; CLOSE")
    pairs([("Closure Date", d.get("d8_close_date"))], cols=3)
    if _fmt(d.get("d8_recognition")):
        block("D8 · Recognition / Closure Notes", d.get("d8_recognition"))

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Prepared by {_fmt(rep.get('created_by')) or '-'} &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()


# ============================================================
#  Quality Complaint — report PDF (portrait A4, navy)
# ============================================================
def build_complaint_pdf(rep: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=32, rightMargin=32,
                            topMargin=24, bottomMargin=28)
    st = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, textColor=NAVY, alignment=1, leading=18),
        "sub": ParagraphStyle("s", fontName="Helvetica", fontSize=8.5, textColor=MUT, alignment=1),
        "meta": ParagraphStyle("m", fontName="Helvetica-Bold", fontSize=9, textColor=NAVY, alignment=2, leading=12),
        "sec": ParagraphStyle("se", fontName="Helvetica-Bold", fontSize=9.5, textColor=colors.white, leading=12),
        "lab": ParagraphStyle("l", fontName="Helvetica", fontSize=8, textColor=MUT, leading=10),
        "val": ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=9, textColor=colors.HexColor("#0e1524"), leading=11),
        "body": ParagraphStyle("b", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#1f2937"), leading=13),
    }
    story = []
    logo = ""
    if os.path.exists(_LOGO):
        try:
            logo = Image(_LOGO, width=40 * mm, height=11 * mm, kind="proportional")
        except Exception:
            logo = ""
    title_block = [Paragraph("QUALITY COMPLAINT", st["title"]),
                   Paragraph((rep.get("source") or "Customer") + " complaint", st["sub"])]
    meta = Paragraph(f"{rep.get('complaint_no') or '-'}<br/>{str(rep.get('qc_date') or '')[:10]}", st["meta"])
    head = Table([[logo, title_block, meta]], colWidths=[125, None, 140])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (0, 0), "LEFT"),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.1, NAVY), ("BOTTOMPADDING", (0, 0), (-1, 0), 8)]))
    story.append(head)
    story.append(Spacer(1, 10))
    W = doc.width

    def bar(title):
        t = Table([[Paragraph(title, st["sec"])]], colWidths=[W])
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)

    def pairs(items, cols=3):
        data, row = [], []
        for lab, val in items:
            row.append([Paragraph(lab, st["lab"]), Spacer(1, 1), Paragraph(_fmt(val) or "-", st["val"])])
            if len(row) == cols:
                data.append(row); row = []
        if row:
            while len(row) < cols:
                row.append("")
            data.append(row)
        if data:
            t = Table(data, colWidths=[W / float(cols)] * cols)
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("LEFTPADDING", (0, 0), (-1, -1), 7),
                                   ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("BOX", (0, 0), (-1, -1), 0.4, GRID),
                                   ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID)]))
            story.append(t)
        story.append(Spacer(1, 10))

    def block(title, text):
        bar(title)
        body = _fmt(text) or "—"
        box = Table([[Paragraph(body.replace("\n", "<br/>"), st["body"])]], colWidths=[W])
        box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, GRID), ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 7),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfcfe"))]))
        story.append(box)
        story.append(Spacer(1, 10))

    bar("COMPLAINT DETAILS")
    pairs([("Complaint No", rep.get("complaint_no")), ("Source", rep.get("source")), ("Status", rep.get("status")),
           ("Filed By", rep.get("filed_by")), ("QC Date", rep.get("qc_date")),
           ("Customer", rep.get("customer_name")), ("Customer Email", rep.get("customer_email"))])

    bar("MATERIAL DETAILS")
    pairs([("Grade", rep.get("grade")), ("Dimensions", rep.get("dimensions")), ("Batch No", rep.get("batch_number")),
           ("TC No", rep.get("tc_number")), ("Invoice No", rep.get("invoice_no")), ("Invoice Date", rep.get("invoice_date")),
           ("Quantity (T)", rep.get("quantity")), ("Mill", rep.get("mill"))])

    block("DEFECT DESCRIPTION", rep.get("defect_description"))

    if _fmt(rep.get("vendor_name")) or _fmt(rep.get("vendor_email")):
        bar("VENDOR ESCALATION")
        pairs([("Vendor", rep.get("vendor_name")), ("Vendor Email", rep.get("vendor_email"))], cols=2)

    if _fmt(rep.get("resolution")):
        block("RESOLUTION", rep.get("resolution"))

    assets = rep.get("assets") or {}
    att = (assets.get("attachments") or {})
    photos = assets.get("photos") or []
    if att or photos:
        lines = []
        for k in ("po", "so", "tc", "invoice"):
            a = att.get(k)
            if a:
                lines.append(f"{k.upper()}: {a.get('name') or a.get('path')}")
        if photos:
            lines.append(f"Defect photos: {len(photos)} attached (in OneDrive)")
        block("ATTACHMENTS", "\n".join(lines))

    gen = datetime.now().strftime("%d-%b-%Y %H:%M")
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Bharat Steel (Chennai) Pvt. Ltd. &nbsp;·&nbsp; Filed by {_fmt(rep.get('created_by')) or '-'} &nbsp;·&nbsp; Generated {gen}",
                           ParagraphStyle("g", fontName="Helvetica", fontSize=7.5, textColor=MUT, alignment=2)))
    doc.build(story)
    return buf.getvalue()
