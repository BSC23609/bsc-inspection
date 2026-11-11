"""Render the maintenance overdue board as a PNG image, so it can be delivered
to admins through a WhatsApp image-header template (same pattern as the daily
stock report: the template header points at a hosted PNG, and this module keeps
that PNG current). Pure Pillow — no matplotlib — so it runs in the Vercel/Render
runtime with just `Pillow` added to requirements.

build_overdue_png(tdisp, pm, bd) -> PNG bytes.
  pm: list of dicts {code,name,cat,who,date,status}  (preventive: overdue / due today)
  bd: list of dicts {code,name,prob,who,status,date}  (open breakdowns)
"""
import os
from PIL import Image, ImageDraw, ImageFont

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FONT_DIR = os.path.join(_BASE, "static", "fonts")
_LOGO = os.path.join(_BASE, "static", "bsc-logo.png")

# ---- palette (matches maint_pdf.py) ----
NAVY = (31, 59, 91)
DARK = (31, 42, 55)
MUT = (107, 114, 128)
LINEC = (215, 222, 231)
SOFT = (243, 246, 250)
WHITE = (255, 255, 255)
RED = (197, 48, 48)
RED_BG = (253, 232, 232)
AMBER = (146, 94, 7)
AMBER_BG = (255, 243, 214)
GREY_BG = (236, 240, 245)

# columns: (key, header, width)
COLS = [
    ("sno", "S.No", 60),
    ("date", "Date", 150),
    ("code", "M/C Code", 122),
    ("name", "Machine Name", 218),
    ("cat", "Category", 182),
    ("status", "Status", 132),
    ("who", "Assigned To", 192),
    ("by", "Assigned By", 192),
]
PAD = 12           # cell left/right padding
VPAD = 9           # cell top/bottom padding
MARGIN = 28        # page margin
HEADER_H = 96      # title band height
ROWH_HDR = 42      # table header row height


def _font(bold=False, size=20):
    path = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        try:
            return ImageFont.load_default(size)
        except Exception:
            return ImageFont.load_default()


def _cat(c):
    c = (c or "").lower()
    if "quarter" in c:
        return "Quarterly Maintenance"
    if "daily" in c:
        return "Daily Maintenance"
    return (c.title() if c else "-")


def _wrap(draw, text, font, max_w):
    """Greedy word-wrap to a pixel width; returns a list of lines."""
    text = "" if text is None else str(text)
    if not text:
        return ["-"]
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    # hard-break any single token still too wide
    out = []
    for ln in lines:
        while draw.textlength(ln, font=font) > max_w and len(ln) > 1:
            cut = len(ln)
            while cut > 1 and draw.textlength(ln[:cut], font=font) > max_w:
                cut -= 1
            out.append(ln[:cut])
            ln = ln[cut:]
        out.append(ln)
    return out or ["-"]


def _rows(pm, bd):
    """Flatten preventive + breakdown dicts into display rows and order them
    Overdue -> Open -> Due today."""
    rows = []
    for it in (pm or []):
        rows.append({
            "date": it.get("date") or "-",
            "code": (it.get("code") or "").strip() or "-",
            "name": (it.get("name") or "").strip() or "-",
            "cat": _cat(it.get("cat")),
            "status": it.get("status") or "Overdue",
            "who": (it.get("who") or "-").strip() or "-",
            "by": (it.get("by") or "-").strip() or "-",
        })
    for it in (bd or []):
        rows.append({
            "date": it.get("date") or "-",
            "code": (it.get("code") or "").strip() or "-",
            "name": (it.get("name") or "").strip() or "-",
            "cat": "Breakdown",
            "status": it.get("status") or "Open",
            "who": (it.get("who") or "-").strip() or "-",
            "by": (it.get("by") or "-").strip() or "-",
        })
    prio = {"Overdue": 0, "Open": 1, "Due today": 2}
    rows.sort(key=lambda r: prio.get(r["status"], 1))
    for i, r in enumerate(rows, 1):
        r["sno"] = str(i)
    return rows


def _status_style(s):
    s = (s or "").lower()
    if "due today" in s:
        return AMBER, AMBER_BG
    if "open" in s:
        return RED, RED_BG
    return RED, RED_BG  # Overdue


