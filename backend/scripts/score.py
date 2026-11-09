"""Score engines against hand-checked ground truth (same metrics as the original score.ts).

    uv run python scripts/score.py --gt ../groundtruth a.pdf b.pdf [--engines auto,lines] [--verbose]

Ground truth: <gt>/<pdf stem>/page-NN.json = {"tables": [{"title", "columns", "rows": [[label, v1, ...]]}]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pdfspine  # noqa: E402

from app.extract import ENGINES, extract_page  # noqa: E402

NUM = re.compile(r"^(?:-?\d+(?:\.\d{3})*(?:,\d+)?|-)$")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\u2212\u2013\u2014]", "-", s or "")).strip()


def is_num(s: str) -> bool:
    return bool(NUM.match(norm(s)))


def letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s.lower()))


def text_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", norm(s).lower()))


def label_key(label: str) -> str:
    return letters(re.sub(r"^\s*(?:[A-Z](?:,[A-Z])*|\d+)\.\s*", "", label))[:14]


def nums_match(got: list[str], want: list[str]) -> bool:
    return got == want or (bool(got) and bool(re.match(r"^\d{1,2}$", got[0])) and got[1:] == want)


def texts_match(cells: list[str], want: list[str]) -> bool:
    i = 0
    for c in cells:
        if i < len(want) and text_key(c) == want[i]:
            i += 1
    return i == len(want)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--gt", required=True)
    ap.add_argument("--engines", default="auto,lines,stream,text")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    engines = [e for e in a.engines.split(",") if e in ENGINES and ENGINES[e].available]
    tot = {e: dict(rows_ok=0, rows=0, vals_ok=0, vals=0, labels_ok=0, labels=0, fp=0, fp_pages=0, ms=0, errors=0) for e in engines}
    per_table = []
    for fi, f in enumerate(a.files):
        stem = Path(f).stem
        doc = pdfspine.open(f)
        for page in range(1, doc.page_count + 1):
            gt_file = Path(a.gt) / stem / f"page-{page:02d}.json"
            if not gt_file.exists():
                continue
            gt = json.loads(gt_file.read_text())["tables"]
            by_engine = {}
            for e in engines:
                res = extract_page(doc, page, e)
                acc = tot[e]
                acc["ms"] += res["ms"]
                if res.get("error"):
                    acc["errors"] += 1
                    if a.verbose:
                        print(f"  ERR {e} p{page}: {res['error']}", file=sys.stderr)
                by_engine[e] = [[[norm(c) for c in r] for r in t.rows] for t in res["tables"]]
                if not gt and res["tables"]:
                    acc["fp"] += len(res["tables"])
                    acc["fp_pages"] += 1
                    if a.verbose:
                        print(f"  FP {e} {stem[:10]} p{page}: {len(res['tables'])} tables", file=sys.stderr)
            for t in gt:
                scores = {}
                for e in engines:
                    acc, tables = tot[e], by_engine[e]
                    cands = []
                    for rows in tables:
                        for i, r in enumerate(rows):
                            near = [x for k in (i - 1, i, i + 1) if 0 <= k < len(rows) for x in rows[k] if not is_num(x)]
                            cands.append((
                                [c for c in r if is_num(c)],
                                [c for c in r if c and not is_num(c)],
                                letters(" ".join(near)),
                            ))
                    used: set[int] = set()
                    ok, missed = 0, []
                    for gr in t["rows"]:
                        values = [norm(v) for v in gr[1:]]
                        want = [v for v in values if is_num(v)]
                        want_text = [text_key(v) for v in values if not is_num(v)]
                        key = label_key(gr[0])
                        hit = next((i for i, c in enumerate(cands) if i not in used and nums_match(c[0], want)
                                    and texts_match(c[1], want_text) and key in c[2]), -1)
                        if hit >= 0:
                            used.add(hit)
                            ok += 1
                        else:
                            missed.append(gr[0][:18])
                    pool: dict[str, int] = {}
                    for rows in tables:
                        for r in rows:
                            for c in r:
                                if is_num(c):
                                    pool[c] = pool.get(c, 0) + 1
                    vals_ok = vals = 0
                    for gr in t["rows"]:
                        for v in gr[1:]:
                            v = norm(v)
                            if is_num(v):
                                vals += 1
                                if pool.get(v, 0) > 0:
                                    vals_ok += 1
                                    pool[v] -= 1
                    all_text = letters(" ".join(c for rows in tables for r in rows for c in r))
                    labels_ok = sum(1 for gr in t["rows"] if label_key(gr[0]) in all_text)
                    acc["rows_ok"] += ok
                    acc["rows"] += len(t["rows"])
                    acc["vals_ok"] += vals_ok
                    acc["vals"] += vals
                    acc["labels_ok"] += labels_ok
                    acc["labels"] += len(t["rows"])
                    scores[e] = f"{ok}/{len(t['rows'])}"
                    if a.verbose and missed:
                        print(f"  {e} {stem[:10]} p{page} {' '.join(t['title'].split()[:2])}: missed {'; '.join(missed)}", file=sys.stderr)
                per_table.append((f"file{fi + 1} p{page} {' '.join(t['title'].split()[:3])}", scores))
    w = 12
    print("table".ljust(34) + "".join(e.ljust(w) for e in engines))
    for name, sc in per_table:
        print(name[:33].ljust(34) + "".join(sc[e].ljust(w) for e in engines))
    pct = lambda x, y: f"{100 * x / y:.1f}%" if y else "-"  # noqa: E731
    print()
    for label, fn in [
        ("row exact", lambda c: pct(c["rows_ok"], c["rows"])),
        ("values in own cell", lambda c: pct(c["vals_ok"], c["vals"])),
        ("labels readable", lambda c: pct(c["labels_ok"], c["labels"])),
        ("false-pos tables (pages)", lambda c: f"{c['fp']} ({c['fp_pages']})"),
        ("errors", lambda c: str(c["errors"])),
        ("total time", lambda c: f"{c['ms']} ms"),
    ]:
        print(label.ljust(34) + "".join(fn(tot[e]).ljust(w) for e in engines))


if __name__ == "__main__":
    main()
