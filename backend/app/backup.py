"""Backup and restore of the whole library: one .zip with the SQLite database and the PDFs.

    backup.zip
      manifest.json            {"format": "tabel-dari-pdfs-backup", "version": 1, "created_at", "counts", "pdfs"}
      library.db               consistent copy (SQLite online backup, safe while the server runs)
      docs/<id>/source.pdf     the PDFs (optional: pdfs=False keeps only the data)
      docs/<id>/thumb.png

Restore modes:
  merge    add the documents of the backup that are not in this library yet (same id or the same PDF file
           are skipped), with their tables, history and jobs. Nothing here is changed or removed.
  replace  this library becomes the backup. The current database and PDFs are first moved to
           data/backups/before-restore-<time>/, so a replace can be undone by hand.

CLI (also inside Docker: docker compose exec app python -m app.backup ...):
    python -m app.backup export backup.zip [--no-pdfs]
    python -m app.backup import backup.zip [--mode merge|replace]
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from . import db
from .config import DATA_DIR, DB_PATH, DOCS_DIR

FORMAT = "tabel-dari-pdfs-backup"
VERSION = 1
BACKUPS_DIR = DATA_DIR / "backups"
DOC_FILES = ("source.pdf", "thumb.png")  # snap/ (rendered previews) is a cache: rebuilt on demand
_lock = threading.Lock()


class BackupError(ValueError):
    pass


def _counts(con: sqlite3.Connection) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "documents": one("SELECT COUNT(*) FROM documents WHERE deleted_at IS NULL"),
        "tables": one("SELECT COUNT(*) FROM tables WHERE joined_into IS NULL AND deleted=0"),
        "valid_tables": one("SELECT COUNT(*) FROM tables WHERE joined_into IS NULL AND deleted=0 AND status='valid'"),
    }


def info() -> dict:
    with db.db() as con:
        counts = _counts(con)
    pdf_bytes = sum(p.stat().st_size for p in DOCS_DIR.glob("*/source.pdf"))
    return {"counts": counts, "db_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0, "pdf_bytes": pdf_bytes}


# ---------------------------------------------------------------- export

def export_zip(out: Path, *, pdfs: bool = True) -> dict:
    """Write a backup to `out`; returns the manifest."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATA_DIR) as tmp:
        snap = Path(tmp) / "library.db"
        with db.db() as con:
            dst = sqlite3.connect(snap)
            con.backup(dst)  # consistent even while other requests write
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.close()
            ids = [r[0] for r in con.execute("SELECT id FROM documents")]
            counts = _counts(con)
        manifest = {"format": FORMAT, "version": VERSION, "created_at": time.time(), "counts": counts, "pdfs": pdfs}
        part = out.with_suffix(out.suffix + ".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
            z.write(snap, "library.db")
            if pdfs:
                for doc_id in ids:
                    for name in DOC_FILES:
                        f = DOCS_DIR / doc_id / name
                        if f.exists():  # PDFs / PNGs are compressed already
                            z.write(f, f"docs/{doc_id}/{name}", compress_type=zipfile.ZIP_STORED)
        part.replace(out)
    return manifest


def backup_name() -> str:
    return time.strftime("tabel-dari-pdfs-backup-%Y%m%d-%H%M.zip")


# ---------------------------------------------------------------- import

def _open_backup(path: Path, tmp: Path) -> tuple[dict, Path, zipfile.ZipFile]:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise BackupError("This is not a backup file (not a .zip)") from e
    names = set(z.namelist())
    if "manifest.json" not in names or "library.db" not in names:
        raise BackupError("This .zip is not a Tabel dari PDF backup (manifest.json or library.db missing)")
    manifest = json.loads(z.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise BackupError("This .zip is not a Tabel dari PDF backup")
    if int(manifest.get("version", 0)) > VERSION:
        raise BackupError("The backup was made by a newer version of the app. Update the app first.")
    for n in names:  # no path tricks
        if n.startswith("/") or ".." in Path(n).parts:
            raise BackupError("The backup contains an unsafe path")
    dbf = tmp / "library.db"
    with z.open("library.db") as src, open(dbf, "wb") as dst:
        shutil.copyfileobj(src, dst)
    con = sqlite3.connect(dbf, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The database in the backup is damaged")
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"documents", "tables"} <= have:
            raise BackupError("The database in the backup has no documents / tables")
        db.migrate(con)  # an older backup gets the current columns
    except sqlite3.DatabaseError as e:
        raise BackupError(f"The database in the backup cannot be read ({e})") from e
    finally:
        con.close()
    return manifest, dbf, z


def _extract_docs(z: zipfile.ZipFile, ids: set[str] | None, dest: Path) -> set[str]:
    """Copy docs/<id>/... for `ids` (None = all) into dest/<id>/; returns the ids that have a PDF."""
    with_pdf: set[str] = set()
    for n in z.namelist():
        parts = Path(n).parts
        if len(parts) != 3 or parts[0] != "docs" or parts[2] not in DOC_FILES:
            continue
        if ids is not None and parts[1] not in ids:
            continue
        target = dest / parts[1] / parts[2]
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(n) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        if parts[2] == "source.pdf":
            with_pdf.add(parts[1])
    return with_pdf


def _cols(con, schema: str, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def import_zip(path: Path, mode: str = "merge") -> dict:
    if mode not in ("merge", "replace"):
        raise BackupError("mode must be merge or replace")
    if not _lock.acquire(blocking=False):
        raise BackupError("Another restore is running")
    try:
        with db.db() as con:
            if con.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]:
                raise BackupError("Tables are being found right now. Wait until that finishes, then restore.")
        with tempfile.TemporaryDirectory(dir=DATA_DIR) as t:
            tmp = Path(t)
            manifest, dbf, z = _open_backup(path, tmp)
            with z:
                return _replace(manifest, dbf, z, tmp) if mode == "replace" else _merge(manifest, dbf, z)
    finally:
        _lock.release()


def _replace(manifest: dict, dbf: Path, z: zipfile.ZipFile, tmp: Path) -> dict:
    new_docs = tmp / "docs"
    new_docs.mkdir()
    with_pdf = _extract_docs(z, None, new_docs)
    keep = BACKUPS_DIR / time.strftime("before-restore-%Y%m%d-%H%M%S")
    k = 2
    while keep.exists():  # never overwrite an earlier safety copy
        keep = keep.with_name(f"{keep.name.split('~')[0]}~{k}")
        k += 1
    keep.mkdir(parents=True)
    with db.db() as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(DB_PATH) + suffix)
        if f.exists():
            shutil.move(str(f), keep / f.name)
    if DOCS_DIR.exists():
        shutil.move(str(DOCS_DIR), keep / "docs")
    shutil.move(str(dbf), DB_PATH)
    if manifest.get("pdfs", True):
        shutil.move(str(new_docs), DOCS_DIR)
    else:  # data-only backup: keep the PDFs this library already has
        shutil.copytree(keep / "docs", DOCS_DIR, dirs_exist_ok=True) if (keep / "docs").exists() else DOCS_DIR.mkdir()
    db.init()
    with db.db() as con:
        ids = {r[0] for r in con.execute("SELECT id FROM documents")}
        counts = _counts(con)
    missing = sorted(i for i in ids if not (DOCS_DIR / i / "source.pdf").exists())
    return {"mode": "replace", "counts": counts, "missing_pdfs": len(missing), "previous": str(keep),
            "pdfs_in_backup": len(with_pdf)}


def _merge(manifest: dict, dbf: Path, z: zipfile.ZipFile) -> dict:
    names = set(z.namelist())
    with db.db() as con:
        con.execute("ATTACH DATABASE ? AS b", (str(dbf),))
        try:
            here_ids = {r[0] for r in con.execute("SELECT id FROM main.documents")}
            here_sha = {r[0] for r in con.execute("SELECT sha256 FROM main.documents")}
            add, skipped_same, skipped_nopdf = [], 0, 0
            for r in con.execute("SELECT id, sha256 FROM b.documents WHERE deleted_at IS NULL"):
                if r[0] in here_ids or r[1] in here_sha:
                    skipped_same += 1
                elif f"docs/{r[0]}/source.pdf" not in names and not (DOCS_DIR / r[0] / "source.pdf").exists():
                    skipped_nopdf += 1
                else:
                    add.append(r[0])
            n_tables = 0
            if add:
                marks = ",".join("?" * len(add))
                con.execute("BEGIN")
                for table, key in (("documents", "id"), ("jobs", "document_id"), ("tables", "document_id")):
                    cols = [c for c in _cols(con, "main", table) if c in _cols(con, "b", table)]
                    cl = ",".join(cols)
                    cur = con.execute(f"INSERT OR IGNORE INTO main.{table} ({cl}) SELECT {cl} FROM b.{table} "
                                      f"WHERE {key} IN ({marks})", add)
                    if table == "tables":
                        n_tables = cur.rowcount
                vcols = [c for c in _cols(con, "main", "table_versions")
                         if c != "id" and c in _cols(con, "b", "table_versions")]
                vl = ",".join(vcols)
                con.execute(f"INSERT INTO main.table_versions ({vl}) SELECT {vl} FROM b.table_versions WHERE table_id IN "
                            f"(SELECT id FROM b.tables WHERE document_id IN ({marks})) ORDER BY id", add)
                con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.execute("DETACH DATABASE b")
    if add:
        _extract_docs(z, set(add), DOCS_DIR)
    with db.db() as con:
        counts = _counts(con)
    return {"mode": "merge", "added_documents": len(add), "added_tables": n_tables,
            "skipped_existing": skipped_same, "skipped_without_pdf": skipped_nopdf, "counts": counts}


# ---------------------------------------------------------------- CLI

def _main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="python -m app.backup", description="Back up / restore the Tabel dari PDF library")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write a backup .zip")
    e.add_argument("out", nargs="?", default=None, help=f"default: data/backups/{backup_name()}")
    e.add_argument("--no-pdfs", action="store_true", help="only the data (database), not the PDF files")
    i = sub.add_parser("import", help="restore a backup .zip")
    i.add_argument("file")
    i.add_argument("--mode", choices=("merge", "replace"), default="merge")
    a = p.parse_args()
    db.init()
    if a.cmd == "export":
        out = Path(a.out) if a.out else BACKUPS_DIR / backup_name()
        m = export_zip(out, pdfs=not a.no_pdfs)
        print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB, {m['counts']['documents']} documents, "
              f"{m['counts']['tables']} tables{'' if m['pdfs'] else ', without PDFs'})")
    else:
        print(json.dumps(import_zip(Path(a.file), a.mode), indent=2))


if __name__ == "__main__":
    _main()
