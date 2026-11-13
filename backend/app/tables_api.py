"""Tables: per-document CRUD, flags (valid / invalid), soft delete, version history,
region move/resize + re-extract, a cross-document search with facets, and exports."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Literal

import pdfspine
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import auth, db, exporters, jobs
from .common import (MEDIA, PUBLIC_DOC_SQL, PUBLIC_TABLE_SQL, doc_kind, download, get_doc, get_doc_for,
                     get_table, get_table_for, table_is_public)
from .documents import doc_dir, open_pdf, pdf_path
from .extract import ENGINES, Table, extract_page
from .extract.quality import remap_merges

router = APIRouter(prefix="/api")

STATES = ("none", "valid", "invalid", "deleted")
VERSION_GAP_S = 20  # autosaves closer than this collapse into one history entry
MAX_VERSIONS = 100


def state_of(t: dict) -> str:
    return "deleted" if t["deleted"] else t["status"]


def is_edited(rows, merges, t: dict) -> bool:
    return rows != t["original_rows"] or _norm_merges(merges) != _norm_merges(t["original_merges"])


def _norm_merges(ms) -> list:
    return sorted([list(m) for m in ms if m[2] > 1 or m[3] > 1])


def _snapshot(con, t: dict, reason: str) -> int:
    """Store the current content (and area) of a table; returns the version id (used for Undo).
    created_by = who made that content (the last editor of the table)."""
    vid = con.execute(
        "INSERT INTO table_versions (table_id, rows, merges, header_rows, bbox, orig, reason, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (t["id"], db.dumps(t["rows"]), db.dumps(t["merges"]), t["header_rows"], db.dumps(t["bbox"]),
         db.dumps({"original_rows": t["original_rows"], "original_merges": t["original_merges"], "engine": t["engine"]}),
         reason, time.time(), t.get("updated_by")),
    ).lastrowid
    con.execute(
        "DELETE FROM table_versions WHERE table_id=? AND id NOT IN "
        "(SELECT id FROM table_versions WHERE table_id=? ORDER BY id DESC LIMIT ?)",
        (t["id"], t["id"], MAX_VERSIONS),
    )
    return vid


def _touch_doc(con, doc_id: str) -> None:
    con.execute("UPDATE documents SET updated_at=? WHERE id=?", (time.time(), doc_id))


def _me() -> str | None:
    return auth.current_user_id()


def _fresh(con, table_id: str) -> dict:
    """The table as it is now, read inside a write transaction (db.tx)."""
    t = db.row(con.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone())
    if not t:
        raise HTTPException(404, "Table not found")
    return t


AUDIT = ("created_by", "updated_by", "validated_by")


def _audit(items: list[dict], request: Request) -> list[dict]:
    """Staff see who created / changed a table; visitors do not see user ids or names."""
    if auth.is_public(request):
        for it in items:
            for k in AUDIT:
                it.pop(k, None)
            it.pop("validated_at", None)
        return items
    names = auth.user_names()
    for it in items:
        auth.with_names(it, names)
    return items


def _clean_merges(merges: list[list[int]], nrows: int, ncols: int) -> list[list[int]]:
    """Clip to the grid, drop 1x1 and overlapping spans."""
    out, taken = [], set()
    for m in merges:
        if len(m) != 4:
            continue
        r, c, rs, cs = (int(v) for v in m)
        rs, cs = min(rs, nrows - r), min(cs, ncols - c)
        if r < 0 or c < 0 or rs < 1 or cs < 1 or (rs == 1 and cs == 1):
            continue
        cells = {(a, b) for a in range(r, r + rs) for b in range(c, c + cs)}
        if cells & taken:
            continue
        taken |= cells
        out.append([r, c, rs, cs])
    return out


# ---------------------------------------------------------------- per document

@router.get("/documents/{doc_id}/tables")
def list_doc_tables(doc_id: str, request: Request):
    get_doc_for(request, doc_id)
    only = f" AND {PUBLIC_TABLE_SQL}" if auth.is_public(request) else ""
    with db.db() as con:
        return _audit(db.rows(con.execute(
            f"SELECT * FROM tables t WHERE document_id=? AND joined_into IS NULL{only} ORDER BY page, idx, created_at",
            (doc_id,))), request)


class TableCreate(BaseModel):
    page: int = Field(ge=1)
    title: str = ""
    rows: list[list[str]] = Field(default_factory=lambda: [["", ""], ["", ""]])
    bbox: list[float] = Field(default_factory=lambda: [0, 0, 0, 0], min_length=4, max_length=4)


@router.post("/documents/{doc_id}/tables")
def create_table(doc_id: str, body: TableCreate):
    get_doc(doc_id)
    t = Table(rows=body.rows, bbox=tuple(body.bbox), engine="manual", title=body.title)
    with db.db() as con:
        ids = jobs.save_tables(con, doc_id, body.page, [t], job_id=None, origin="manual", replace=False, by=_me())
    return get_table(ids[0])


# ---------------------------------------------------------------- cross-document search

def _all_light(con, public: bool = False) -> list[dict]:
    rs = con.execute(
        """SELECT t.id, t.document_id, t.page, t.idx, t.title, t.notes, t.status, t.deleted, t.edited,
                  t.updated_at, t.validated_by, t.validated_at, d.title AS doc_title, d.source AS doc_source, d.meta AS doc_meta,
                  d.category AS doc_category
           FROM tables t JOIN documents d ON d.id = t.document_id
           WHERE t.joined_into IS NULL AND d.deleted_at IS NULL""" + (f" AND {PUBLIC_TABLE_SQL}" if public else "")
    ).fetchall()
    out = []
    for r in rs:
        d = dict(r)
        d["deleted"] = bool(d["deleted"])
        d["edited"] = bool(d["edited"])
        meta = json.loads(d.pop("doc_meta") or "{}")
        d["doc_category"] = json.loads(d["doc_category"] or "[]")
        d["kind"] = meta.get("kind", "publication") if d["doc_source"] == "bps" else "upload"
        out.append(d)
    return out


def _csv(s: str | None) -> list[str]:
    return [x for x in (s or "").split(",") if x]


@router.get("/tables")
def search_tables(
    request: Request,
    state: str = Query("valid", description="csv of none,valid,invalid,deleted; empty = all states"),
    edited: bool | None = None,
    docs: str = "",
    kinds: str = "",
    q: str = "",
    validator: str = Query("", description="csv of user ids; 'none' = no validator recorded"),
    sort: Literal["recent", "document", "validated"] = "document",
    offset: int = Query(0, ge=0),
    limit: int = Query(30, ge=1, le=200),
):
    public = auth.is_public(request)
    if public:  # visitors who are not logged in: only tables flagged valid
        state = "valid"
    want_state, want_docs, want_kinds = set(_csv(state)), set(_csv(docs)), set(_csv(kinds))
    want_val = set() if public else set(_csv(validator))
    ql = q.strip().lower()
    with db.db() as con:
        light = _all_light(con, public)

    def ok(t: dict, skip: str = "") -> bool:
        if skip != "state" and want_state and state_of(t) not in want_state:
            return False
        if skip != "edited" and edited is not None and t["edited"] != edited:
            return False
        if skip != "docs" and want_docs and t["document_id"] not in want_docs:
            return False
        if skip != "kinds" and want_kinds and t["kind"] not in want_kinds:
            return False
        if skip != "validator" and want_val and (t["validated_by"] or "none") not in want_val:
            return False
        if ql and ql not in f"{t['title']} {t['notes']} {t['doc_title']}".lower():
            return False
        return True

    # facet counts ignore their own filter, so the UI can show what each option would add
    facets = {
        "state": {s: sum(1 for t in light if ok(t, "state") and state_of(t) == s) for s in STATES},
        "edited": sum(1 for t in light if ok(t, "edited") and t["edited"]),
        "kinds": {},
        "docs": {},
        "validators": [],
    }
    if not public:  # who flagged the tables (names only for staff)
        names = auth.user_names()
        by: dict[str, int] = {}
        for t in light:
            if ok(t, "validator") and t["status"] != "none":
                k = t["validated_by"] or "none"
                by[k] = by.get(k, 0) + 1
        facets["validators"] = sorted(
            ({"id": k, "name": names.get(k, "Unknown") if k != "none" else "Not recorded", "count": n}
             for k, n in by.items()), key=lambda v: (v["id"] == "none", -v["count"]))
    for t in light:
        if ok(t, "kinds"):
            facets["kinds"][t["kind"]] = facets["kinds"].get(t["kind"], 0) + 1
        if ok(t, "docs"):
            f = facets["docs"].setdefault(t["document_id"], {"id": t["document_id"], "title": t["doc_title"],
                                                             "kind": t["kind"], "category": t["doc_category"],
                                                             "count": 0})
            f["count"] += 1
    with db.db() as con:  # documents with no matching table still appear in the source list
        for d in db.rows(con.execute("SELECT id, title, source, meta, category FROM documents d WHERE "
                                     + (PUBLIC_DOC_SQL if public else "deleted_at IS NULL"))):
            facets["docs"].setdefault(d["id"], {"id": d["id"], "title": d["title"], "kind": doc_kind(d),
                                                "category": d["category"] or [], "count": 0})
    facets["docs"] = sorted(facets["docs"].values(), key=lambda f: (-f["count"], f["title"]))

    hits = [t for t in light if ok(t)]
    if sort == "recent":
        hits.sort(key=lambda t: -t["updated_at"])
    elif sort == "validated":  # most recently flagged first, unflagged last
        hits.sort(key=lambda t: -(t["validated_at"] or 0))
    else:
        hits.sort(key=lambda t: (t["doc_title"].lower(), t["document_id"], t["page"], t["idx"]))
    page_ids = [t["id"] for t in hits[offset: offset + limit]]
    items = []
    if page_ids:
        with db.db() as con:
            full = {t["id"]: t for t in db.rows(con.execute(
                f"SELECT * FROM tables WHERE id IN ({','.join('?' * len(page_ids))})", page_ids))}
            docs_by = {d["id"]: d for d in db.rows(con.execute(
                "SELECT id, title, source, meta, page_count, pages FROM documents"))}
        for tid in page_ids:
            items.append(_with_ref(full[tid], docs_by[full[tid]["document_id"]]))
    _audit(items, request)
    return {"total": len(hits), "offset": offset, "limit": limit, "items": items, "facets": facets}


def _with_ref(t: dict, d: dict) -> dict:
    pw, ph = d["pages"][t["page"] - 1] if t["page"] <= len(d["pages"]) else (595, 842)
    meta = d.get("meta") or {}
    t = dict(t)
    t["state"] = state_of(t)
    t["pages"] = sorted({t["page"], *(p["page"] for p in t.get("parts") or [])})
    t["document"] = {
        "id": d["id"], "title": d["title"], "kind": doc_kind(d), "page_count": d["page_count"],
        "page_size": [pw, ph], "source_pdf": meta.get("pdf"), "date": meta.get("date"),
        "bps_id": meta.get("id"), "domain": meta.get("domain"), "category": d.get("category") or [],
    }
    return t


@router.get("/tables/{table_id}")
def get_one_route(table_id: str, request: Request):
    get_table_for(request, table_id)
    return _audit([get_one(table_id)], request)[0]


def get_one(table_id: str) -> dict:
    """The table with its document reference and audit names (for staff: used after every write)."""
    t = get_table(table_id)
    with db.db() as con:
        d = db.row(con.execute("SELECT * FROM documents WHERE id=?", (t["document_id"],)).fetchone())
    return auth.with_names(_with_ref(t, d))  # type: ignore[arg-type]


# ---------------------------------------------------------------- bulk + cross-doc export

class Bulk(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    action: Literal["valid", "invalid", "none", "delete", "restore", "purge"]


@router.post("/tables/bulk")
def bulk(body: Bulk, request: Request):
    if body.action == "purge":
        auth.need(request, "admin")
    marks = ",".join("?" * len(body.ids))
    now, me = time.time(), _me()
    with db.db() as con, db.tx(con):
        if body.action in ("valid", "invalid"):  # the validator changes only where the flag changes
            n = con.execute(f"UPDATE tables SET validated_by=CASE WHEN status=? THEN validated_by ELSE ? END, "
                            f"validated_at=CASE WHEN status=? THEN validated_at ELSE ? END, "
                            f"status=?, updated_at=?, updated_by=? WHERE id IN ({marks})",
                            (body.action, me, body.action, now, body.action, now, me, *body.ids)).rowcount
        elif body.action == "none":
            n = con.execute(f"UPDATE tables SET status='none', validated_by=NULL, validated_at=NULL, updated_at=?, "
                            f"updated_by=? WHERE id IN ({marks})", (now, me, *body.ids)).rowcount
        elif body.action == "delete":
            n = con.execute(f"UPDATE tables SET deleted=1, deleted_at=?, updated_at=?, updated_by=? WHERE id IN ({marks})",
                            (now, now, me, *body.ids)).rowcount
        elif body.action == "restore":
            n = con.execute(f"UPDATE tables SET deleted=0, deleted_at=NULL, updated_at=?, updated_by=? "
                            f"WHERE id IN ({marks})", (now, me, *body.ids)).rowcount
        else:
            con.execute(f"DELETE FROM tables WHERE joined_into IN ({marks})", body.ids)
            n = con.execute(f"DELETE FROM tables WHERE id IN ({marks})", body.ids).rowcount
    return {"ok": True, "changed": n}


def _tables_by_ids(ids: list[str], public: bool = False) -> list[dict]:
    if not ids:
        return []
    with db.db() as con:
        ts = db.rows(con.execute(f"SELECT t.*, d.title AS document_title, d.category AS document_category "
                                 f"FROM tables t JOIN documents d "
                                 f"ON d.id=t.document_id WHERE t.id IN ({','.join('?' * len(ids))})"
                                 + (f" AND {PUBLIC_TABLE_SQL} AND d.deleted_at IS NULL" if public else ""), ids))
    order = {i: n for n, i in enumerate(ids)}
    return sorted(ts, key=lambda t: order[t["id"]])


@router.get("/tables-export")
def export_many(request: Request, ids: str, format: Literal["xlsx", "json", "zip"] = "xlsx", numbers: bool = True,
                decimal: Literal["auto", ",", "."] = "auto", title: bool = False):
    ts = _tables_by_ids(_csv(ids), auth.is_public(request))
    if not ts:
        raise HTTPException(400, "No tables selected")
    if format == "xlsx":
        data = exporters.xlsx_bytes(ts, numbers=numbers, decimal=decimal, title=title)
    elif format == "json":
        data = exporters.json_bytes(None, ts, decimal)
    else:
        data = exporters.zip_csv_bytes(ts)
    return download(data, f"tabel-{len(ts)}-tables.{format}", MEDIA[format])


@router.get("/documents/{doc_id}/export")
def export_document(request: Request, doc_id: str, format: Literal["xlsx", "json", "zip"] = "xlsx", ids: str = "",
                    include: Literal["usable", "valid", "all"] = "usable", numbers: bool = True,
                    decimal: Literal["auto", ",", "."] = "auto", title: bool = False):
    """usable = everything except deleted and 'not a table'; valid = only tables flagged valid.
    Visitors who are not logged in always get only the valid tables."""
    d = get_doc_for(request, doc_id)
    with db.db() as con:
        ts = db.rows(con.execute("SELECT * FROM tables WHERE document_id=? AND joined_into IS NULL "
                                 "ORDER BY page, idx, created_at", (doc_id,)))
    if _csv(ids):
        keep = set(_csv(ids))
        ts = [t for t in ts if t["id"] in keep]
    elif include == "usable":
        ts = [t for t in ts if not t["deleted"] and t["status"] != "invalid"]
    elif include == "valid":
        ts = [t for t in ts if not t["deleted"] and t["status"] == "valid"]
    if auth.is_public(request):
        ts = [t for t in ts if table_is_public(t)]
    if format == "xlsx":
        data = exporters.xlsx_bytes(ts, doc_title=d["title"], doc_category=d.get("category"), numbers=numbers,
                                    decimal=decimal, title=title)
    elif format == "json":
        data = exporters.json_bytes(d, ts, decimal)
    else:
        data = exporters.zip_csv_bytes(ts)
    return download(data, exporters.safe_filename(d["title"][:80], format), MEDIA[format])


# ---------------------------------------------------------------- single table

class TablePatch(BaseModel):
    title: str | None = Field(None, max_length=1000)
    notes: str | None = Field(None, max_length=10000)
    rows: list[list[str]] | None = None
    merges: list[list[int]] | None = None
    header_rows: int | None = Field(None, ge=0, le=100)
    status: Literal["none", "valid", "invalid"] | None = None
    deleted: bool | None = None
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)
    decimal_sep: Literal["auto", ",", "."] | None = None
    checkpoint: bool = False  # force a history entry (structural edits)
    # the updated_at the client's copy is based on: when the table changed since (someone else saved), the
    # patch is refused with 409 and the current table, instead of silently overwriting the other edit
    base: float | None = None


class Conflict(HTTPException):
    def __init__(self, t: dict):
        names = auth.user_names()
        who = names.get(t.get("updated_by") or "", "someone else")
        super().__init__(409, {"message": f"This table was changed by {who} while you were editing it.",
                               "conflict": True, "table": get_one(t["id"])})


@router.patch("/tables/{table_id}")
def patch_table(table_id: str, body: TablePatch):
    with db.db() as con, db.tx(con):  # read, compare and write under one lock
        t = _fresh(con, table_id)
        if body.base is not None and abs(t["updated_at"] - body.base) > 1e-6:
            raise Conflict(t)
        _patch(con, t, body)
    return get_one(table_id)


def _patch(con, t: dict, body: TablePatch) -> bool:
    table_id = t["id"]
    sets: dict[str, object] = {}
    if body.title is not None:
        sets["title"] = body.title.strip()
    if body.notes is not None:
        sets["notes"] = body.notes.strip()
    if body.status is not None:
        sets["status"] = body.status
        if body.status != t["status"]:  # who decided, and when
            flagged = body.status != "none"
            sets["validated_by"] = _me() if flagged else None
            sets["validated_at"] = time.time() if flagged else None
    if body.decimal_sep is not None:
        sets["decimal_sep"] = body.decimal_sep
    if body.deleted is not None:
        sets["deleted"] = int(body.deleted)
        sets["deleted_at"] = time.time() if body.deleted else None
    if body.bbox is not None:
        x0, y0, x1, y1 = body.bbox
        sets["bbox"] = db.dumps([min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)])
    content = body.rows is not None or body.merges is not None or body.header_rows is not None
    if content:
        rows = t["rows"]
        if body.rows is not None:
            width = max((len(r) for r in body.rows), default=0)
            rows = [[str(c) for c in r] + [""] * (width - len(r)) for r in body.rows]
        width = max((len(r) for r in rows), default=0)
        merges = _clean_merges(body.merges if body.merges is not None else t["merges"], len(rows), width)
        header = min(body.header_rows if body.header_rows is not None else t["header_rows"], len(rows))
        sets.update(rows=db.dumps(rows), merges=db.dumps(merges), header_rows=header,
                    edited=int(is_edited(rows, merges, t)))
    if not sets:
        return False
    sets["updated_at"] = time.time()
    sets["updated_by"] = _me()
    if content:
        last = con.execute("SELECT created_at, created_by FROM table_versions WHERE table_id=? ORDER BY id DESC LIMIT 1",
                           (table_id,)).fetchone()
        # autosaves collapse into one history entry, but not across users (each keeps their own state)
        if body.checkpoint or not last or time.time() - last["created_at"] > VERSION_GAP_S \
                or t.get("updated_by") != sets["updated_by"]:
            _snapshot(con, t, "edit")
    con.execute(f"UPDATE tables SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), table_id))
    _touch_doc(con, t["document_id"])
    return True


@router.post("/tables/{table_id}/reset")
def reset_table(table_id: str):
    """Back to the extracted version (the current state is kept in history)."""
    with db.db() as con, db.tx(con):
        t = _fresh(con, table_id)
        vid = _snapshot(con, t, "before reset")
        con.execute("UPDATE tables SET rows=original_rows, merges=original_merges, edited=0, "
                    "header_rows=?, updated_at=?, updated_by=? WHERE id=?",
                    (jobs.guess_header_rows(t["original_rows"]), time.time(), _me(), table_id))
    return get_one(table_id) | {"undo_version": vid}


@router.delete("/tables/{table_id}")
def delete_table(table_id: str, request: Request, purge: bool = False):
    """Soft delete (flag 'deleted', restorable). purge=true (admin) removes it for good."""
    if purge:
        auth.need(request, "admin")
    t = get_table(table_id)
    with db.db() as con, db.tx(con):
        if purge:
            con.execute("DELETE FROM tables WHERE id=? OR joined_into=?", (table_id, table_id))
        else:
            con.execute("UPDATE tables SET deleted=1, deleted_at=?, updated_at=?, updated_by=? WHERE id=?",
                        (time.time(), time.time(), _me(), table_id))
        _touch_doc(con, t["document_id"])
    return {"ok": True, "purged": purge}


@router.get("/tables/{table_id}/versions")
def versions(table_id: str):
    get_table(table_id)
    with db.db() as con:
        vs = db.rows(con.execute(
            "SELECT id, rows, merges, header_rows, reason, created_at, created_by FROM table_versions "
            "WHERE table_id=? ORDER BY id", (table_id,)))
    names = auth.user_names()
    return [auth.with_names(v, names) for v in vs]


@router.post("/tables/{table_id}/versions/{version_id}/restore")
def restore_version(table_id: str, version_id: int):
    with db.db() as con, db.tx(con):
        t = _fresh(con, table_id)
        v = db.row(con.execute("SELECT * FROM table_versions WHERE id=? AND table_id=?",
                               (version_id, table_id)).fetchone())
        if not v:
            raise HTTPException(404, "Version not found")
        vid = _snapshot(con, t, "before restore")
        o = v.get("orig") or {}
        if o:  # undo of a re-extract also brings back the extracted version it replaced
            con.execute("UPDATE tables SET original_rows=?, original_merges=?, engine=? WHERE id=?",
                        (db.dumps(o["original_rows"]), db.dumps(o["original_merges"]), o["engine"], table_id))
            t = {**t, "original_rows": o["original_rows"], "original_merges": o["original_merges"]}
        con.execute("UPDATE tables SET rows=?, merges=?, header_rows=?, edited=?, bbox=COALESCE(?, bbox), "
                    "updated_at=?, updated_by=? WHERE id=?",
                    (db.dumps(v["rows"]), db.dumps(v["merges"]), v["header_rows"],
                     int(is_edited(v["rows"], v["merges"], t)), db.dumps(v["bbox"]) if v.get("bbox") else None,
                     time.time(), _me(), table_id))
    return get_one(table_id) | {"undo_version": vid}


class Reextract(BaseModel):
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)
    engine: str = "auto"


@router.post("/tables/{table_id}/reextract")
async def reextract(table_id: str, body: Reextract):
    """Run extraction again inside the (moved / resized) region and replace the table content.
    The previous content stays in the version history, so it can be undone."""
    t = get_table(table_id)
    if t["parts"]:
        raise HTTPException(409, "This table has joined continuations. Unjoin them before re-extracting.")
    if body.engine not in ENGINES or not ENGINES[body.engine].available:
        raise HTTPException(400, f"Engine not available: {body.engine}")
    x0, y0, x1, y1 = body.bbox or t["bbox"]
    clip = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    if clip[2] - clip[0] < 4 or clip[3] - clip[1] < 4:
        raise HTTPException(400, "Region is too small")
    me = _me()  # the worker thread below has no request context of its own

    def run():
        pdf = open_pdf(pdf_path(t["document_id"]))
        res = extract_page(pdf, t["page"], body.engine, clip)
        if res.get("error"):
            raise HTTPException(500, res["error"])
        found = [x for x in res["tables"] if x.rows]
        if not found:
            raise HTTPException(422, "No text found in this region")
        best = max(found, key=lambda x: sum(1 for r in x.rows for c in r if c))
        if len(found) > 1:  # several grids inside the region: stack them, keep the biggest one's spans
            rows, merges = [], []
            width = max(len(r) for x in found for r in x.rows)
            for x in sorted(found, key=lambda x: x.bbox[1]):
                if x is best:
                    merges = remap_merges(x.merges, list(range(len(x.rows))), list(range(width)))
                    merges = [[m[0] + len(rows), *m[1:]] for m in merges]
                rows += [r + [""] * (width - len(r)) for r in x.rows]
        else:
            rows, merges = best.rows, best.merges
        with db.db() as con, db.tx(con):
            cur = _fresh(con, table_id)  # the version as it is now (it may have been saved meanwhile)
            vid = _snapshot(con, cur, "before re-extract")
            con.execute(
                "UPDATE tables SET rows=?, original_rows=?, merges=?, original_merges=?, header_rows=?, bbox=?, "
                "engine=?, edited=0, title=CASE WHEN title='' THEN ? ELSE title END, "
                "notes=CASE WHEN notes='' THEN ? ELSE notes END, updated_at=?, updated_by=? WHERE id=?",
                (db.dumps(rows), db.dumps(rows), db.dumps(merges), db.dumps(merges), jobs.guess_header_rows(rows),
                 db.dumps(list(clip)), best.engine, best.title, best.notes, time.time(), me, table_id))
            _touch_doc(con, t["document_id"])
        return get_one(table_id) | {"undo_version": vid}

    return await run_in_threadpool(run)


@router.get("/tables/{table_id}/export")
def export_table(request: Request, table_id: str, format: Literal["csv", "xlsx", "json"] = "csv", numbers: bool = True,
                 decimal: Literal["auto", ",", "."] = "auto", title: bool = False):
    t = get_table_for(request, table_id)
    d = get_doc(t["document_id"])
    name = f"{d['title'][:50]} p{t['page']} {t['title'][:40]}"
    if format == "csv":
        data = exporters.csv_bytes(t)
    elif format == "xlsx":
        data = exporters.xlsx_bytes([t], doc_title=d["title"], doc_category=d.get("category"), numbers=numbers,
                                    decimal=decimal, title=title)
    else:
        data = json.dumps(exporters.table_json(t, decimal), ensure_ascii=False, indent=2).encode()
    return download(data, exporters.safe_filename(name, format), MEDIA[format])


@router.get("/tables/{table_id}/snapshot.png")
def snapshot(request: Request, table_id: str, scale: float = Query(1.5, ge=0.5, le=3.0), pad: float = Query(8, ge=0, le=100),
             part: int = Query(0, ge=0)):
    """The table's region rendered from the PDF (pdfspine), for previews and side-by-side checking.
    part=N renders the N-th joined continuation (1-based) instead of the main region."""
    t = get_table_for(request, table_id)
    if part:
        if part > len(t["parts"]):
            raise HTTPException(404, "No such part")
        t = {**t, "page": t["parts"][part - 1]["page"], "bbox": t["parts"][part - 1]["bbox"]}
    x0, y0, x1, y1 = t["bbox"]
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise HTTPException(404, "Table has no region")
    key = hashlib.md5(json.dumps([t["page"], t["bbox"], scale, pad]).encode()).hexdigest()[:12]
    out = doc_dir(t["document_id"]) / "snap" / f"{table_id}-{key}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        pdf = open_pdf(pdf_path(t["document_id"]))
        page = pdf[t["page"] - 1]
        pw, ph = page.rect.width, page.rect.height
        r = (max(0, x0 - pad) * scale, max(0, y0 - pad) * scale, min(pw, x1 + pad) * scale, min(ph, y1 + pad) * scale)
        # pdfspine applies `clip` in output pixels (after the matrix)
        page.get_pixmap(matrix=pdfspine.Matrix(scale, scale), clip=pdfspine.Rect(*r), alpha=False).save(str(out))
    return FileResponse(out, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


# ---------------------------------------------------------------- join continuation tables

def _norm_row(r: list[str]) -> str:
    return "|".join("".join(c.split()).lower() for c in r).strip("|")


def repeated_header(parent_rows: list[list[str]], child_rows: list[list[str]]) -> int:
    """How many leading rows of the continuation repeat the parent's leading (header) rows."""
    n = 0
    while n < min(len(parent_rows), len(child_rows), 12) and _norm_row(child_rows[n]) == _norm_row(parent_rows[n]) \
            and _norm_row(child_rows[n]):
        n += 1
    return n


