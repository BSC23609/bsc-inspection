"""Work-order style maintenance report PDFs (Opmaint-style), for saving to OneDrive:
preventive (checklist completion) reports and breakdown reports. Uses the same
reportlab + BSC logo setup as pdfgen.py. Returns PDF bytes."""
import os
import io
from datetime import datetime
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                 Spacer, Image)

NAVY = colors.HexColor("#1F3B5B")
BLUE = colors.HexColor("#0069aa")
DARK = colors.HexColor("#1F2A37")
MUT = colors.HexColor("#6B7280")
GREEN = colors.HexColor("#2F7D32")
LINEC = colors.HexColor("#D7DEE7")
SOFT = colors.HexColor("#F3F6FA")
YELL = colors.HexColor("#FFF3D6")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO = os.path.join(_BASE, "static", "bsc-logo.png")

_S = {
    "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=16, textColor=NAVY, leading=19),
    "lbl": ParagraphStyle("l", fontName="Helvetica-Bold", fontSize=8.5, textColor=DARK, leading=11),
    "val": ParagraphStyle("v", fontName="Helvetica", fontSize=8.5, textColor=DARK, leading=11),
    "num": ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=9, textColor=NAVY, alignment=1),
    "cp": ParagraphStyle("cp", fontName="Helvetica-Bold", fontSize=8.5, textColor=DARK, leading=11),
    "res": ParagraphStyle("re", fontName="Helvetica", fontSize=8.5, textColor=DARK, leading=11),
    "when": ParagraphStyle("w", fontName="Helvetica", fontSize=7.5, textColor=MUT, leading=9.5),
    "sec": ParagraphStyle("s", fontName="Helvetica-Bold", fontSize=11, textColor=NAVY, leading=14),
    "small": ParagraphStyle("sm", fontName="Helvetica", fontSize=8, textColor=MUT, leading=10),
}


def _fmt_dt(v):
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            try:
                v = v.astimezone(_IST)
            except Exception:
                pass
        return v.strftime("%d %b %Y %H:%M")
    return str(v) if v else "--"


def _p(txt, style="val"):
    return Paragraph("" if txt is None else str(txt), _S[style])


def _qr(url, size=34 * mm):
    try:
        from reportlab.graphics.barcode import qr
        from reportlab.graphics.shapes import Drawing
        from reportlab.graphics import renderPDF  # noqa
        code = qr.QrCodeWidget(url or "")
        b = code.getBounds()
        w, h = b[2] - b[0], b[3] - b[1]
        d = Drawing(size, size, transform=[size / w, 0, 0, size / h, 0, 0])
        d.add(code)
        return d
    except Exception:
        return ""


def _kv_table(pairs, label_w=40 * mm, val_w=48 * mm):
    rows = []
    for k, v in pairs:
        rows.append([_p(k, "lbl"), v if hasattr(v, "wrapOn") else _p(v, "val")])
    t = Table(rows, colWidths=[label_w, val_w])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), SOFT),
        ("GRID", (0, 0), (-1, -1), 0.5, LINEC),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _header(title, qr_url):
    logo = ""
    try:
        if os.path.exists(_LOGO):
            logo = Image(_LOGO, width=34 * mm, height=13 * mm, kind="proportional")
    except Exception:
        logo = ""
    q = _qr(qr_url)
    head = Table([[logo, "", q]], colWidths=[45 * mm, None, 38 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("ALIGN", (2, 0), (2, 0), "RIGHT")]))
    return [head, Spacer(1, 6), Paragraph(title, _S["title"]), Spacer(1, 10)]


def _two_cols(left_pairs, right_pairs):
    lt = _kv_table(left_pairs)
    rt = _kv_table(right_pairs)
    outer = Table([[lt, "", rt]], colWidths=[90 * mm, 6 * mm, 90 * mm])
    outer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return outer


def _section(title):
    return [Spacer(1, 12), Paragraph(title, _S["sec"]), Spacer(1, 4)]


def _items_table(items, cols=("#", "CHECKPOINT", "RESULT", "RECORDED")):
    data = [[_p(c, "lbl") for c in cols]]
    for row in items:
        data.append([_p(row[0], "num"), _p(row[1], "cp"), _p(row[2], "res"), _p(row[3], "when")])
    t = Table(data, colWidths=[12 * mm, 70 * mm, None, 32 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, LINEC),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
    ]))
    return t


