"""Background extraction jobs (thread pool, progress persisted in SQLite)."""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .config import JOB_WORKERS
from .documents import open_pdf, pdf_path
from .extract import ENGINES, Table, extract_page
from .extract.quality import COL_NO, is_value

_pool = ThreadPoolExecutor(max_workers=JOB_WORKERS, thread_name_prefix="extract")
_cancel: set[str] = set()
_lock = threading.Lock()
YEAR = re.compile(r"^(?:19|20)\d\d\**$")


def guess_header_rows(rows: list[list[str]]) -> int:
    for i, r in enumerate(rows[:8]):
        cells = [c for c in r if c]
        if len(cells) >= 2 and sum(1 for c in cells if COL_NO.match(c)) >= len(cells) - 1:
            return i + 1  # BPS "(1) (2) (3)" numbering row closes the header
        values = [c for c in cells[1:] if is_value(c)]
        if values and not all(YEAR.match(c) or COL_NO.match(c) for c in values):
            return i  # first row carrying real numbers starts the body
    return 1 if len(rows) > 1 else 0


def _overlaps(a, b) -> bool:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])) or 1
    return w * h / small > 0.5


def save_tables(con, doc_id: str, page: int, tables: list[Table], *, job_id: str | None, origin: str,
                replace: bool) -> list[str]:
    now = time.time()
    kept = db.rows(con.execute(
        "SELECT id,bbox,edited,origin,status,deleted FROM tables WHERE document_id=? AND page=?", (doc_id, page)))
    touched = lambda k: k["edited"] or k["origin"] != "job" or k["status"] != "none" or k["deleted"]  # noqa: E731
    if replace:
        con.execute("DELETE FROM tables WHERE document_id=? AND page=? AND edited=0 AND origin='job' "
                    "AND status='none' AND deleted=0", (doc_id, page))
        kept = [k for k in kept if touched(k)]
    # tables the user edited, flagged or deleted are decisions: never re-create a table on top of them
    protected = [k["bbox"] for k in kept if touched(k)]
    start = len(kept)
    ids = []
    for i, t in enumerate(tables):
        if origin == "job" and any(_overlaps(t.bbox, b) for b in protected):
            continue
        tid = db.new_id()
        bbox = db.dumps(list(t.bbox))
        con.execute(
            "INSERT INTO tables (id,document_id,job_id,page,idx,bbox,original_bbox,engine,origin,title,notes,header_rows,"
            "rows,original_rows,merges,original_merges,edited,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (tid, doc_id, job_id, page, start + i, bbox, bbox, t.engine, origin, t.title, t.notes,
             guess_header_rows(t.rows), db.dumps(t.rows), db.dumps(t.rows), db.dumps(t.merges), db.dumps(t.merges),
             now, now),
        )
        ids.append(tid)
    return ids


def create_job(doc: dict, engine: str, pages: list[int] | None, replace: bool = True) -> dict:
    if engine not in ENGINES or not ENGINES[engine].available:
        raise ValueError(f"Engine not available: {engine}")
    if pages:
        pages = sorted({p for p in pages if 1 <= p <= doc["page_count"]})
        if not pages:
            raise ValueError("No valid pages selected")
    job_id = db.new_id()
    total = len(pages) if pages else doc["page_count"]
    with db.db() as con:
        con.execute(
            "INSERT INTO jobs (id,document_id,engine,pages,status,total,created_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, doc["id"], engine, db.dumps(pages) if pages else None, "queued", total, time.time()),
        )
        job = db.row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    _pool.submit(_run, job_id, doc["id"], engine, pages or list(range(1, doc["page_count"] + 1)), replace)
    return job  # type: ignore[return-value]


def cancel(job_id: str) -> None:
    with _lock:
        _cancel.add(job_id)


def _run(job_id: str, doc_id: str, engine: str, pages: list[int], replace: bool) -> None:
    with db.db() as con:
        con.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (time.time(), job_id))
        try:
            pdf = open_pdf(pdf_path(doc_id))
            found = 0
            errors: list[str] = []
            for n, page in enumerate(pages, 1):
                with _lock:
                    if job_id in _cancel:
                        _cancel.discard(job_id)
                        con.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (time.time(), job_id))
                        return
                res = extract_page(pdf, page, engine)
                if res.get("error"):
                    errors.append(f"p{page}: {res['error']}")
                con.execute("BEGIN")
                ids = save_tables(con, doc_id, page, res["tables"], job_id=job_id, origin="job", replace=replace)
                found += len(ids)
                con.execute("UPDATE jobs SET done=?, found=? WHERE id=?", (n, found, job_id))
                con.execute("COMMIT")
            con.execute(
                "UPDATE jobs SET status='done', finished_at=?, error=? WHERE id=?",
                (time.time(), "; ".join(errors[:5]) or None, job_id),
            )
            con.execute("UPDATE documents SET updated_at=? WHERE id=?", (time.time(), doc_id))
        except Exception as e:
            if con.in_transaction:
                con.execute("ROLLBACK")
            con.execute(
                "UPDATE jobs SET status='error', error=?, finished_at=? WHERE id=?",
                (f"{type(e).__name__}: {e}", time.time(), job_id),
            )
