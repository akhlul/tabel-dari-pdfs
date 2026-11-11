"""Table extraction engines (all built on pdfspine; no PyMuPDF).

Engines
-------
auto     ruled tables from `lines`, then borderless `stream` tables for the rest
lines    pdfspine `find_tables(strategy="lines")` structure + de-duplicated words
stream   borderless tables from word geometry only
text     pdfspine `find_tables(strategy="text")` (raw, for comparison)
tatr     pdfspine `find_tables(strategy="vision", backend="tatr")` - optional, later
"""

from __future__ import annotations

import importlib.util
import re
import time
from dataclasses import dataclass
from typing import Callable

from .lines import ruled_tables
from .quality import has_leader, is_value, looks_like_table, prose_like
from .stream import NOTE, TITLE, _lines, stream_tables
from .types import Rect, Table
from .words import Word, page_words


@dataclass
class Engine:
    id: str
    label: str
    description: str
    run: Callable[[object, list[Word], Rect | None], list[Table]]
    available: bool = True
    note: str = ""


def _overlap(a: Rect, b: Rect) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    area = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1.0
    return w * h / area


def _run_lines(page, words, clip):
    return [t for t in ruled_tables(page, words, clip) if looks_like_table(t.rows)]


def _run_stream(page, words, clip):
    return [t for t in stream_tables(words, clip) if clip or (looks_like_table(t.rows) and not prose_like(t.rows))]


def _run_text(page, words, clip):
    out = ruled_tables(page, words, clip, strategy="text")
    for t in out:
        t.engine = "text"
    return [t for t in out if clip or looks_like_table(t.rows)]


def _under_segmented(t: Table) -> bool:
    """A ruled frame around an open body: one cell swallowed many numbers."""
    return any(sum(1 for tok in c.split() if is_value(tok)) >= 4 for r in t.rows for c in r)


def _resegment(t: Table, words) -> Table:
    inner = stream_tables(words, t.bbox)
    if inner and len(inner[0].rows) > len(t.rows):
        inner[0].engine = "lines+stream"
        inner[0].bbox = t.bbox
        return inner[0]
    return t


def _run_auto(page, words, clip):
    ruled = [t for t in ruled_tables(page, words, clip) if looks_like_table(t.rows) and len(t.rows) >= 3]
    ruled = [_resegment(t, words) if _under_segmented(t) else t for t in ruled]
    if clip:
        if ruled:
            return ruled
        return stream_tables(words, clip)
    streams = [t for t in stream_tables(words, None, exclude=[t.bbox for t in ruled])
               if looks_like_table(t.rows) and not prose_like(t.rows)]
    streams = [s for s in streams if not any(_overlap(s.bbox, r.bbox) > 0.3 for r in ruled)]
    return ruled + streams


def _run_tatr(page, words, clip):
    kw = {"strategy": "vision", "backend": "tatr"}
    if clip:
        kw["clip"] = clip
    out = []
    for t in page.find_tables(**kw).tables:
        rows = [[c or "" for c in r] for r in t.extract()]
        out.append(Table(rows=rows, bbox=tuple(float(v) for v in t.bbox), engine="tatr"))
    return out


# pdfspine[tatr] pulls torch + transformers; models are fetched separately (see README)
_tatr_ok = all(importlib.util.find_spec(m) is not None for m in ("torch", "transformers"))

ENGINES: dict[str, Engine] = {
    e.id: e
    for e in [
        Engine("auto", "Automatic", "Ruled tables first, then tables without grid lines", _run_auto),
        Engine("lines", "Ruled Tables", "Tables drawn with grid lines", _run_lines),
        Engine("stream", "Borderless Tables", "Columns found from text alignment, for tables without grid lines", _run_stream),
        Engine("text", "Text Layout (Raw)", "pdfspine text strategy without clean-up, for comparison", _run_text),
        Engine(
            "tatr",
            "Table Transformer (AI)",
            "Vision model that recognises table structure from the page image",
            _run_tatr,
            available=_tatr_ok,
            note="" if _tatr_ok else 'Not installed. Run uv add "pdfspine[tatr]" in backend/ to enable it',
        ),
    ]
}


# a caption line starts with the keyword, optionally after "Lanjutan" / "Continued" (continuation pages)
CAPTION = re.compile(r"^(?:(?:Lanjutan|LANJUTAN|Continued|CONTINUED)\b[^\d]{0,40}?)?"
                     r"(?:Tabel|TABEL|Table|TABLE|Lampiran|LAMPIRAN|Appendix)\b")
CAPTION_NO = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.?|[A-Z]\.?)$")


