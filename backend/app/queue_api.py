"""The queue page: BPS imports (imports.py) and table searches (jobs.py), waiting, running and finished."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import auth, db, imports, jobs
from .common import get_doc

router = APIRouter(prefix="/api/queue")

STAGE = {"queued": "queued", "running": "extracting", "done": "done", "error": "error", "cancelled": "cancelled"}
GROUP = {"queued": "waiting", "downloading": "running", "processing": "running", "extracting": "running",
         "done": "finished", "error": "finished", "cancelled": "finished"}


def _today() -> float:
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _import_item(i: dict, pos: dict[str, int]) -> dict:
    return {
        "key": f"import:{i['id']}", "type": "import", "id": i["id"], "title": i["title"] or i["bps_id"],
        "source": "import", "kind": i["kind"], "cover": i["cover"], "document_id": i["document_id"],
        "status": i["status"], "position": pos.get(i["id"]), "bytes_done": i["done"], "bytes_total": i["total"],
        "pages_done": i.get("job_done"), "pages_total": i.get("job_total"), "found": i.get("job_found"),
        "error": i["error"], "created_by": i["created_by"], "created_at": i["created_at"],
        "started_at": i["started_at"], "finished_at": i["finished_at"], "job_id": i["job_id"],
        "source_key": i["source_key"],
    }


def _job_item(j: dict, first: set[str], pos: dict[str, int]) -> dict:
    return {
        "key": f"job:{j['id']}", "type": "job", "id": j["id"], "title": j["title"],
        "source": "upload" if j["id"] in first and j["doc_source"] == "upload" else "rerun",
        "kind": "upload" if j["doc_source"] == "upload" else "bps", "cover": None, "document_id": j["document_id"],
        "status": STAGE.get(j["status"], j["status"]), "position": pos.get(j["id"]), "bytes_done": None,
        "bytes_total": None, "pages_done": j["done"], "pages_total": j["total"], "found": j["found"],
        "error": j["error"], "created_by": j["created_by"], "created_at": j["created_at"],
        "started_at": j["started_at"], "finished_at": j["finished_at"], "job_id": j["id"], "pages": j["pages"],
    }


def _all(con) -> list[dict]:
    pos = imports.positions(con)
    imps = db.rows(con.execute(
        "SELECT i.*, j.done AS job_done, j.total AS job_total, j.found AS job_found FROM imports i "
        "LEFT JOIN jobs j ON j.id = i.job_id ORDER BY i.created_at DESC LIMIT 500"))
    linked = {r[0] for r in con.execute("SELECT job_id FROM imports WHERE job_id IS NOT NULL")}
    js = [j for j in db.rows(con.execute(
        "SELECT j.*, d.title, d.source AS doc_source FROM jobs j JOIN documents d ON d.id = j.document_id "
        "WHERE j.dismissed = 0 AND d.deleted_at IS NULL ORDER BY j.created_at DESC LIMIT 300"))
        if j["id"] not in linked]
    first = {r[0] for r in con.execute(
        "SELECT id FROM jobs j WHERE created_at = (SELECT MIN(created_at) FROM jobs k WHERE k.document_id = j.document_id)")}
    jpos = {r[0]: i for i, r in enumerate(con.execute(
        "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at"), len(pos) + 1)}
    return [_import_item(i, pos) for i in imps] + [_job_item(j, first, jpos) for j in js]


@router.get("")
def queue(status: Literal["active", "done", "error", "cancelled", "all"] = "all",
          source: Literal["", "import", "upload", "rerun"] = "", by: str = ""):
    with db.db() as con:
        items = _all(con)
        found_today = con.execute("SELECT COALESCE(SUM(found), 0) FROM jobs WHERE status='done' AND finished_at>=?",
                                  (_today(),)).fetchone()[0]
        paused = db.kv_get(con, "queue_paused") == "1"
    names = auth.user_names()
    for it in items:
        it["created_by_name"] = names.get(it["created_by"]) if it["created_by"] else None
        it["group"] = GROUP.get(it["status"], "finished")
    users = sorted({(it["created_by"], it["created_by_name"]) for it in items if it["created_by"]}, key=lambda u: u[1] or "")
    today = _today()
    counts = {
        "running": sum(1 for it in items if it["group"] == "running"),
        "waiting": sum(1 for it in items if it["group"] == "waiting"),
        "done_today": sum(1 for it in items if it["status"] == "done" and (it["finished_at"] or 0) >= today),
        "found_today": found_today,
        "active": sum(1 for it in items if it["group"] != "finished"),
        "done": sum(1 for it in items if it["status"] == "done"),
        "error": sum(1 for it in items if it["status"] == "error"),
        "cancelled": sum(1 for it in items if it["status"] == "cancelled"),
        "all": len(items),
    }
    if status == "active":
        items = [it for it in items if it["group"] != "finished"]
    elif status != "all":
        items = [it for it in items if it["status"] == status]
    if source:
        items = [it for it in items if it["source"] == source]
    if by:
        items = [it for it in items if it["created_by"] == by]
    order = {"running": 0, "waiting": 1, "finished": 2}
    items.sort(key=lambda it: (order[it["group"]], it["position"] or 0 if it["group"] == "waiting" else 0,
                               -(it["finished_at"] or it["started_at"] or it["created_at"] or 0)))
    return {"items": items, "counts": counts, "paused": paused,
            "users": [{"id": u, "name": n or "Unknown"} for u, n in users]}


@router.get("/count")
def count():
    """Waiting and running imports and table searches (the badge in the top bar)."""
    with db.db() as con:
        n = con.execute(f"SELECT COUNT(*) FROM imports WHERE {imports.ACTIVE_SQL}").fetchone()[0]
        n += con.execute(f"SELECT COUNT(*) FROM jobs WHERE {db.ACTIVE_JOB} AND id NOT IN "
                         f"(SELECT job_id FROM imports WHERE job_id IS NOT NULL)").fetchone()[0]
        paused = db.kv_get(con, "queue_paused") == "1"
    return {"active": n, "paused": paused}


class ImportItem(BaseModel):
    kind: Literal["publication", "pressrelease"]
    id: str = Field(min_length=1, max_length=64)
    domain: str = Field("0000", pattern=r"^\d{4}$")
    lang: Literal["ind", "eng"] = "ind"
    extract: bool = True
    engine: str = "auto"
    title: str = Field("", max_length=1000)
    cover: str | None = Field(None, max_length=1000)
    size: str | None = Field(None, max_length=40)


class Enqueue(BaseModel):
    items: list[ImportItem] = Field(min_length=1, max_length=200)


@router.post("/imports")
def add_imports(body: Enqueue):
    return {"results": imports.enqueue([i.model_dump() for i in body.items], auth.current_user_id())}


def _imp(iid: str) -> dict:
    imp = imports.get(iid)
    if not imp:
        raise HTTPException(404, "Import not found")
    return imp


@router.post("/imports/{iid}/cancel")
def cancel_import(iid: str):
    _imp(iid)
    return imports.cancel(iid)


@router.post("/imports/cancel")
def cancel_imports(body: dict):
    """Undo of "added to the queue": cancel several waiting imports at once."""
    for iid in body.get("ids", [])[:200]:
        imports.cancel(str(iid))
    return {"ok": True}


@router.post("/imports/{iid}/retry")
def retry_import(iid: str):
    _imp(iid)
    return imports.retry(iid)


class Move(BaseModel):
    to: Literal["up", "top"] = "up"


@router.post("/imports/{iid}/move")
def move_import(iid: str, body: Move):
    _imp(iid)
    imports.move(iid, body.to)
    return {"ok": True}


@router.delete("/imports/{iid}")
def remove_import(iid: str):
    _imp(iid)
    if not imports.remove(iid):
        raise HTTPException(409, "Only finished imports can be removed. Cancel it first.")
    return {"ok": True}


def _job(job_id: str) -> dict:
    with db.db() as con:
        j = db.row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    j = _job(job_id)
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, "This search is still running.")
    try:
        new = jobs.create_job(get_doc(j["document_id"]), j["engine"], j["pages"], by=auth.current_user_id())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    with db.db() as con:
        con.execute("UPDATE jobs SET dismissed=1 WHERE id=?", (job_id,))
    return new


@router.delete("/jobs/{job_id}")
def dismiss_job(job_id: str):
    j = _job(job_id)
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, "Only finished searches can be removed. Cancel it first.")
    with db.db() as con:
        con.execute("UPDATE jobs SET dismissed=1 WHERE id=?", (job_id,))
    return {"ok": True}


class Pause(BaseModel):
    paused: bool


@router.post("/pause")
def pause(body: Pause):
    """Paused: the running import finishes, the waiting ones wait."""
    imports.set_paused(body.paused)
    return {"paused": body.paused}


@router.post("/clear")
def clear(status: Literal["finished", "done"] = Query("finished")):
    """Remove finished imports and table searches from the list (documents and tables stay)."""
    st = ("done", "error", "cancelled") if status == "finished" else ("done",)
    marks = ",".join("?" * len(st))
    with db.db() as con:
        n = con.execute(f"DELETE FROM imports WHERE status IN ({marks})", st).rowcount
        n += con.execute(f"UPDATE jobs SET dismissed=1 WHERE dismissed=0 AND status IN ({marks})", st).rowcount
    return {"ok": True, "removed": n, "at": time.time()}
