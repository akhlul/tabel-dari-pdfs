"""Import queue: BPS publications and press releases are downloaded, stored and searched for tables one at a
time, in the order of the queue. The queue lives in SQLite (table `imports`), so it survives a restart: a
download that was cut off starts again (see db.init).

Stages of an import: queued -> downloading -> processing (reading the PDF) -> extracting (finding tables)
-> done | error | cancelled.
"""

from __future__ import annotations

import threading
import time

from . import bps, categories, db, jobs
from .common import get_doc
from .documents import PdfError, store_pdf

ACTIVE = ("queued", "downloading", "processing", "extracting")
RUNNING = ("downloading", "processing", "extracting")
FINISHED = ("done", "error", "cancelled")
ACTIVE_SQL = "status IN ('queued','downloading','processing','extracting')"

_wake = threading.Event()
_cancel: set[str] = set()
_lock = threading.Lock()
_started = False


class Cancelled(Exception):
    pass


def key_of(kind: str, domain: str, bps_id: str) -> str:
    return f"bps:{kind}:{domain}:{bps_id}"


# ---------------------------------------------------------------- queue operations

def enqueue(items: list[dict], by: str | None) -> list[dict]:
    """Add BPS items ({kind, id, domain, lang, extract, engine, title?, cover?, size?}) to the end of the queue.
    Items already in the library come back as {"existing": True, "document_id"}, items already waiting or
    running as their import; nothing is added twice."""
    out = []
    with db.db() as con, db.tx(con):
        pos = con.execute("SELECT COALESCE(MAX(position), 0) FROM imports").fetchone()[0]
        for it in items:
            key = key_of(it["kind"], it["domain"], it["id"])
            doc = con.execute("SELECT id, deleted_at FROM documents WHERE source_key=?", (key,)).fetchone()
            if doc:
                if doc["deleted_at"]:  # deleted earlier (undo window): bring it back
                    con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (doc["id"],))
                out.append({"existing": True, "document_id": doc["id"], "source_key": key})
                continue
            cur = db.row(con.execute(f"SELECT * FROM imports WHERE source_key=? AND {ACTIVE_SQL}", (key,)).fetchone())
            if cur:
                out.append({"existing": False, "import": cur})
                continue
            pos += 1
            iid = db.new_id()
            con.execute(
                "INSERT INTO imports (id, source_key, kind, bps_id, domain, lang, extract, engine, title, cover, size, "
                "status, position, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                (iid, key, it["kind"], it["id"], it["domain"], it.get("lang") or "ind", int(it.get("extract", True)),
                 it.get("engine") or "auto", (it.get("title") or "")[:500], it.get("cover"), it.get("size"), pos, by,
                 time.time()))
            out.append({"existing": False, "import": get(iid, con)})
    _wake.set()
    return out


def get(iid: str, con=None) -> dict | None:
    if con is None:
        with db.db() as c:
            return get(iid, c)
    return db.row(con.execute("SELECT * FROM imports WHERE id=?", (iid,)).fetchone())


def cancel(iid: str) -> dict | None:
    """Waiting: cancelled right away. Running: stops at the next chunk / page (a running table search too)."""
    with db.db() as con:
        imp = get(iid, con)
        if not imp or imp["status"] in FINISHED:
            return imp
        if imp["status"] == "queued":
            con.execute("UPDATE imports SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                        (time.time(), iid))
        else:
            with _lock:
                _cancel.add(iid)
            if imp["status"] == "extracting" and imp["job_id"]:
                jobs.cancel(imp["job_id"])
        return get(iid, con)


def retry(iid: str) -> dict | None:
    with db.db() as con, db.tx(con):
        imp = get(iid, con)
        if not imp or imp["status"] not in ("error", "cancelled"):
            return imp
        if con.execute(f"SELECT 1 FROM imports WHERE source_key=? AND id!=? AND {ACTIVE_SQL}",
                       (imp["source_key"], iid)).fetchone():
            return imp  # the same item was added again meanwhile
        pos = con.execute("SELECT COALESCE(MAX(position), 0) FROM imports").fetchone()[0] + 1
        con.execute("UPDATE imports SET status='queued', position=?, done=0, total=NULL, error=NULL, job_id=NULL, "
                    "started_at=NULL, finished_at=NULL WHERE id=?", (pos, iid))
    _wake.set()
    return get(iid)


def move(iid: str, to: str) -> None:
    """to = 'up' (one place earlier) or 'top' (next to run)."""
    with db.db() as con, db.tx(con):
        imp = get(iid, con)
        if not imp or imp["status"] != "queued":
            return
        if to == "top":
            first = con.execute("SELECT MIN(position) FROM imports WHERE status='queued'").fetchone()[0]
            con.execute("UPDATE imports SET position=? WHERE id=?", (first - 1, iid))
            return
        prev = con.execute("SELECT id, position FROM imports WHERE status='queued' AND position<? "
                           "ORDER BY position DESC LIMIT 1", (imp["position"],)).fetchone()
        if prev:
            con.execute("UPDATE imports SET position=? WHERE id=?", (imp["position"], prev["id"]))
            con.execute("UPDATE imports SET position=? WHERE id=?", (prev["position"], iid))


def remove(iid: str) -> bool:
    """Remove a finished import from the list (the document it made stays in the library)."""
    with db.db() as con:
        return con.execute("DELETE FROM imports WHERE id=? AND status IN ('done','error','cancelled')",
                           (iid,)).rowcount > 0


def paused() -> bool:
    with db.db() as con:
        return db.kv_get(con, "queue_paused") == "1"


def set_paused(on: bool) -> None:
    with db.db() as con:
        db.kv_set(con, "queue_paused", "1" if on else "0")
    _wake.set()


def positions(con) -> dict[str, int]:
    """import id -> place in line (1 = next to run)."""
    return {r["id"]: i for i, r in enumerate(con.execute(
        "SELECT id FROM imports WHERE status='queued' ORDER BY position"), 1)}


# ---------------------------------------------------------------- worker (one import at a time)

def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="imports", daemon=True).start()