def _caption_block(t: Table, words: list[Word]) -> str:
    """Multi-line caption right above the table. Handles the BPS layout where the label sits in its own
    column, left of the title text ("Tabel"/"Table" stacked over "1.1.1"; Indonesian + English lines):

        Tabel   1.1.1   Luas Daerah ... Menurut Kabupaten/Kota
        Table           di Provinsi Lampung, 2025
                        Total Area ... in Lampung Province, 2025
    """
    x0, y0, x1, _ = t.bbox
    near = [w for w in words if y0 - 140 <= w.cy < y0 and w.x1 > x0 - 40 and w.x0 < x1 + 40]
    lines = _lines(near)
    if not lines or y0 - lines[-1].y1 > 60:
        return ""
    # the contiguous block of lines ending at the table; its topmost caption line starts the caption
    start = None
    for k in range(len(lines) - 1, -1, -1):
        ln = lines[k]
        if k < len(lines) - 1:
            lh = max(ln.y1 - ln.y0, 8.0)
            if lines[k + 1].y0 - ln.y1 > 2.2 * lh:
                break
        if CAPTION.match(ln.text):
            start = k
    if start is None:
        return ""
    head = lines[start]
    cap = lines[start:]
    first = head.segs[0].text
    if len(head.segs) >= 2 and len(first) <= 40 and not re.search(r"\d", first.split()[-1]) \
            and head.segs[1].x0 - head.segs[0].x1 > 6:
        # label column: everything left of the title text column
        text_x = min((s.x0 for ln in cap for s in ln.segs if s.x0 >= head.segs[1].x0 - 3), default=head.segs[1].x0)
        text_x = min(text_x, head.segs[1].x0)
        label = [w for ln in cap for s in ln.segs if s.x1 < text_x - 2 for w in s.words]
        no = next((w.text for w in sorted(label, key=lambda w: (w.cy, w.x0)) if CAPTION_NO.match(w.text)), "")
        if not no:
            return ""
        body = [" ".join(s.text for s in ln.segs if s.x0 >= text_x - 3) for ln in cap]
        return f"{first} {no} {' '.join(b for b in body if b)}".strip()[:300]
    text = " ".join(ln.text for ln in cap)
    # needs a number after the keyword ("Tabel 4.", "Lampiran 1.10"), not "... tabel di bawah ini:"
    if not re.search(r"\b(?:Tabel|TABEL|Table|TABLE|Lampiran|LAMPIRAN|Appendix)\s*(?:\d|[IVX]+\b|[A-Z]\b)", text):
        return ""
    return text[:300]


def _title_for(t: Table, words: list[Word]) -> str:
    """Caption text ("Tabel 3.1 ...") above the table."""
    if t.title:
        return t.title
    cap = _caption_block(t, words)
    if cap:
        return cap
    above = [w for w in words if t.bbox[1] - 60 <= w.cy < t.bbox[1] and w.x1 > t.bbox[0] - 20 and w.x0 < t.bbox[2] + 20]
    if not above:
        return ""
    lines = _lines(above)
    text = " ".join(ln.text for ln in lines)
    # caption = capitalised keyword + number ("Tabel 4.", "Lampiran 1.10"); not "... tabel di bawah ini:"
    m = re.search(r"\b(?:Tabel|TABEL|Table|TABLE|Lampiran|LAMPIRAN|Appendix)\s+(?:\d|[IVX]+\b|[A-Z]\b)[\w.]*.*", text)
    return (m.group(0) if m else "")[:300]


def _notes_for(t: Table, words: list[Word]) -> str:
    """'Sumber : ...' / 'Catatan: ...' / footnote lines printed right under the table."""
    x0, _, x1, y1 = t.bbox
    below = [w for w in words if y1 - 2 < w.cy < y1 + 160 and w.x1 > x0 - 40 and w.x0 < x1 + 40]
    lines = _lines(below)
    out: list[str] = []
    prev_y1 = y1
    for i, ln in enumerate(lines):
        lh = max(ln.y1 - ln.y0, 8.0)
        gap = ln.y0 - prev_y1
        if not out:
            if gap > 3 * lh or i > 1:
                break
            if not NOTE.match(ln.text):
                continue  # one stray line (e.g. a total row label) may sit between table and note
        elif gap > 1.6 * lh:
            break
        elif not NOTE.match(ln.text) and gap > 0.9 * lh:
            break  # next paragraph of body text
        out.append(ln.text)
        prev_y1 = ln.y1
        if len(out) >= 10:
            break
    return "\n".join(out)[:2000]


def _is_toc(words: list[Word]) -> bool:
    """Table of contents / list of tables: many lines ending in dot leaders + page numbers."""
    lines = {round(w.cy / 4) for w in words if has_leader(w.text)}
    return len(lines) >= 4


def _is_graphic(page) -> bool:
    """Infographic page (chapter openers): the artwork is cut into hundreds of image tiles, and the
    numbers printed over it line up like a borderless table. Real table pages have a handful at most."""
    try:
        return len(page.get_images()) >= GRAPHIC_IMAGES
    except Exception:
        return False


GRAPHIC_IMAGES = 50


def extract_page(doc, page_no: int, engine_id: str = "auto", clip: Rect | None = None) -> dict:
    """Run one engine on one page (1-based). Returns {tables, ms, error?}."""
    engine = ENGINES[engine_id]
    t0 = time.perf_counter()
    try:
        page = doc[page_no - 1]
        words = page_words(page)
        tables = engine.run(page, words, clip)
        if clip is None:  # whole-page runs only: a drawn area is always extracted
            if _is_toc(words):
                tables = []
            elif any(t.engine.endswith("stream") for t in tables) and _is_graphic(page):
                tables = [t for t in tables if not t.engine.endswith("stream")]
        for t in tables:
            t.title = _title_for(t, words)
            t.notes = _notes_for(t, words)
        tables.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
        return {"tables": tables, "ms": round((time.perf_counter() - t0) * 1000)}
    except Exception as e:  # engine errors are data, not crashes
        return {"tables": [], "ms": round((time.perf_counter() - t0) * 1000), "error": f"{type(e).__name__}: {e}"}


def list_engines() -> list[dict]:
    return [
        {"id": e.id, "label": e.label, "description": e.description, "available": e.available, "note": e.note}
        for e in ENGINES.values()
    ]


__all__ = ["ENGINES", "Table", "extract_page", "list_engines", "TITLE"]