def build_overdue_png(tdisp, pm, bd):
    rows = _rows(pm, bd)
    f_hdr = _font(bold=True, size=19)
    f_cell = _font(bold=False, size=19)
    f_cell_b = _font(bold=True, size=19)
    f_title = _font(bold=True, size=30)
    f_sub = _font(bold=False, size=20)
    f_foot = _font(bold=True, size=18)
    line_h = 25

    table_w = sum(c[2] for c in COLS)
    W = table_w + 2 * MARGIN

    tmp = Image.new("RGB", (10, 10))
    d0 = ImageDraw.Draw(tmp)

    # pre-compute wrapped lines + row heights
    wrap_cols = {"name", "cat", "who", "by"}
    prepared = []
    for r in rows:
        cell_lines, max_lines = {}, 1
        for key, _hdr, w in COLS:
            avail = w - 2 * PAD
            if key in wrap_cols:
                ls = _wrap(d0, r[key], f_cell, avail)
            else:
                ls = [str(r[key])]
            cell_lines[key] = ls
            max_lines = max(max_lines, len(ls))
        rh = max_lines * line_h + 2 * VPAD
        prepared.append((r, cell_lines, rh))

    body_h = sum(p[2] for p in prepared) if prepared else 60
    H = MARGIN + HEADER_H + ROWH_HDR + body_h + 54  # +footer band

    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    # ---- title band ----
    y = MARGIN
    logo_w = 0
    try:
        if os.path.exists(_LOGO):
            logo = Image.open(_LOGO).convert("RGBA")
            lw = 150
            lh = int(logo.height * lw / logo.width)
            logo = logo.resize((lw, lh))
            img.paste(logo, (MARGIN, y), logo)
            logo_w = lw + 18
    except Exception:
        logo_w = 0
    d.text((MARGIN + logo_w, y + 6), "BSC MAINTENANCE", font=f_title, fill=NAVY)
    d.text((MARGIN + logo_w, y + 48), f"Maintenance overdue as of {tdisp}", font=f_sub, fill=MUT)
    y += HEADER_H

    # ---- table header ----
    x = MARGIN
    d.rectangle([MARGIN, y, MARGIN + table_w, y + ROWH_HDR], fill=NAVY)
    for key, hdr, w in COLS:
        d.text((x + PAD, y + (ROWH_HDR - line_h) // 2 + 2), hdr, font=f_hdr, fill=WHITE)
        x += w
    y += ROWH_HDR

    # ---- body ----
    if not prepared:
        d.rectangle([MARGIN, y, MARGIN + table_w, y + 60], outline=LINEC, width=1)
        d.text((MARGIN + PAD, y + 20), "No overdue or due items right now.",
               font=f_cell, fill=MUT)
        y += 60
    for idx, (r, cell_lines, rh) in enumerate(prepared):
        bg = WHITE if idx % 2 == 0 else SOFT
        d.rectangle([MARGIN, y, MARGIN + table_w, y + rh], fill=bg)
        x = MARGIN
        for key, _hdr, w in COLS:
            cy = y + VPAD
            if key == "status":
                st = r["status"]
                fg, chip = _status_style(st)
                tw = d0.textlength(st, font=f_cell_b)
                chip_w = tw + 20
                chip_x = x + PAD
                d.rounded_rectangle([chip_x, cy - 1, chip_x + chip_w, cy + line_h + 1],
                                    radius=9, fill=chip)
                d.text((chip_x + 10, cy + 1), st, font=f_cell_b, fill=fg)
            else:
                fnt = f_cell_b if key == "sno" else f_cell
                col = NAVY if key == "sno" else DARK
                for ln in cell_lines[key]:
                    d.text((x + PAD, cy), ln, font=fnt, fill=col)
                    cy += line_h
            x += w
        # row separator
        d.line([MARGIN, y + rh, MARGIN + table_w, y + rh], fill=LINEC, width=1)
        y += rh

    # ---- grid: outer box + vertical column lines ----
    top = MARGIN + HEADER_H
    bottom = y
    d.rectangle([MARGIN, top, MARGIN + table_w, bottom], outline=LINEC, width=1)
    x = MARGIN
    for key, _hdr, w in COLS[:-1]:
        x += w
        d.line([x, top, x, bottom], fill=LINEC, width=1)

    # ---- footer ----
    d.text((MARGIN, bottom + 16), "THE BHARAT STEEL GROUP", font=f_foot, fill=NAVY)
    n = len(rows)
    tag = f"{n} item{'s' if n != 1 else ''}"
    tw = d0.textlength(tag, font=f_sub)
    d.text((MARGIN + table_w - tw, bottom + 17), tag, font=f_sub, fill=MUT)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
