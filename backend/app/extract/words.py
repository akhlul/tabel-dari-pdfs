"""Word index built from pdfspine `rawdict` characters.

Two clean-ups the raw text layer needs for BPS documents:

* **Fake bold / shadow text** – many BPS PDFs draw the same glyph twice, offset by
  ~0.5 pt. A naive extractor reads "66..334422" instead of "6.342". We drop a glyph
  when the same character was already placed within `DUP_TOL` points.
* **Rotated text / watermarks** – only horizontal, left-to-right lines are kept.
"""

from __future__ import annotations

from dataclasses import dataclass

DUP_TOL = 1.2  # points: glyph shadows are usually offset by 0.3-0.6 pt


@dataclass(slots=True)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    size: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


class _GlyphGrid:
    """Spatial hash used to detect duplicated glyphs."""

    def __init__(self) -> None:
        self.cells: dict[tuple[str, int, int], list[tuple[float, float]]] = {}

    def seen(self, c: str, x: float, y: float) -> bool:
        gx, gy = int(x // DUP_TOL), int(y // DUP_TOL)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for px, py in self.cells.get((c, gx + dx, gy + dy), ()):
                    if abs(px - x) <= DUP_TOL and abs(py - y) <= DUP_TOL:
                        return True
        self.cells.setdefault((c, gx, gy), []).append((x, y))
        return False


def page_words(page) -> list[Word]:
    """Horizontal words of a pdfspine page, shadows removed, reading order."""
    raw = page.get_text("rawdict")
    grid = _GlyphGrid()
    # (baseline, x0, y0, x1, y1, char, size); spaces are kept as word separators
    glyphs: list[tuple[float, float, float, float, float, str, float]] = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            if abs(dy) > 0.05 or dx <= 0:
                continue
            for span in line.get("spans", []):
                size = float(span.get("size") or 10.0)
                for ch in span.get("chars", []):
                    c = ch.get("c")
                    if not c:
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    ox, oy = ch.get("origin", (x0, y1))
                    if not c.isspace() and grid.seen(c, ox, oy):
                        continue
                    glyphs.append((oy, x0, y0, x1, y1, c, size))
    return _glyph_words(glyphs)


def _glyph_words(glyphs: list) -> list[Word]:
    """Group glyphs by baseline, then split into words on spaces and gaps."""
    glyphs.sort(key=lambda g: (g[0], g[1]))
    rows: list[list] = []
    for g in glyphs:
        if rows and abs(rows[-1][0][0] - g[0]) <= max(0.8, 0.2 * g[6]):
            rows[-1].append(g)
        else:
            rows.append([g])
    words: list[Word] = []
    for row in rows:
        row.sort(key=lambda g: g[1])
        cur: list = []

        def flush() -> None:
            if cur:
                words.append(Word(min(g[1] for g in cur), min(g[2] for g in cur), max(g[3] for g in cur),
                                  max(g[4] for g in cur), "".join(g[5] for g in cur), cur[0][6]))
                cur.clear()

        for g in row:
            if g[5].isspace():
                flush()
                continue
            if cur and g[1] - cur[-1][3] > max(1.0, 0.25 * g[6]):
                flush()
            cur.append(g)
        flush()
    words.sort(key=lambda w: (round(w.cy, 0), w.x0))
    return words


def words_in(words: list[Word], rect: tuple[float, float, float, float], pad: float = 0.0) -> list[Word]:
    x0, y0, x1, y1 = rect
    return [w for w in words if x0 - pad <= w.cx <= x1 + pad and y0 - pad <= w.cy <= y1 + pad]


def join_words(words: list[Word]) -> str:
    """Join words into cell text: visual lines top-to-bottom, words left-to-right."""
    if not words:
        return ""
    ws = sorted(words, key=lambda w: (w.cy, w.x0))
    lines: list[list[Word]] = []
    for w in ws:
        if lines and abs(lines[-1][0].cy - w.cy) <= max(2.0, 0.4 * w.h):
            lines[-1].append(w)
        else:
            lines.append([w])
    parts = [" ".join(x.text for x in sorted(ln, key=lambda w: w.x0)) for ln in lines]
    text = " ".join(parts)
    # re-join words hyphenated across lines: "Peri- kanan" -> "Perikanan"
    return _dehyphen(text)


def _dehyphen(s: str) -> str:
    import re

    return re.sub(r"(\w)- (\w)", lambda m: m.group(1) + m.group(2) if m.group(2).islower() else m.group(0), s)