def _build(title, left, right, section_title, items, item_cols, footer_lines, qr_url):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm, title=title)
    story = []
    story += _header(title, qr_url)
    story.append(_two_cols(left, right))
    if items:
        story += _section(section_title)
        story.append(_items_table(items, item_cols))
    if footer_lines:
        story.append(Spacer(1, 10))
        fp = [[_p(fl[0], "lbl"), _p(fl[1], "val")] for fl in footer_lines]
        ft = Table(fp, colWidths=[40 * mm, None])
        ft.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT),
                                ("BOX", (0, 0), (-1, -1), 0.5, LINEC),
                                ("TOPPADDING", (0, 0), (-1, -1), 4),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                                ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
        story.append(ft)
    doc.build(story)
    return buf.getvalue()


# ----------------------------------------------------------------- PM report
def build_pm_report(meta: dict, checkpoints: list) -> bytes:
    """meta: asset_code, asset_name, checklist, doc_no, started, completed, due,
    raised_by, category, location. checkpoints: [(n, text, result, when_by), ...]."""
    title = f"#{meta.get('doc_no','')} {meta.get('asset_code','')} {meta.get('asset_name','')} ({meta.get('checklist','Checklist')})".strip()
    left = [
        ("Doc No:", meta.get("doc_no", "--")),
        ("Started on:", _fmt_dt(meta.get("started"))),
        ("Due by:", _fmt_dt(meta.get("due")) if meta.get("due") else "--"),
        ("Completed on:", _fmt_dt(meta.get("completed"))),
        ("Raised by:", meta.get("raised_by", "--")),
    ]
    right = [
        ("Description:", f"{meta.get('asset_name','')} ({meta.get('checklist','')})"),
        ("Category:", meta.get("category", "Preventive Maintenance")),
        ("Location:", meta.get("location", "--") or "--"),
        ("Asset:", f"{meta.get('asset_code','')} - {meta.get('asset_name','')}"),
        ("Status:", meta.get("status", "Completed")),
    ]
    footer = [("Completed by:", meta.get("completed_by", "--")),
              ("Completed at:", _fmt_dt(meta.get("completed")))]
    return _build(title, left, right, "Inspection Report", checkpoints,
                  ("#", "CHECKPOINT", "RESULT", "RECORDED"), footer, meta.get("qr_url", ""))


# ---------------------------------------------------------- breakdown report
def build_breakdown_report(meta: dict) -> bytes:
    """meta: asset_code, asset_name, doc_no, btype, problem, raised, raised_by,
    assignees, location, status, closed, closed_by, resolution, qr_url."""
    title = f"#{meta.get('doc_no','')} {meta.get('asset_code','')} {meta.get('asset_name','')} (Breakdown)".strip()
    left = [
        ("Doc No:", meta.get("doc_no", "--")),
        ("Raised on:", _fmt_dt(meta.get("raised"))),
        ("Raised by:", meta.get("raised_by", "--")),
        ("Breakdown type:", meta.get("btype", "--") or "--"),
        ("Status:", meta.get("status", "Open")),
    ]
    right = [
        ("Asset:", f"{meta.get('asset_code','')} - {meta.get('asset_name','')}"),
        ("Category:", "Breakdown Maintenance"),
        ("Location:", meta.get("location", "--") or "--"),
        ("Assigned to:", meta.get("assignees", "--") or "--"),
        ("Closed on:", _fmt_dt(meta.get("closed")) if meta.get("closed") else "--"),
    ]
    items = [
        ("1", "Problem reported", meta.get("problem", "--") or "--", _fmt_dt(meta.get("raised"))),
        ("2", "Action taken", meta.get("resolution", "--") or "--",
         _fmt_dt(meta.get("closed")) if meta.get("closed") else "pending"),
    ]
    footer = [("Raised by:", meta.get("raised_by", "--")),
              ("Assigned to:", meta.get("assignees", "--") or "--"),
              ("Closed by:", meta.get("closed_by", "--") or "--")]
    return _build(title, left, right, "Breakdown Details", items,
                  ("#", "ITEM", "DETAIL", "WHEN"), footer, meta.get("qr_url", ""))
