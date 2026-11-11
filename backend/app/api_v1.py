"""Public read-only JSON API: only tables flagged valid, for scripts, dashboards and other web pages.

    GET /api/v1/documents                       documents that have valid tables
    GET /api/v1/documents/{id}/tables           all valid tables of a document, with their data
    GET /api/v1/tables?q=&document=&offset=&limit=   valid tables (without the cells)
    GET /api/v1/tables/{id}?decimal=auto|,|.    one valid table: columns, rows and records (numbers parsed)

No login, CORS open to every origin (auth.AuthMiddleware), rate-limited per IP. Responses carry an ETag
(answer If-None-Match with 304) and Cache-Control: max-age=60.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from . import db, exporters
from .common import PUBLIC_DOC_SQL, PUBLIC_TABLE_SQL, doc_kind

router = APIRouter(prefix="/api/v1", tags=["public API v1"])
Decimal = Literal["auto", ",", "."]


def _cached(request: Request, payload, stamp: object) -> Response:
    """JSON with an ETag computed from `stamp` (e.g. the newest updated_at involved)."""
    etag = '"' + hashlib.sha1(json.dumps([request.url.path, str(request.query_params), stamp]).encode()).hexdigest()[:20] + '"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=60"}
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers=headers)
    return JSONResponse(payload, headers=headers)


def _doc_out(d: dict) -> dict:
    meta = d.get("meta") or {}
    return {"id": d["id"], "title": d["title"], "kind": doc_kind(d), "category": d.get("category") or [],
            "page_count": d["page_count"], "date": meta.get("date"), "bps_id": meta.get("id"),
            "domain": meta.get("domain"), "source_pdf": meta.get("pdf"), "updated_at": d["updated_at"],
            "table_count": d.get("valid_count")}


def _table_meta(t: dict) -> dict:
    rows, h = t["rows"], t["header_rows"]
    return {"id": t["id"], "document_id": t["document_id"], "title": t["title"], "notes": t["notes"],
            "page": t["page"], "pages": sorted({t["page"], *(p["page"] for p in t.get("parts") or [])}),
            "columns": exporters.column_names(rows, h), "row_count": max(0, len(rows) - h),
            "updated_at": t["updated_at"], "url": f"/api/v1/tables/{t['id']}"}


def _table_full(t: dict, decimal: str) -> dict:
    j = exporters.table_json(t, decimal)
    for k in ("status", "deleted", "engine", "bbox", "parts"):
        j.pop(k, None)
    return j | {"pages": sorted({t["page"], *(p["page"] for p in t.get("parts") or [])}),
                "updated_at": t["updated_at"]}


@router.get("/documents")
def documents(request: Request, q: str = "", offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    sql = (f"SELECT d.*, (SELECT COUNT(*) FROM tables t WHERE t.document_id=d.id AND {PUBLIC_TABLE_SQL}) "
           f"AS valid_count FROM documents d WHERE {PUBLIC_DOC_SQL}")
    args: list = []
    if q.strip():
        sql += " AND d.title LIKE ?"
        args.append(f"%{q.strip()}%")
    with db.db() as con:
        ds = db.rows(con.execute(sql + " ORDER BY d.updated_at DESC", args))
    page = ds[offset: offset + limit]
    return _cached(request, {"total": len(ds), "offset": offset, "limit": limit, "items": [_doc_out(d) for d in page]},
                   max((d["updated_at"] for d in ds), default=0))


@router.get("/documents/{doc_id}/tables")
def document_tables(request: Request, doc_id: str, decimal: Decimal = "auto"):
    with db.db() as con:
        d = db.row(con.execute(f"SELECT d.* FROM documents d WHERE d.id=? AND {PUBLIC_DOC_SQL}", (doc_id,)).fetchone())
        if not d:
            raise HTTPException(404, "Document not found")
        ts = db.rows(con.execute(f"SELECT * FROM tables t WHERE t.document_id=? AND {PUBLIC_TABLE_SQL} "
                                 "ORDER BY page, idx, created_at", (doc_id,)))
    d["valid_count"] = len(ts)
    return _cached(request, {"document": _doc_out(d), "tables": [_table_full(t, decimal) for t in ts]},
                   [d["updated_at"], max((t["updated_at"] for t in ts), default=0)])


@router.get("/tables")
def tables(request: Request, q: str = "", document: str = "", offset: int = Query(0, ge=0),
           limit: int = Query(100, ge=1, le=500)):
    sql = (f"SELECT t.*, d.title AS doc_title FROM tables t JOIN documents d ON d.id=t.document_id "
           f"WHERE {PUBLIC_TABLE_SQL} AND d.deleted_at IS NULL")
    args: list = []
    if document:
        sql += " AND t.document_id=?"
        args.append(document)
    if q.strip():
        sql += " AND (t.title LIKE ? OR t.notes LIKE ? OR d.title LIKE ?)"
        args += [f"%{q.strip()}%"] * 3
    with db.db() as con:
        ts = db.rows(con.execute(sql + " ORDER BY d.title, t.document_id, t.page, t.idx", args))
    page = ts[offset: offset + limit]
    items = [_table_meta(t) | {"document_title": t["doc_title"]} for t in page]
    return _cached(request, {"total": len(ts), "offset": offset, "limit": limit, "items": items},
                   [len(ts), max((t["updated_at"] for t in ts), default=0)])


@router.get("/tables/{table_id}")
def table(request: Request, table_id: str, decimal: Decimal = "auto"):
    with db.db() as con:
        t = db.row(con.execute(f"SELECT t.* FROM tables t JOIN documents d ON d.id=t.document_id "
                               f"WHERE t.id=? AND {PUBLIC_TABLE_SQL} AND d.deleted_at IS NULL", (table_id,)).fetchone())
        if not t:
            raise HTTPException(404, "Table not found")
        d = db.row(con.execute("SELECT * FROM documents WHERE id=?", (t["document_id"],)).fetchone())
    return _cached(request, _table_full(t, decimal) | {"document": _doc_out(d)}, [t["updated_at"], d["updated_at"]])
