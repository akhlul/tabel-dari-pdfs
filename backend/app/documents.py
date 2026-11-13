"""PDF storage + metadata via pdfspine."""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path

import pdfspine

from . import categories, db
from .config import DOCS_DIR


class PdfError(ValueError):
    pass


_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20\d\d)(?!\d)")


def guess_year(title: str, meta: dict | None = None, filename: str = "") -> int | None:
    """The year a publication is about: the last year in its title ("… 2025", "2019–2024" -> 2024),
    else the BPS release year, else the last year in the file name."""
    ys = _YEAR.findall(title or "")
    if ys:
        return int(ys[-1])
    rel = str((meta or {}).get("date") or "")[:4]
    if rel.isdigit() and 1950 <= int(rel) <= 2100:
        return int(rel)
    ys = _YEAR.findall(Path(filename or "").stem.replace("_", " "))
    return int(ys[-1]) if ys else None


def clean_tags(tags: list[str]) -> list[str]:
    """Trimmed, single spaces, at most 40 characters, no duplicates (ignoring case), at most 30."""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        t = re.sub(r"\s+", " ", str(t)).strip().strip(",")[:40].strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:30]


def doc_dir(doc_id: str) -> Path:
    return DOCS_DIR / doc_id


def pdf_path(doc_id: str) -> Path:
    return doc_dir(doc_id) / "source.pdf"


def thumb_path(doc_id: str) -> Path:
    return doc_dir(doc_id) / "thumb.png"


def open_pdf(path: Path):
    try:
        doc = pdfspine.open(str(path))
    except Exception as e:  # pdfspine.PdfError family
        raise PdfError(f"Not a readable PDF: {e}") from e
    if getattr(doc, "needs_pass", False) and not doc.authenticate(""):
        raise PdfError("PDF is password protected")
    return doc


def store_pdf(data: bytes, *, filename: str, title: str, source: str, source_key: str | None = None,
              meta: dict | None = None, by: str | None = None) -> dict:
    if not data.startswith(b"%PDF"):
        # some servers prepend whitespace/BOM; tolerate a short prefix
        i = data.find(b"%PDF", 0, 1024)
        if i < 0:
            raise PdfError("File is not a PDF")
        data = data[i:]
    sha = hashlib.sha256(data).hexdigest()
    with db.db() as con:
        existing = con.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()
        if existing:
            if existing["deleted_at"]:  # deleted earlier (undo window): bring it back
                con.execute("UPDATE documents SET deleted_at=NULL, updated_at=? WHERE id=?", (time.time(), existing["id"]))
                existing = con.execute("SELECT * FROM documents WHERE id=?", (existing["id"],)).fetchone()
            return db.row(existing)  # type: ignore[return-value]

    doc_id = db.new_id()
    d = doc_dir(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    path = pdf_path(doc_id)
    path.write_bytes(data)
    try:
        pdf = open_pdf(path)
        pages = [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in pdf]
        if not pages:
            raise PdfError("PDF has no pages")
        _thumbnail(pdf, thumb_path(doc_id))
        if not title:
            title = (pdf.metadata or {}).get("title") or Path(filename).stem
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    now = time.time()
    category = categories.for_document({"source": source, "meta": meta or {}, "title": title})
    with db.db() as con:
        with db.tx(con):
            # the same file (or BPS item) may have been stored by another request while this one was reading it
            dup = con.execute("SELECT id FROM documents WHERE sha256=? OR (source_key IS NOT NULL AND source_key=?)",
                              (sha, source_key)).fetchone()
            if dup is None:
                con.execute(
                    "INSERT INTO documents (id,title,filename,source,source_key,meta,category,page_count,pages,size,"
                    "sha256,created_at,updated_at,created_by,updated_by,year) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (doc_id, title, filename, source, source_key, db.dumps(meta or {}), db.dumps(category),
                     len(pages), db.dumps(pages), len(data), sha, now, now, by, by,
                     guess_year(title, meta, filename)),
                )
            else:
                con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (dup["id"],))
        if dup is not None:
            shutil.rmtree(d, ignore_errors=True)  # no orphan folder
            doc_id = dup["id"]
        return db.row(con.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone())  # type: ignore[return-value]


def _thumbnail(pdf, out: Path) -> None:
    try:
        page = pdf[0]
        scale = 220 / max(page.rect.width, 1)
        pix = page.get_pixmap(matrix=pdfspine.Matrix(scale, scale), alpha=False)
        pix.save(str(out))
    except Exception:
        pass  # thumbnail is cosmetic


def delete_document(doc_id: str) -> None:
    with db.db() as con:
        con.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    shutil.rmtree(doc_dir(doc_id), ignore_errors=True)