def _key(c: str) -> str:
    return "".join(c.split()).lower()


def column_continuation(parent_rows: list[list[str]], child_rows: list[list[str]]) -> bool:
    """The continuation carries more columns for the same rows (BPS: a wide table split over two pages,
    the label column "Kabupaten/Kota" repeated with columns (1) (4) (5) on the next page)."""
    pk = [_key(r[0]) for r in parent_rows if r and _key(r[0])]
    ck = [_key(r[0]) for r in child_rows if r and _key(r[0])]
    if len(ck) < 3 or len(pk) < 3 or min(len(r) for r in child_rows) < 2:
        return False
    pool: dict[str, int] = {}
    for k in pk:
        pool[k] = pool.get(k, 0) + 1
    common = 0
    for k in ck:
        if pool.get(k):
            pool[k] -= 1
            common += 1
    return common >= 0.7 * max(len(pk), len(ck))


def join_columns(t: dict, o: dict) -> tuple[list[list[str]], list[list[int]], int, int]:
    """Place `o`'s columns to the right of `t`, matching rows on the label (first) column, whose copy in `o`
    is dropped. Returns rows, merges, added columns, added rows (labels of `o` missing in `t`, at the end)."""
    p, c = t["rows"], o["rows"]
    pw = max((len(r) for r in p), default=0)
    cw = max((len(r) for r in c), default=0)
    add = max(cw - 1, 0)
    out = [r + [""] * (pw - len(r) + add) for r in p]
    where: dict[str, list[int]] = {}
    for j, r in enumerate(p):
        if r and _key(r[0]):
            where.setdefault(_key(r[0]), []).append(j)
    used: set[int] = set()
    last, mapping, extra = -1, {}, 0
    for i, r in enumerate(c):
        r = r + [""] * (cw - len(r))
        k = _key(r[0])
        j = None
        if k:
            cands = [x for x in where.get(k, []) if x not in used]
            j = next((x for x in cands if x > last), cands[0] if cands else None)
        elif last + 1 < len(p) and last + 1 not in used and not _key(p[last + 1][0] if p[last + 1] else ""):
            j = last + 1  # unlabeled line (e.g. 2nd header line) next to the matching unlabeled parent line
        if j is None:
            out.append([r[0]] + [""] * (pw - 1) + r[1:])
            j = len(out) - 1
            extra += 1
        else:
            out[j][pw:] = r[1:]
            used.add(j)
            last = j
        mapping[i] = j
    merges = [list(m) for m in t["merges"]]
    for r0, c0, rs, cs in o["merges"]:
        if c0 == 0:
            continue
        rows = [mapping.get(r0 + k) for k in range(rs)]
        if None in rows or rows != list(range(rows[0], rows[0] + rs)):
            continue
        merges.append([rows[0], c0 - 1 + pw, rs, cs])
    return out, merges, add, extra


