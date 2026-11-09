"""Borderless ("stream") tables reconstructed from word positions.

BPS publications often rule only the header and leave the body open, so the
`lines` strategy finds a one-row table. This engine works from text alone:

1. words -> visual lines -> segments (runs of words separated by a column gap)
2. numeric lines (>= 2 segments, at least one number) seed table blocks
3. column bands = union of segment x-ranges over the numeric lines
4. header lines above the block are attached while they stay inside the columns
5. every segment is assigned to the band it overlaps most
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .quality import clean_grid, is_num, is_value
from .types import Rect, Table
from .words import Word, join_words

TITLE = re.compile(r"^(tabel|table|gambar|grafik|figure|lampiran)\b", re.I)
FOOTER = re.compile(r"^(sumber|source|catatan|note|keterangan|\*)", re.I)
# note lines under a table: also footnote markers ("¹Q-to-Q", "1) ...", "a) ...")
NOTE = re.compile(r"^(sumber|source|catatan|notes?|keterangan|ket\.|\*|[¹²³⁴⁵⁶⁷⁸⁹]|\d\)|[a-z]\))", re.I)
HEADERISH = re.compile(r"^(?:\(\d{1,2}\)|(?:19|20)\d\d\**)$")  # column numbers, years


@dataclass
class Seg:
    x0: float
    x1: float
    words: list[Word]

    @property
    def text(self) -> str:
        return join_words(self.words)


@dataclass
class Line:
    y0: float
    y1: float
    segs: list[Seg]
    size: float

    @property
    def x0(self) -> float:
        return self.segs[0].x0

    @property
    def x1(self) -> float:
        return self.segs[-1].x1

    @property
    def numeric(self) -> bool:
        """A body row: >= 2 segments with a real value (years / "(1) (2)" are header material)."""
        nums = [s for s in self.segs if is_num(s.text)]
        return len(self.segs) >= 2 and any(is_value(s.text) and not HEADERISH.match(s.text) for s in nums)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segs)


def _lines(words: list[Word]) -> list[Line]:
    ws = sorted(words, key=lambda w: (w.cy, w.x0))
    groups: list[list[Word]] = []
    for w in ws:
        if groups:
            g = groups[-1]
            gy = sum(x.cy for x in g) / len(g)
            if abs(gy - w.cy) <= 0.45 * min(w.h, g[0].h):
                g.append(w)
                continue
        groups.append([w])
    out: list[Line] = []
    for g in groups:
        g.sort(key=lambda w: w.x0)
        size = sorted(w.size for w in g)[len(g) // 2]
        gap = max(7.0, 0.9 * size)
        segs: list[Seg] = []
        for w in g:
            if segs and w.x0 - segs[-1].x1 <= gap:
                segs[-1].words.append(w)
                segs[-1].x1 = max(segs[-1].x1, w.x1)
            else:
                segs.append(Seg(w.x0, w.x1, [w]))
        out.append(Line(min(w.y0 for w in g), max(w.y1 for w in g), segs, size))
    return out


def _bands(lines: list[Line]) -> list[tuple[float, float]]:
    iv = sorted((s.x0, s.x1) for ln in lines for s in ln.segs)
    bands: list[list[float]] = []
    for a, b in iv:
        if bands and a <= bands[-1][1] + 1.5:
            bands[-1][1] = max(bands[-1][1], b)
        else:
            bands.append([a, b])
    return [(a, b) for a, b in bands]


def _assign(seg: Seg, bands: list[tuple[float, float]]) -> int:
    best, best_ov = 0, -1e9
    for i, (a, b) in enumerate(bands):
        ov = min(seg.x1, b) - max(seg.x0, a)
        if ov > best_ov:
            best, best_ov = i, ov
    if best_ov <= 0:  # no overlap: nearest band by center distance
        cx = (seg.x0 + seg.x1) / 2
        best = min(range(len(bands)), key=lambda i: abs((bands[i][0] + bands[i][1]) / 2 - cx))
    return best


def _blocks(lines: list[Line]) -> list[tuple[int, int]]:
    """Index ranges [start, end] of lines forming table bodies."""
    blocks: list[tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].numeric:
            i += 1
            continue
        start = end = i
        j = i + 1
        gap_lines = 0
        while j < n:
            ln, prev = lines[j], lines[j - 1]
            lh = max(prev.y1 - prev.y0, ln.y1 - ln.y0, 10.0)
            if ln.y0 - prev.y1 > 2.6 * lh or FOOTER.match(ln.text) or TITLE.match(ln.text):
                break
            if ln.numeric:
                end = j
                gap_lines = 0
            else:
                gap_lines += 1
                if gap_lines > 3:
                    break
            j += 1
        nums = sum(1 for k in range(start, end + 1) if lines[k].numeric)
        if nums >= 2 or (nums == 1 and _has_header_above(lines, start)):
            blocks.append((start, end))
        i = end + 1
    return blocks


def _has_header_above(lines: list[Line], start: int) -> bool:
    """A "(1) (2) (3)" numbering row or a year header just above: a one-row table is still a table."""
    for k in range(start - 1, max(-1, start - 4), -1):
        segs = lines[k].segs
        if len(segs) >= 3 and sum(1 for s in segs if HEADERISH.match(s.text)) >= len(segs) - 1:
            return lines[start].y0 - lines[k].y1 < 40
    return False


def _caption_end(lines: list[Line], start: int, floor: int) -> int:
    """Index of the last caption line ("Tabel 2.1" + centered continuation lines) above `start`, else floor."""
    for k in range(start - 1, max(floor, start - 9), -1):
        if TITLE.match(lines[k].text):
            end = k
            while end + 1 < start:
                nxt, prev = lines[end + 1], lines[end]
                tight = nxt.y0 - prev.y1 < 0.8 * max(prev.y1 - prev.y0, 8.0)  # caption lines are set solid
                if tight and len(nxt.segs) == 1 and not nxt.numeric and abs(nxt.cx - lines[k].cx) < 45:
                    end += 1
                else:
                    break
            return end
    return floor


def _header_start(lines: list[Line], start: int, x0: float, x1: float, floor: int) -> int:
    k = start
    width = x1 - x0
    floor = _caption_end(lines, start, floor)
    while k - 1 > floor and start - (k - 1) <= 6:
        ln, below = lines[k - 1], lines[k]
        lh = max(below.y1 - below.y0, 10.0)
        if below.y0 - ln.y1 > 2.2 * lh:
            break
        t = ln.text
        if TITLE.match(t) or FOOTER.match(t):
            break
        if ln.x0 < x0 - 15 or ln.x1 > x1 + 15:
            break
        if len(ln.segs) == 1 and (ln.x1 - ln.x0) > 0.6 * width and len(t) > 40:
            break  # a sentence or caption, not a header cell
        k -= 1
    return k


def stream_tables(words: list[Word], clip: Rect | None = None, exclude: list[Rect] | None = None) -> list[Table]:
    if exclude:
        words = [w for w in words if not any(r[0] - 2 <= w.cx <= r[2] + 2 and r[1] - 2 <= w.cy <= r[3] + 2 for r in exclude)]
    if clip:
        x0, y0, x1, y1 = clip
        words = [w for w in words if x0 <= w.cx <= x1 and y0 <= w.cy <= y1]
    lines = _lines(words)
    if not lines:
        return []

    if clip:  # user drew the region: treat everything inside as one table
        blocks = [(0, len(lines) - 1)]
    else:
        blocks = _blocks(lines)

    out: list[Table] = []
    floor = -1
    for start, end in blocks:
        body = lines[start : end + 1]
        num_lines = [ln for ln in body if ln.numeric] or [ln for ln in body if len(ln.segs) >= 2] or body
        bx0 = min(ln.x0 for ln in num_lines)
        bx1 = max(ln.x1 for ln in num_lines)
        hs = start if clip else _header_start(lines, start, bx0, bx1, floor)
        floor = end
        bands = _bands(num_lines)
        if len(bands) < 2 and not clip:
            continue
        rows: list[list[str]] = []
        kinds: list[bool] = []
        for ln in lines[hs : end + 1]:
            row: list[list[Word]] = [[] for _ in bands]
            for s in ln.segs:
                row[_assign(s, bands)].extend(s.words)
            rows.append([join_words(c) for c in row])
            kinds.append(ln.numeric)
        rows = _merge_wrapped(rows, kinds)
        grid = clean_grid(rows)
        if not grid:
            continue
        sel = lines[hs : end + 1]
        bbox = (
            min(ln.x0 for ln in sel),
            min(ln.y0 for ln in sel),
            max(ln.x1 for ln in sel),
            max(ln.y1 for ln in sel),
        )
        title = ""
        if hs > 0 and not clip:
            for k in range(hs - 1, max(-1, hs - 8), -1):
                if TITLE.match(lines[k].text):
                    cap_end = _caption_end(lines, hs, k - 1)
                    title = " ".join(ln.text for ln in lines[k : cap_end + 1])[:300]
                    break
        out.append(Table(rows=grid, bbox=bbox, engine="stream", title=title))
    return out


def _merge_wrapped(rows: list[list[str]], numeric: list[bool]) -> list[list[str]]:
    """Fold label-only continuation lines ("dan Daur Ulang") into the row above."""
    out: list[list[str]] = []
    kinds: list[bool] = []
    for row, num in zip(rows, numeric):
        only_label = bool(row[0]) and not any(row[1:])
        if out and only_label and not num and kinds[-1] and row[0][:1].islower():
            out[-1][0] = (out[-1][0] + " " + row[0]).strip()
            continue
        out.append(list(row))
        kinds.append(num)
    return out
