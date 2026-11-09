"""CSV / XLSX / JSON / ZIP export of extracted tables."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.hyperlink import Hyperlink

from .extract.quality import COL_NO

ID_NUM = re.compile(r"^-?\d{1,3}(?:\.\d{3})+(?:,\d+)?$|^-?\d+(?:,\d+)?$")  # 1.234.567,89 · 12,8 · 1300
EN_NUM = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$|^-?\d+(?:\.\d+)?$")  # 1,234,567.89 · 12.8


def to_number(s: str, decimal: str = ","):
    """BPS number text -> float/int, or None when it is not a plain number."""
    t = (s or "").strip().replace("\u2212", "-").replace(" ", "")
    if not t:
        return None
    if decimal == ",":
        if not ID_NUM.match(t):
            return None
        t = t.replace(".", "").replace(",", ".")
    else:
        if not EN_NUM.match(t):
            return None
        t = t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return None
    return int(v) if "." not in t and abs(v) < 2**53 else v


# ---------------------------------------------------------------- decimal separator detection

_TOKEN = re.compile(r"^\d+(?:[.,]\d+)+$")  # digits with at least one separator; plain integers say nothing
_WRAP = re.compile(r"^[(\[]?[-+\u2212\u2013]?|%?[)\]]?\*{0,3}$")


def _vote(cell: str) -> tuple[str, bool] | None:
    """What one cell says about the decimal separator: (',' or '.', strong?) or None (no evidence).

    strong: 10.498,2 / 1,234.5 (both present: the last one is the decimal) · 1.234.567 (repeated:
            thousands) · 25,9 / 405.5 / 0,125 / 1234.567 (one separator not followed by a 3-digit group)
    weak:   1.234 / 1,234 (one separator + exactly 3 digits: most likely a thousands separator)"""
    t = _WRAP.sub("", (cell or "").strip().replace("\u00a0", "").replace(" ", ""))
    if not _TOKEN.match(t):
        return None
    seps = [ch for ch in t if ch in ".,"]
    groups = re.split(r"[.,]", t)
    if len(set(seps)) == 2:
        dec = seps[-1]
        if seps.count(dec) != 1 or t.rfind(dec) < max(t.rfind(","), t.rfind(".")):
            return None  # 1,2.3,4: not a number
        thousands = groups[1:-1]
        if len(groups[0]) > 3 or any(len(g) != 3 for g in thousands):
            return None
        return dec, True
    s = seps[0]
    other = "," if s == "." else "."
    if len(seps) > 1:  # same separator repeated: thousands grouping (if the groups fit)
        if len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:]):
            return None  # 1.2.3 (a date or a code)
        return other, True
    head, tail = groups
    if len(tail) != 3 or len(head) > 3 or head == "0":
        return s, True
    return other, False


def detect_decimal(rows: list[list[str]], header_rows: int = 0, default: str = ",") -> dict:
    """Guess the decimal separator of a table from its body cells.

    Every number with a separator votes. Strong votes decide; weak ones (1.234: ambiguous) only break
    a tie; with no evidence at all the default (Indonesian / BPS: ',') is kept."""
    body = rows[header_rows:] or rows
    strong = {",": 0, ".": 0}
    weak = {",": 0, ".": 0}
    for r in body:
        for c in r:
            v = _vote(c)
            if v:
                (strong if v[1] else weak)[v[0]] += 1
    if strong[","] != strong["."]:
        dec = "," if strong[","] > strong["."] else "."
        sure = True
    elif weak[","] != weak["."]:
        dec = "," if weak[","] > weak["."] else "."
        sure = False
    else:
        dec, sure = default, False
    return {"decimal": dec, "sure": sure, "strong": strong, "weak": weak}


def table_decimal(t: dict, requested: str = "auto") -> str:
    """Separator used to read the numbers of a table: an explicit export choice wins, then the table's
    own setting, then auto-detection."""
    if requested in (",", "."):
        return requested
    own = t.get("decimal_sep") or "auto"
    if own in (",", "."):
        return own
    return detect_decimal(t["rows"], t.get("header_rows", 1))["decimal"]


def column_names(rows: list[list[str]], header_rows: int) -> list[str]:
    width = max((len(r) for r in rows), default=0)
    head = [[(r[j] if j < len(r) else "").strip() for j in range(width)] for r in rows[:header_rows]]
    head = [r for r in head if not all(COL_NO.match(c) or not c for c in r)]  # drop "(1) (2) (3)" row
    # spanning header text ("Harga Berlaku" over 3 columns) carries to the right, except on the last row
    for r in head[:-1]:
        for j in range(1, width):
            if not r[j]:
                r[j] = r[j - 1]
    names = []
    for j in range(width):
        parts: list[str] = []
        for r in head:
            if r[j] and (not parts or parts[-1] != r[j]):
                parts.append(r[j])
        names.append(" / ".join(parts) or f"col_{j + 1}")
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


def page_label(t: dict) -> str:
    pages = sorted({t["page"], *(p["page"] for p in t.get("parts") or [])})
    return f"{pages[0]}–{pages[-1]}" if len(pages) > 1 else str(pages[0])


def table_json(t: dict, decimal: str = "auto") -> dict:
    rows, h = t["rows"], t.get("header_rows", 1)
    decimal = table_decimal(t, decimal)
    cols = column_names(rows, h)
    records = []
    for r in rows[h:]:
        rec = {}
        for j, name in enumerate(cols):
            v = r[j] if j < len(r) else ""
            n = to_number(v, decimal)
            rec[name] = n if n is not None else v
        records.append(rec)
    return {
        "id": t["id"], "document_id": t["document_id"], "page": t["page"], "title": t["title"],
        "notes": t.get("notes", ""), "status": t.get("status", "none"), "edited": t["edited"],
        "deleted": t.get("deleted", False), "engine": t["engine"], "bbox": t["bbox"], "header_rows": h,
        "merges": t.get("merges", []), "parts": t.get("parts", []), "decimal": decimal,
        "columns": cols, "rows": rows, "records": records,
    }


def csv_bytes(t: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerows(t["rows"])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM: Excel opens UTF-8 correctly


def _sheet_name(t: dict, used: set[str]) -> str:
    base = f"p{t['page']} " + (t["title"] or "table")
    base = re.sub(r"[\[\]:*?/\\]", " ", base).strip()[:28] or "table"
    name, k = base, 2
    while name.lower() in used:
        name = f"{base[:26]}~{k}"
        k += 1
    used.add(name.lower())
    return name


STATUS_LABEL = {"valid": "valid", "invalid": "not a table", "none": ""}
THIN = Side(style="thin", color="B8C2CC")
HEAD_FILL = PatternFill("solid", fgColor="E8EEF5")


def _write_sheet(ws, t: dict, numbers: bool, decimal: str, title: bool) -> None:
    """The table alone, from A1. With title=True: the title on top and the notes below it."""
    off = 0
    if title:
        ws.append([t["title"] or f"Table on page {page_label(t)}"])
        ws["A1"].font = Font(bold=True, size=12)
        ws.append([])
        off = 2
    h = t.get("header_rows", 1)
    decimal = table_decimal(t, decimal)
    widths: dict[int, int] = {}
    for i, r in enumerate(t["rows"]):
        out = []
        for v in r:
            n = to_number(v, decimal) if numbers and i >= h else None
            out.append(n if n is not None else v)
        ws.append(out)
        for j, v in enumerate(r, 1):
            c = ws.cell(row=off + i + 1, column=j)
            c.border = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
            if i < h:
                c.font = Font(bold=True)
                c.fill = HEAD_FILL
                c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
            elif isinstance(c.value, float):
                decimals = len(v.split(decimal)[-1]) if decimal in v else 0
                c.number_format = "#,##0" + ("." + "0" * decimals if decimals else "")
            elif isinstance(c.value, int):
                c.number_format = "#,##0"
            widths[j] = max(widths.get(j, 0), min(60, len(v) + 2))
    for r0, c0, rs, cs in t.get("merges", []):
        if rs > 1 or cs > 1:
            ws.merge_cells(start_row=off + r0 + 1, start_column=c0 + 1,
                           end_row=off + r0 + rs, end_column=c0 + cs)
            ws.cell(row=off + r0 + 1, column=c0 + 1).alignment = Alignment(
                wrap_text=True, vertical="center", horizontal="center" if r0 < h else "left")
    notes = (t.get("notes") or "").strip()
    if title and notes:
        ws.append([])
        for line in notes.splitlines():
            ws.append([line])
            ws.cell(row=ws.max_row, column=1).font = Font(italic=True, color="555555", size=9)
    for j, wdt in widths.items():
        ws.column_dimensions[get_column_letter(j)].width = max(8, wdt)
    if h:
        ws.freeze_panes = f"A{off + h + 1}"  # a coordinate: the cell may be inside a merge


INDEX_COLS = [("Sheet", 8), ("Title", 70), ("Document", 45), ("Category", 40), ("Page", 7), ("Rows", 7),
              ("Columns", 9), ("Flag", 11), ("Edited", 8), ("Notes", 60)]


def sheet_names(n: int) -> list[str]:
    """s001, s002, ... (wider numbers past 999)."""
    width = max(3, len(str(n)))
    return [f"s{i:0{width}d}" for i in range(1, n + 1)]


def xlsx_bytes(tables: list[dict], *, doc_title: str = "", doc_category: list[str] | None = None,
               numbers: bool = True, decimal: str = "auto", title: bool = False) -> bytes:
    """First sheet "Index" (one row per table, linked to its sheet), then one sheet per table: s001, s002, ...
    The table sheets hold only the table; title=True also writes the title above it and the notes below."""
    wb = Workbook()
    idx = wb.active
    idx.title = "Index"
    idx.append([c for c, _ in INDEX_COLS])
    for c in idx[1]:
        c.font = Font(bold=True)
        c.fill = HEAD_FILL
        c.border = Border(bottom=THIN)
    for (_, wdt), col in zip(INDEX_COLS, range(1, len(INDEX_COLS) + 1)):
        idx.column_dimensions[get_column_letter(col)].width = wdt
    idx.freeze_panes = "A2"
    for name, t in zip(sheet_names(len(tables)), tables):
        _write_sheet(wb.create_sheet(name), t, numbers, decimal, title)
        cat = t.get("document_category") if t.get("document_category") is not None else doc_category
        idx.append([name, t["title"] or f"Table on page {page_label(t)}", t.get("document_title") or doc_title,
                    " / ".join(cat or []), int(pl) if (pl := page_label(t)).isdigit() else pl, len(t["rows"]), max((len(r) for r in t["rows"]), default=0),
                    STATUS_LABEL.get(t.get("status", "none"), ""), "yes" if t["edited"] else "",
                    (t.get("notes") or "").replace("\n", " ")[:500]])
        link = idx.cell(row=idx.max_row, column=1)
        link.hyperlink = Hyperlink(ref=link.coordinate, location=f"'{name}'!A1", display=name)
        link.font = Font(color="1F5FAD", underline="single")
    if tables:
        idx.auto_filter.ref = f"A1:{get_column_letter(len(INDEX_COLS))}{len(tables) + 1}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def zip_csv_bytes(tables: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used: set[str] = set()
        for t in tables:
            name = _sheet_name(t, used)
            z.writestr(re.sub(r"\s+", "_", name) + ".csv", csv_bytes(t))
    return buf.getvalue()


def json_bytes(doc: dict | None, tables: list[dict], decimal: str = "auto") -> bytes:
    payload: dict = {"tables": [table_json(t, decimal) for t in tables]}
    if doc:
        payload["document"] = {k: doc[k] for k in ("id", "title", "filename", "source", "page_count")} | {
            "meta": doc.get("meta", {})}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def safe_filename(s: str, ext: str) -> str:
    s = re.sub(r"[^\w\-. ]+", "", s, flags=re.UNICODE).strip().replace(" ", "_")[:80] or "tables"
    return f"{s}.{ext}"
