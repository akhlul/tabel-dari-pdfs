"""Ruled tables: pdfspine `find_tables` for the structure, our de-duplicated words for text."""

from __future__ import annotations

from .quality import clean_grid_merges
from .types import Rect, Table
from .words import Word, join_words, words_in


def _center_in(r: Rect, clip: Rect) -> bool:
    cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
    return clip[0] <= cx <= clip[2] and clip[1] <= cy <= clip[3]


def ruled_tables(page, words: list[Word], clip: Rect | None = None, strategy: str = "lines") -> list[Table]:
    kw = {"strategy": strategy}
    if clip:
        kw["clip"] = clip
    out: list[Table] = []
    for t in page.find_tables(**kw).tables:
        nrows, ncols = t.row_count, t.col_count
        if nrows == 0 or ncols == 0:
            continue
        grid = [["" for _ in range(ncols)] for _ in range(nrows)]
        merges: list[list[int]] = []
        try:
            for cell in t.origin_cells:
                r = tuple(cell.bbox)
                if clip and not _center_in(r, clip):
                    continue  # user drew a region inside a bigger grid: keep only that part
                grid[cell.row][cell.col] = join_words(words_in(words, r))
                if cell.row_span > 1 or cell.col_span > 1:
                    merges.append([cell.row, cell.col, cell.row_span, cell.col_span])
        except ValueError:
            # pdfspine rejects degenerate cells in some text-strategy grids: use its own text
            grid = [[c or "" for c in r] for r in t.extract()]
            merges = []
        rows, merges = clean_grid_merges(grid, merges)
        # a span is only meaningful when its origin holds text (empty spans are just layout)
        merges = [m for m in merges if rows[m[0]][m[1]]]
        bbox = tuple(float(v) for v in t.bbox)
        if clip:
            bbox = (max(bbox[0], clip[0]), max(bbox[1], clip[1]), min(bbox[2], clip[2]), min(bbox[3], clip[3]))
        out.append(Table(rows=rows, bbox=bbox, engine="lines", merges=merges, raw_rows=nrows))
    return out