def _next() -> dict | None:
    with db.db() as con:
        if db.kv_get(con, "queue_paused") == "1":
            return None
        return db.row(con.execute("SELECT * FROM imports WHERE status='queued' ORDER BY position LIMIT 1").fetchone())


def _loop() -> None:
    while True:
        imp = _next()
        if not imp:
            _wake.wait(5)
            _wake.clear()
            continue
        try:
            _run(imp)
        except Exception as e:  # noqa: BLE001  never let the worker die
            _set(imp["id"], status="error", error=f"Import failed: {type(e).__name__}", finished_at=time.time())
        finally:
            with _lock:
                _cancel.discard(imp["id"])


def _set(iid: str, **cols) -> None:
    with db.db() as con:
        con.execute(f"UPDATE imports SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), iid))


def _cancelled(iid: str) -> bool:
    with _lock:
        return iid in _cancel


def _run(imp: dict) -> None:
    iid, by = imp["id"], imp["created_by"]
    with db.db() as con:  # claim it (a second worker would skip it)
        if con.execute("UPDATE imports SET status='downloading', started_at=?, done=0 WHERE id=? AND status='queued'",
                       (time.time(), iid)).rowcount == 0:
            return
    try:
        item = bps.detail(imp["kind"], imp["bps_id"], domain=imp["domain"], lang=imp["lang"])
        _set(iid, total=bps.parse_size(item.get("size")), title=item["title"] or imp["title"],
             cover=item.get("cover") or imp["cover"])
        last = [0.0]

        def progress(d: int, t: int | None) -> None:
            if time.time() - last[0] > 0.4:  # not one write per chunk
                last[0] = time.time()
                _set(iid, done=d, **({"total": t} if t else {}))

        data = bps.download(item["pdf"], progress=progress, cancelled=lambda: _cancelled(iid))
        _set(iid, status="processing", done=len(data), total=len(data))
        meta = item | {"domain": imp["domain"], "lang": imp["lang"]}
        doc = store_pdf(data, filename=f"{imp['kind']}-{imp['bps_id']}.pdf", title=item["title"], source="bps",
                        source_key=imp["source_key"], meta=meta, by=by)
        if doc.get("source_key") != imp["source_key"] and doc["source"] != "bps":
            # the same file was uploaded before: attach the BPS metadata to it
            with db.db() as con:
                con.execute("UPDATE documents SET source='bps', source_key=?, meta=?, title=?, category=CASE "
                            "WHEN category IS NULL OR category='[]' THEN ? ELSE category END WHERE id=?",
                            (imp["source_key"], db.dumps(meta), item["title"],
                             db.dumps(categories.for_bps(meta, item["title"])), doc["id"]))
            doc = get_doc(doc["id"])
        _set(iid, document_id=doc["id"])
        if _cancelled(iid):
            raise Cancelled
        if not imp["extract"]:
            _set(iid, status="done", finished_at=time.time())
            return
        try:  # the queue runs one import at a time, so the per-user job limit does not apply
            job = jobs.create_job(doc, imp["engine"], None, by=by, limit=False)
        except jobs.JobConflict as e:  # tables are already being found in this document: follow that job
            job = e.job
        if not job:
            _set(iid, status="done", finished_at=time.time())
            return
        _set(iid, status="extracting", job_id=job["id"])
        while True:  # wait for the table search, so the next import starts after it
            time.sleep(1)
            with db.db() as con:
                j = db.row(con.execute("SELECT status, error FROM jobs WHERE id=?", (job["id"],)).fetchone())
            if not j or j["status"] not in ("queued", "running"):
                break
            if _cancelled(iid):
                jobs.cancel(job["id"])
        if not j or j["status"] == "done":
            _set(iid, status="done", finished_at=time.time())
        elif j["status"] == "cancelled":
            _set(iid, status="cancelled", finished_at=time.time())
        else:
            _set(iid, status="error", error=f"Finding tables failed: {j['error']}", finished_at=time.time())
    except (bps.Cancelled, Cancelled):
        _set(iid, status="cancelled", finished_at=time.time())
    except (bps.BpsError, PdfError) as e:
        _set(iid, status="error", error=str(e), finished_at=time.time())