class Join(BaseModel):
    other: str
    skip: int | None = Field(None, ge=0, le=50)  # rows dropped from the top of `other`; None = auto (repeated header)
    # rows = append below; columns = append to the right, rows matched on the label column; auto = guess
    mode: Literal["auto", "rows", "columns"] = "auto"


@router.post("/tables/{table_id}/join")
def join(table_id: str, body: Join):
    """Append another table (a continuation, e.g. on the next page) to this one.
    The other table is hidden (joined_into) and remembered as a part, so it can be unjoined."""
    t, o = get_table(table_id), get_table(body.other)
    if t["id"] == o["id"] or o["document_id"] != t["document_id"]:
        raise HTTPException(400, "Can only join two different tables of the same document")
    if t["joined_into"] or o["joined_into"]:
        raise HTTPException(400, "Table is already joined into another table")
    mode = body.mode
    if mode == "auto":
        mode = "columns" if body.skip is None and column_continuation(t["rows"], o["rows"]) else "rows"
    if mode == "columns":
        rows, merges, cols, extra = join_columns(t, o)
        parts = t["parts"] + [{"table_id": o["id"], "page": o["page"], "bbox": o["bbox"], "rows": 0,
                               "skipped": 0, "mode": "columns", "cols": cols, "extra_rows": extra}] + o["parts"]
        return _save_join(t, o, rows, merges, parts) | {"skipped": 0, "mode": "columns", "cols": cols}
    skip = repeated_header(t["rows"], o["rows"]) if body.skip is None else min(body.skip, len(o["rows"]))
    width = max(max((len(r) for r in t["rows"]), default=0), max((len(r) for r in o["rows"]), default=0))
    pad = lambda rows: [r + [""] * (width - len(r)) for r in rows]  # noqa: E731
    added = pad(o["rows"][skip:])
    rows = pad(t["rows"]) + added
    shift = len(t["rows"]) - skip
    merges = t["merges"] + [[m[0] + shift, *m[1:]] for m in o["merges"] if m[0] >= skip]
    parts = t["parts"] + [{"table_id": o["id"], "page": o["page"], "bbox": o["bbox"], "rows": len(added),
                           "skipped": skip, "mode": "rows"}] + o["parts"]
    return _save_join(t, o, rows, merges, parts) | {"skipped": skip, "mode": "rows"}


