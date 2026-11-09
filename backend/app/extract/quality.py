"""Grid clean-up and table checks that need no ground truth."""

from __future__ import annotations

import re

# BPS number formats: 1.298 · 12,80 · -0,32 · 1300 · 5,29% · "–"/"-"/"..." (no data)
NUM = re.compile(r"^[(\[]?[-+−–]?\d+(?:[.,]\d+)*%?[)\]]?\*{0,3}$")
DASH = re.compile(r"^[-–—−]+$|^\.{2,3}$|^x$|^NA$", re.I)
COL_NO = re.compile(r"^\(\d{1,2}\)$")  # BPS column numbering row: (1) (2) (3)


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ").replace("\u2212", "-")
    return re.sub(r"\s+", " ", s).strip()


def is_num(s: str) -> bool:
    s = norm(s)
    return bool(s) and (bool(NUM.match(s)) or bool(DASH.match(s)))


def is_value(s: str) -> bool:
    """A real number (not a dash placeholder)."""
    return bool(NUM.match(norm(s)))


Merge = list[int]  # [row, col, row_span, col_span] of the origin (top-left) cell


def clean_grid(rows: list[list[str]]) -> list[list[str]]:
    return clean_grid_merges(rows, [])[0]


def clean_grid_merges(rows: list[list[str]], merges: list[Merge]) -> tuple[list[list[str]], list[Merge]]:
    """Normalise text, drop empty rows/columns and remap merged-cell spans onto the kept grid."""
    rows = [[norm(c) for c in r] for r in rows]
    if not rows:
        return [], []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep_r = [i for i, r in enumerate(rows) if any(r)]
    keep_c = [j for j in range(width) if any(r[j] for r in rows)]
    if not keep_r:
        return [], []
    return [[rows[i][j] for j in keep_c] for i in keep_r], remap_merges(merges, keep_r, keep_c)


def remap_merges(merges: list[Merge], keep_r: list[int], keep_c: list[int]) -> list[Merge]:
    rpos = {i: n for n, i in enumerate(keep_r)}
    cpos = {j: n for n, j in enumerate(keep_c)}
    out: list[Merge] = []
    taken: set[tuple[int, int]] = set()
    for r, c, rs, cs in merges:
        rr = [rpos[i] for i in range(r, r + rs) if i in rpos]
        cc = [cpos[j] for j in range(c, c + cs) if j in cpos]
        if not rr or not cc:
            continue
        m = [rr[0], cc[0], rr[-1] - rr[0] + 1, cc[-1] - cc[0] + 1]
        cells = {(a, b) for a in range(m[0], m[0] + m[2]) for b in range(m[1], m[1] + m[3])}
        if (m[2] > 1 or m[3] > 1) and not cells & taken:
            out.append(m)
            taken |= cells
    return out


def filled(rows: list[list[str]]) -> int:
    return sum(1 for r in rows for c in r if c)


def numeric_rows(rows: list[list[str]]) -> int:
    return sum(1 for r in rows if sum(1 for c in r if is_num(c)) >= 1 and any(is_value(c) for c in r))


def looks_like_table(rows: list[list[str]]) -> bool:
    """Reject frames around paragraphs, chart bars, single-cell boxes."""
    if len(rows) < 2:
        return False
    ncols = max(len(r) for r in rows)
    if ncols < 2:
        return False
    cells = [c for r in rows for c in r if c]
    if len(cells) < 4:
        return False
    if max(len(c) for c in cells) > 400:
        return False  # a cell holding a whole paragraph: page frame
    leaders = sum(1 for r in rows if any("....." in c or "\u2026\u2026" in c for c in r))
    if leaders >= max(2, len(rows) // 3):
        return False  # table of contents / list of tables
    fill_ratio = len(cells) / (len(rows) * ncols)
    if fill_ratio < 0.3:
        return False
    # charts / infographics give sparse, staggered rows; real tables have full body rows
    need = max(2, int(0.6 * ncols + 0.999))
    full = sum(1 for r in rows if sum(1 for c in r if c) >= need)
    return full >= 2


def score(rows: list[list[str]]) -> float:
    """Rough usefulness: filled cells weighted by numeric content."""
    if not rows:
        return 0.0
    cells = [c for r in rows for c in r if c]
    nums = sum(1 for c in cells if is_num(c))
    return len(cells) + 2 * nums
