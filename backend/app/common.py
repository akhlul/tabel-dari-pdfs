"""Helpers shared by the API modules."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import HTTPException, Request
from fastapi.responses import Response

from . import auth, db

MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "zip": "application/zip",
}


def get_doc(doc_id: str) -> dict:
    with db.db() as con:
        d = db.row(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())
    if not d:
        raise HTTPException(404, "Document not found")
    return d


def get_table(table_id: str) -> dict:
    with db.db() as con:
        t = db.row(con.execute("SELECT * FROM tables WHERE id=?", (table_id,)).fetchone())
    if not t:
        raise HTTPException(404, "Table not found")
    return t


# ---------------------------------------------------------------- what visitors who are not logged in may see

# a table is public when it is flagged valid, not deleted and not joined into another table (alias t)
PUBLIC_TABLE_SQL = "t.status='valid' AND t.deleted=0 AND t.joined_into IS NULL"
# a document is public when it is not deleted and has at least one public table (alias d)
PUBLIC_DOC_SQL = (f"d.deleted_at IS NULL AND EXISTS (SELECT 1 FROM tables t WHERE t.document_id=d.id "
                  f"AND {PUBLIC_TABLE_SQL})")


def table_is_public(t: dict) -> bool:
    return t["status"] == "valid" and not t["deleted"] and not t["joined_into"]


def doc_is_public(doc_id: str) -> bool:
    with db.db() as con:
        return con.execute(f"SELECT 1 FROM documents d WHERE d.id=? AND {PUBLIC_DOC_SQL}", (doc_id,)).fetchone() \
            is not None


def get_doc_for(request: Request, doc_id: str) -> dict:
    """get_doc, but visitors who are not logged in only get documents with valid tables (else 404)."""
    d = get_doc(doc_id)
    if auth.is_public(request) and not doc_is_public(doc_id):
        raise HTTPException(404, "Document not found")
    return d


def get_table_for(request: Request, table_id: str) -> dict:
    """get_table, but visitors who are not logged in only get valid tables of a visible document (else 404)."""
    t = get_table(table_id)
    if auth.is_public(request):
        d = get_doc(t["document_id"])
        if not table_is_public(t) or d.get("deleted_at"):
            raise HTTPException(404, "Table not found")
    return t


def doc_kind(d: dict) -> str:
    return (d.get("meta") or {}).get("kind", "publication") if d["source"] == "bps" else "upload"


def download(data: bytes, filename: str, media: str) -> Response:
    ascii_name = filename.encode("ascii", "ignore").decode() or "tables"
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"})