def _save_join(t: dict, o: dict, rows: list[list[str]], merges: list[list[int]], parts: list[dict]) -> dict:
    table_id = t["id"]
    now, me = time.time(), _me()
    with db.db() as con, db.tx(con):
        cur, oc = _fresh(con, table_id), _fresh(con, o["id"])
        if cur["updated_at"] != t["updated_at"] or oc["updated_at"] != o["updated_at"] or oc["joined_into"]:
            raise HTTPException(409, "One of the tables changed meanwhile. Try joining again.")
        _snapshot(con, t, f"before join {o['id']}")
        con.execute(
            "UPDATE tables SET rows=?, merges=?, parts=?, edited=?, "
            "title=CASE WHEN title='' THEN ? ELSE title END, notes=CASE WHEN notes='' THEN ? ELSE notes END, "
            "updated_at=?, updated_by=? WHERE id=?",
            (db.dumps(rows), db.dumps(merges), db.dumps(parts), int(is_edited(rows, merges, t)), o["title"],
             o["notes"], now, me, table_id))
        con.execute("UPDATE tables SET joined_into=?, deleted=1, deleted_at=?, updated_at=?, updated_by=? WHERE id=?",
                    (table_id, now, now, me, o["id"]))
        _touch_doc(con, t["document_id"])
    return get_one(table_id)


