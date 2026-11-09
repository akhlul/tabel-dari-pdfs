"""SQLite library: documents, extraction jobs, tables."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  source TEXT NOT NULL,              -- upload | bps
  source_key TEXT UNIQUE,            -- bps:<kind>:<domain>:<id>
  meta TEXT NOT NULL DEFAULT '{}',   -- json: BPS metadata, abstract, cover...
  page_count INTEGER NOT NULL,
  pages TEXT NOT NULL,               -- json [[w,h], ...] in points
  size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  engine TEXT NOT NULL,
  pages TEXT,                        -- json list or null = all
  status TEXT NOT NULL,              -- queued | running | done | error | cancelled
  done INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  found INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS tables (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  job_id TEXT,
  page INTEGER NOT NULL,
  idx INTEGER NOT NULL DEFAULT 0,
  bbox TEXT NOT NULL,
  engine TEXT NOT NULL,
  origin TEXT NOT NULL DEFAULT 'job', -- job | region | manual
  title TEXT NOT NULL DEFAULT '',
  header_rows INTEGER NOT NULL DEFAULT 1,
  rows TEXT NOT NULL,
  original_rows TEXT NOT NULL,
  edited INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tables_doc ON tables(document_id, page, idx);
CREATE INDEX IF NOT EXISTS jobs_doc ON jobs(document_id, created_at);
-- previous states of a table (undo across sessions, "restore previous version")
CREATE TABLE IF NOT EXISTS table_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_id TEXT NOT NULL REFERENCES tables(id) ON DELETE CASCADE,
  rows TEXT NOT NULL,
  merges TEXT NOT NULL DEFAULT '[]',
  header_rows INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT 'edit',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS versions_table ON table_versions(table_id, id);
"""

# columns added after the first release: (table, column, definition)
MIGRATIONS = [
    ("tables", "notes", "TEXT NOT NULL DEFAULT ''"),
    ("tables", "merges", "TEXT NOT NULL DEFAULT '[]'"),           # [[row, col, row_span, col_span], ...]
    ("tables", "original_merges", "TEXT NOT NULL DEFAULT '[]'"),
    ("tables", "status", "TEXT NOT NULL DEFAULT 'none'"),         # none | valid | invalid
    ("tables", "deleted", "INTEGER NOT NULL DEFAULT 0"),           # soft delete
    ("tables", "deleted_at", "REAL"),
    ("tables", "original_bbox", "TEXT"),                           # region as extracted, before move/resize
    ("tables", "parts", "TEXT NOT NULL DEFAULT '[]'"),             # joined continuations: [{table_id,page,bbox,rows}]
    ("tables", "joined_into", "TEXT"),                             # this table was appended to another one
    ("table_versions", "bbox", "TEXT"),
    ("table_versions", "orig", "TEXT"),                            # {original_rows, original_merges, engine}                            # area at that time (undo of move / re-extract)
    ("documents", "deleted_at", "REAL"),                           # soft delete (undo), purged later
    ("tables", "decimal_sep", "TEXT NOT NULL DEFAULT 'auto'"),     # auto | , | .  (how numbers are read on export)
    ("documents", "category", "TEXT"),                             # json path ["Statistik Ekonomi","Transportasi"]; [] = uncategorised, NULL = not set yet
]

JSON_COLS = {"category", "document_category", "meta", "pages", "bbox", "rows", "original_rows", "merges", "original_merges", "original_bbox", "parts", "orig"}


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    con = _connect()
    try:
        yield con
    finally:
        con.close()


def migrate(con: sqlite3.Connection) -> None:
    """Bring any library database (also an older backup) to the current schema."""
    con.executescript(SCHEMA)
    for table, col, ddl in MIGRATIONS:
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    con.executescript("CREATE INDEX IF NOT EXISTS tables_flags ON tables(status, deleted, edited);")
    _categorise_new(con)


def init() -> None:
    with db() as con:
        migrate(con)
        # documents deleted (with undo) more than an hour ago are removed for good
        _purge_deleted_documents(con, older_than=3600)
        # a server restart kills in-flight jobs
        con.execute(
            "UPDATE jobs SET status='error', error='interrupted by server restart', finished_at=? "
            "WHERE status IN ('queued','running')",
            (time.time(),),
        )


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS & d.keys():
        if isinstance(d[k], str):
            d[k] = json.loads(d[k])
    for k in ("edited", "deleted"):
        if k in d:
            d[k] = bool(d[k])
    return d


def rows(rs) -> list[dict[str, Any]]:
    return [row(r) for r in rs]  # type: ignore[misc]


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def _categorise_new(con) -> None:
    """Documents from before categories existed: BPS ones get their subject, uploads stay uncategorised."""
    from . import categories

    for r in con.execute("SELECT id, title, source, meta FROM documents WHERE category IS NULL").fetchall():
        d = row(r) or {}
        con.execute("UPDATE documents SET category=? WHERE id=?", (dumps(categories.for_document(d)), d["id"]))


def _purge_deleted_documents(con, older_than: float) -> None:
    import shutil

    from .config import DOCS_DIR

    for r in con.execute("SELECT id FROM documents WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                         (time.time() - older_than,)).fetchall():
        con.execute("DELETE FROM documents WHERE id=?", (r["id"],))
        shutil.rmtree(DOCS_DIR / r["id"], ignore_errors=True)
