# SPDX-License-Identifier: GPL-2.0-or-later
"""Activity log downloads: CSV for spreadsheets, PDF for printing or sending on.

Both cover one area of the log (or everything) between two dates. Times are the
Pi's local time, as everywhere else in CoilGate.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from . import activity

FONT_DIR = Path(__file__).resolve().parent / "pdf_fonts"
PDF_ROW_LIMIT = 5000          # about 150 pages; CSV has no limit

ACTIVITY_HEADER = ["When", "Area", "What", "Change", "By", "Detail", "IP address"]
SIGNIN_HEADER = ["When", "Event", "Username", "Result", "IP address", "Device"]
# Relative column widths for the PDF (landscape A4).
ACTIVITY_WIDTHS = (30, 26, 40, 34, 40, 62, 28)
SIGNIN_WIDTHS = (32, 46, 34, 24, 30, 54)


class ExportTooLarge(Exception):
    pass


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def activity_rows(db: sqlite3.Connection, area: str, start: int, end: int) -> list[list[str]]:
    """Activity entries from start (inclusive) to end (exclusive), oldest first."""
    sql = "SELECT * FROM activity WHERE ts >= ? AND ts < ?"
    args: list = [start, end]
    if area != "all":
        sql += " AND category = ?"
        args.append(area)
    rows = db.execute(sql + " ORDER BY ts, id", args).fetchall()
    return [[
        _when(r["ts"]),
        activity.CATEGORIES.get(r["category"], r["category"]),
        r["target"] or "",
        activity.changed_to(r),
        activity.done_by(r),
        activity.extra_detail(r),
        r["ip"] or "",
    ] for r in rows]


def signin_rows(db: sqlite3.Connection, start: int, end: int) -> list[list[str]]:
    rows = db.execute(
        "SELECT * FROM auth_events WHERE event != 'password_ok' AND ts >= ? AND ts < ?"
        " ORDER BY ts, id", (start, end)).fetchall()
    return [[
        _when(r["ts"]),
        activity.SIGNIN_EVENTS.get(r["event"], r["event"]),
        r["username"] or "",
        "Succeeded" if r["success"] else "Failed",
        r["ip"] or "",
        activity.describe_device(r["user_agent"]) if r["user_agent"] else "",
    ] for r in rows]


# ---------------------------------------------------------------- CSV

def _safe_cell(value: str) -> str:
    """Stop spreadsheet apps treating a name like "=HYPERLINK(...)" as a formula."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def to_csv(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([_safe_cell(str(v)) for v in row] for row in rows)
    # The byte-order mark makes Excel read the file as UTF-8 (°C, names with accents).
    return buffer.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------- PDF

@lru_cache(maxsize=1)
def _font_chars() -> frozenset[int]:
    from fontTools.ttLib import TTFont
    with TTFont(FONT_DIR / "Barlow-Regular.ttf") as font:
        return frozenset(font.getBestCmap())


def _printable(text: str) -> str:
    """Replace characters the embedded font doesn't have."""
    chars = _font_chars()
    return "".join(c if ord(c) in chars or c in "\n" else "?" for c in text)