class Unjoin(BaseModel):
    other: str


@router.post("/tables/{table_id}/unjoin")
def unjoin(table_id: str, body: Unjoin):
    """Undo a join: the table goes back to its state right before `other` was appended
    (later joins and edits made after it are undone too), and the continuation tables come back."""
    with db.db() as con, db.tx(con):
        t = _fresh(con, table_id)
        idx = next((i for i, p in enumerate(t["parts"]) if p["table_id"] == body.other), None)
        if idx is None:
            raise HTTPException(404, "That table is not joined here")
        v = db.row(con.execute("SELECT * FROM table_versions WHERE table_id=? AND reason=? ORDER BY id DESC LIMIT 1",
                               (table_id, f"before join {body.other}")).fetchone())
        if v:
            rows, merges, header = v["rows"], v["merges"], v["header_rows"]
        else:  # history pruned: cut the appended rows off the end (and appended columns off the right)
            direct = _direct(con, table_id)
            gone = [p for p in t["parts"][idx:] if p["table_id"] in direct]
            n = sum(p["rows"] + p.get("extra_rows", 0) for p in gone)
            cols = sum(p.get("cols", 0) for p in gone)
            rows = t["rows"][: max(1, len(t["rows"]) - n)]
            width = max((len(r) for r in rows), default=0)
            keep = max(1, width - cols)
            rows = [r[:keep] for r in rows]
            merges = [m for m in t["merges"] if m[0] + m[2] <= len(rows) and m[1] + m[3] <= keep]
            header = t["header_rows"]
        _snapshot(con, t, "before unjoin")
        released = [p["table_id"] for p in t["parts"][idx:]]
        con.execute("UPDATE tables SET rows=?, merges=?, header_rows=?, parts=?, edited=?, updated_at=?, updated_by=? "
                    "WHERE id=?", (db.dumps(rows), db.dumps(merges), header, db.dumps(t["parts"][:idx]),
                                   int(is_edited(rows, merges, t)), time.time(), _me(), table_id))
        if released:
            con.execute(f"UPDATE tables SET joined_into=NULL, deleted=0, deleted_at=NULL, updated_at=?, updated_by=? "
                        f"WHERE joined_into=? AND id IN ({','.join('?' * len(released))})",
                        (time.time(), _me(), table_id, *released))
        _touch_doc(con, t["document_id"])
    return get_one(table_id)


