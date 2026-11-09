"""Helpers shared by the API modules."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

from . import db

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


def doc_kind(d: dict) -> str:
    return (d.get("meta") or {}).get("kind", "publication") if d["source"] == "bps" else "upload"


def download(data: bytes, filename: str, media: str) -> Response:
    ascii_name = filename.encode("ascii", "ignore").decode() or "tables"
    return Response(data, media_type=media, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"})