def to_pdf(*, site: str, title: str, scope: str, generated_by: str,
           header: list[str], widths: tuple[int, ...], rows: list[list[str]]) -> bytes:
    if len(rows) > PDF_ROW_LIMIT:
        raise ExportTooLarge(
            f"That's {len(rows):,} entries, more than a PDF can sensibly hold "
            f"({PDF_ROW_LIMIT:,}). Choose a shorter period or one area, or download a CSV.")
    from fpdf import FPDF

    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    running = _printable(f"{site} · {title} · {scope}")

    class Report(FPDF):
        def header(self):
            if self.page_no() > 1:
                self.set_font("Barlow", size=8)
                self.set_text_color(110, 120, 130)
                self.cell(0, 5, running, new_x="LMARGIN", new_y="NEXT")
                self.ln(2)

        def footer(self):
            self.set_y(-11)
            self.set_font("Barlow", size=8)
            self.set_text_color(110, 120, 130)
            self.cell(0, 5, _printable(f"Generated {generated} by {generated_by}"), align="L")
            self.set_x(self.l_margin)
            self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    pdf = Report(orientation="L", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_font("Barlow", "", str(FONT_DIR / "Barlow-Regular.ttf"))
    pdf.add_font("Barlow", "B", str(FONT_DIR / "Barlow-SemiBold.ttf"))
    pdf.set_title(_printable(f"{site} {title}"))
    pdf.set_author(_printable(site))
    pdf.set_creator("CoilGate")
    pdf.add_page()

    pdf.set_text_color(26, 35, 48)
    pdf.set_font("Barlow", "B", 18)
    pdf.cell(0, 9, _printable(f"{site}: {title}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Barlow", size=10)
    pdf.set_text_color(86, 98, 110)
    count = f"{len(rows):,} entr{'y' if len(rows) == 1 else 'ies'}"
    pdf.cell(0, 6, _printable(f"{scope} · {count} · times are the Pi's local time"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_text_color(26, 35, 48)

    if not rows:
        pdf.set_font("Barlow", size=11)
        pdf.cell(0, 8, "Nothing was recorded in this period.")
        return bytes(pdf.output())

    _draw_table(pdf, header, widths, rows)
    return bytes(pdf.output())


def _draw_table(pdf, header: list[str], widths: tuple[int, ...], rows: list[list[str]]) -> None:
    """A simple striped table. Drawn directly rather than with fpdf2's table
    helper, which is several times slower on a Pi for long logs."""
    size, line_h, pad_x, pad_y = 8.5, 4.2, 1.6, 1.3
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    scale = usable / sum(widths)
    cols = [w * scale for w in widths]
    xs = [pdf.l_margin + sum(cols[:i]) for i in range(len(cols))]
    widths_cache: dict[str, float] = {}

    def width(text: str) -> float:
        w = widths_cache.get(text)
        if w is None:
            w = widths_cache[text] = pdf.get_string_width(text)
        return w

    def wrap(text: str, room: float) -> list[str]:
        lines: list[str] = []
        for para in (text or "").split("\n"):
            line = ""
            for word in para.split(" "):
                candidate = f"{line} {word}" if line else word
                if width(candidate) <= room or not line:
                    line = candidate
                else:
                    lines.append(line)
                    line = word
                # A single word wider than the column is cut to fit.
                while width(line) > room and len(line) > 1:
                    cut = len(line) - 1
                    while cut > 1 and width(line[:cut]) > room:
                        cut -= 1
                    lines.append(line[:cut])
                    line = line[cut:]
            lines.append(line)
        return lines or [""]

    def draw_row(cells: list[str], heading: bool, shade: bool) -> None:
        wrapped = [wrap(text, cols[i] - 2 * pad_x) for i, text in enumerate(cells)]
        height = max(len(w) for w in wrapped) * line_h + 2 * pad_y
        if pdf.get_y() + height > pdf.page_break_trigger:
            pdf.add_page()
            if not heading:
                draw_row(header, True, False)
        top = pdf.get_y()
        if heading or shade:
            pdf.set_fill_color(*((234, 237, 240) if heading else (246, 247, 249)))
            pdf.rect(pdf.l_margin, top, usable, height, style="F")
        pdf.set_font("Barlow", "B" if heading else "", size)
        pdf.set_text_color(*((86, 98, 110) if heading else (26, 35, 48)))
        for x, lines in zip(xs, wrapped):
            for n, text in enumerate(lines):
                if text:
                    pdf.text(x + pad_x, top + pad_y + (n + 0.78) * line_h, text)
        pdf.set_draw_color(214, 219, 225)
        pdf.line(pdf.l_margin, top + height, pdf.l_margin + usable, top + height)
        pdf.set_y(top + height)

    pdf.set_font("Barlow", "", size)
    draw_row(header, True, False)
    for i, row in enumerate(rows):
        draw_row([_printable(v) for v in row], False, i % 2 == 1)