def _direct(con, table_id: str) -> set[str]:
    return {r["id"] for r in con.execute("SELECT id FROM tables WHERE joined_into=?", (table_id,))}


class JoinMany(BaseModel):
    ids: list[str] = Field(min_length=2, max_length=500)


@router.post("/tables/join-many")
def join_many(body: JoinMany):
    """Join a block of continuation tables in reading order: the first one becomes the parent,
    every other table is appended below it (repeated headers dropped). Each step can be unjoined."""
    with db.db() as con:
        ts = db.rows(con.execute(f"SELECT id, document_id, page, idx, joined_into FROM tables "
                                 f"WHERE id IN ({','.join('?' * len(body.ids))})", body.ids))
    if len(ts) != len(set(body.ids)):
        raise HTTPException(404, "Some tables were not found")
    if len({t["document_id"] for t in ts}) > 1:
        raise HTTPException(400, "All tables must belong to the same document")
    if any(t["joined_into"] for t in ts):
        raise HTTPException(400, "A selected table is already joined into another table")
    ts.sort(key=lambda t: (t["page"], t["idx"]))
    parent, skipped = ts[0]["id"], []
    for t in ts[1:]:
        skipped.append(join(parent, Join(other=t["id"]))["skipped"])
    return get_one(parent) | {"skipped": sum(skipped), "joined": len(ts) - 1}
