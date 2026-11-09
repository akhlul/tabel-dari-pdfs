from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from . import backup, bps, categories, db, geo, jobs
from .common import get_doc, get_table
from .tables_api import router as tables_router
from .config import BPS_KEY, FRONTEND_DIST, MAX_PDF_MB
from .documents import PdfError, delete_document, open_pdf, pdf_path, store_pdf, thumb_path
from .extract import ENGINES, extract_page, list_engines

app = FastAPI(title="Tabel dari PDF", version="1.1")
db.init()
app.include_router(tables_router)


@app.exception_handler(bps.BpsError)
async def _bps_error(_, e: bps.BpsError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.exception_handler(PdfError)
async def _pdf_error(_, e: PdfError):
    return JSONResponse({"detail": str(e)}, status_code=400)


# ---------------------------------------------------------------- helpers

_doc = get_doc
_table = get_table


# ---------------------------------------------------------------- meta

@app.get("/api/health")
def health():
    return {"ok": True, "bps_key": bool(BPS_KEY), "engines": [e["id"] for e in list_engines() if e["available"]]}


@app.get("/api/engines")
def engines():
    return list_engines()


# ---------------------------------------------------------------- BPS

@app.get("/api/bps/domains")
async def bps_domains():
    return await run_in_threadpool(bps.domains)


@app.get("/api/bps/search")
async def bps_search(
    kind: Literal["publication", "pressrelease"] = "publication",
    domain: str = Query("0000", pattern=r"^\d{4}$"),
    keyword: str = "",
    year: int | None = Query(None, ge=1900, le=2100),
    month: int | None = Query(None, ge=1, le=12),
    page: int = Query(1, ge=1),
    lang: Literal["ind", "eng"] = "ind",
):
    res = await run_in_threadpool(bps.search, kind, domain=domain, keyword=keyword, year=year, month=month,
                                  page=page, lang=lang)
    with db.db() as con:
        keys = {r["source_key"]: r["id"] for r in con.execute(
            "SELECT id, source_key FROM documents WHERE source='bps' AND deleted_at IS NULL").fetchall()}
    for it in res["items"]:
        it["document_id"] = keys.get(f"bps:{kind}:{domain}:{it['id']}")
    return res


@app.get("/api/locate")
async def locate(request: Request, lat: float | None = Query(None, ge=-90, le=90),
                 lon: float | None = Query(None, ge=-180, le=180)):
    """The BPS province of the user (default search region): from browser coordinates when given, else the IP.
    {domain: "1800" | null, region, country, source: location | ip}"""
    try:
        if lat is not None and lon is not None:
            return await run_in_threadpool(geo.from_coords, lat, lon)
        return await run_in_threadpool(geo.from_ip, request.client.host if request.client else None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Location lookup failed ({type(e).__name__})") from e


class BpsImport(BaseModel):
    kind: Literal["publication", "pressrelease"]
    id: str = Field(min_length=1, max_length=64)
    domain: str = Field("0000", pattern=r"^\d{4}$")
    lang: Literal["ind", "eng"] = "ind"
    extract: bool = True
    engine: str = "auto"


_imports: dict[str, dict] = {}


@app.post("/api/bps/import")
def bps_import(body: BpsImport):
    """Start importing a BPS item. Returns the existing document, or an import id to poll for progress."""
    key = f"bps:{body.kind}:{body.domain}:{body.id}"
    with db.db() as con:
        existing = db.row(con.execute("SELECT * FROM documents WHERE source_key=?", (key,)).fetchone())
        if existing and existing["deleted_at"]:
            con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (existing["id"],))
    if existing:
        return {"document": _doc(existing["id"]), "job": None, "existing": True}
    iid = db.new_id()
    _imports[iid] = {"id": iid, "status": "starting", "done": 0, "total": None, "document": None, "job": None,
                     "error": None, "cancel": False}
    threading.Thread(target=_run_import, args=(iid, body, key), daemon=True).start()
    return {"import": _public(_imports[iid]), "existing": False}


def _public(st: dict) -> dict:
    return {k: v for k, v in st.items() if k != "cancel"}


def _run_import(iid: str, body: BpsImport, key: str) -> None:
    st = _imports[iid]
    try:
        item = bps.detail(body.kind, body.id, domain=body.domain, lang=body.lang)
        st.update(status="downloading", total=bps.parse_size(item.get("size")))
        data = bps.download(item["pdf"], progress=lambda d, t: st.update(done=d, total=t or st["total"]),
                            cancelled=lambda: st["cancel"])
        st["status"] = "processing"
        meta = item | {"domain": body.domain, "lang": body.lang}
        doc = store_pdf(data, filename=f"{body.kind}-{body.id}.pdf", title=item["title"], source="bps",
                        source_key=key, meta=meta)
        if doc.get("source_key") != key and doc["source"] != "bps":
            # same file was uploaded before: attach the BPS metadata to it
            with db.db() as con:
                con.execute("UPDATE documents SET source='bps', source_key=?, meta=?, title=?, category=CASE "
                            "WHEN category IS NULL OR category='[]' THEN ? ELSE category END WHERE id=?",
                            (key, db.dumps(meta), item["title"], db.dumps(categories.for_bps(meta, item["title"])),
                             doc["id"]))
            doc = _doc(doc["id"])
        st["job"] = jobs.create_job(doc, body.engine, None) if body.extract else None
        st.update(status="done", document=doc)
    except bps.Cancelled:
        st.update(status="cancelled")
    except (bps.BpsError, PdfError) as e:
        st.update(status="error", error=str(e))
    except Exception as e:  # noqa: BLE001
        st.update(status="error", error=f"Import failed: {type(e).__name__}")


@app.get("/api/bps/import/{iid}")
def import_status(iid: str):
    if iid not in _imports:
        raise HTTPException(404, "Import not found")
    return _public(_imports[iid])


@app.post("/api/bps/import/{iid}/cancel")
def import_cancel(iid: str):
    if iid not in _imports:
        raise HTTPException(404, "Import not found")
    _imports[iid]["cancel"] = True
    return {"ok": True}


# ---------------------------------------------------------------- documents

@app.get("/api/documents")
def list_documents(q: str = ""):
    sql = """
      SELECT d.*, (SELECT COUNT(*) FROM tables t WHERE t.document_id=d.id AND t.deleted=0) AS table_count,
             (SELECT COUNT(*) FROM tables t WHERE t.document_id=d.id AND t.deleted=0 AND t.status='valid') AS valid_count,
             (SELECT status FROM jobs j WHERE j.document_id=d.id ORDER BY created_at DESC LIMIT 1) AS last_job_status
      FROM documents d {where} ORDER BY d.updated_at DESC"""
    with db.db() as con:
        if q.strip():
            rs = con.execute(sql.format(where="WHERE d.deleted_at IS NULL AND (d.title LIKE ? OR d.filename LIKE ?)"),
                             (f"%{q}%", f"%{q}%"))
        else:
            rs = con.execute(sql.format(where="WHERE d.deleted_at IS NULL"))
        return db.rows(rs)


@app.post("/api/documents")
async def upload(file: UploadFile = File(...), title: str = Form(""), extract: bool = Form(False),
                 engine: str = Form("auto")):
    data = await file.read(MAX_PDF_MB * 1024 * 1024 + 1)
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF larger than {MAX_PDF_MB} MB")
    doc = await run_in_threadpool(store_pdf, data, filename=file.filename or "upload.pdf", title=title.strip(),
                                  source="upload")
    job = jobs.create_job(doc, engine, None) if extract else None
    return {"document": doc, "job": job}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    d = _doc(doc_id)
    with db.db() as con:
        d["jobs"] = db.rows(con.execute(
            "SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 10", (doc_id,)))
    return d


class DocPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    category: list[str] | None = Field(None, max_length=6)  # [] = uncategorised


@app.patch("/api/documents/{doc_id}")
def patch_document(doc_id: str, body: DocPatch):
    _doc(doc_id)
    with db.db() as con:
        if body.title is not None:
            con.execute("UPDATE documents SET title=?, updated_at=? WHERE id=?", (body.title.strip(), time.time(), doc_id))
        if body.category is not None:  # moving does not count as an edit (keeps the "recent" order)
            con.execute("UPDATE documents SET category=? WHERE id=?", (db.dumps(categories.clean(body.category)), doc_id))
    return _doc(doc_id)


class CategoryMove(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=5000)
    category: list[str] = Field(max_length=6)


@app.post("/api/documents/move")
def move_documents(body: CategoryMove):
    """Put several documents into one category."""
    marks = ",".join("?" * len(body.ids))
    with db.db() as con:
        n = con.execute(f"UPDATE documents SET category=? WHERE id IN ({marks})",
                        (db.dumps(categories.clean(body.category)), *body.ids)).rowcount
    return {"ok": True, "changed": n}


class CategoryRename(BaseModel):
    path: list[str] = Field(min_length=1, max_length=6)  # the category to rename / move
    to: list[str] = Field(max_length=6)                   # its new path ([] = dissolve into uncategorised)


@app.post("/api/categories/rename")
def rename_category(body: CategoryRename):
    """Rename or move a category with everything below it (sub-categories keep their place under it)."""
    src, dst = categories.clean(body.path), categories.clean(body.to)
    n = 0
    with db.db() as con:
        for r in con.execute("SELECT id, category FROM documents WHERE category IS NOT NULL").fetchall():
            cat = db.row(r)["category"] or []  # type: ignore[index]
            if cat[: len(src)] == src:
                new = (dst + cat[len(src):]) if dst else []
                con.execute("UPDATE documents SET category=? WHERE id=?", (db.dumps(new[:6]), r["id"]))
                n += 1
    return {"ok": True, "changed": n}


@app.post("/api/documents/{doc_id}/auto-category")
def auto_category(doc_id: str):
    """Back to the automatic category (BPS subject or title guess; uploads: uncategorised)."""
    d = _doc(doc_id)
    with db.db() as con:
        con.execute("UPDATE documents SET category=? WHERE id=?", (db.dumps(categories.for_document(d)), doc_id))
    return _doc(doc_id)


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: str, purge: bool = False):
    """Soft delete (restorable for an hour, see db.init); purge=true removes files immediately."""
    _doc(doc_id)
    if purge:
        delete_document(doc_id)
    else:
        with db.db() as con:
            con.execute("UPDATE documents SET deleted_at=? WHERE id=?", (time.time(), doc_id))
    return {"ok": True}


@app.post("/api/documents/{doc_id}/restore")
def restore_document(doc_id: str):
    _doc(doc_id)
    with db.db() as con:
        con.execute("UPDATE documents SET deleted_at=NULL WHERE id=?", (doc_id,))
    return _doc(doc_id)


@app.get("/api/documents/{doc_id}/pdf")
def get_pdf(doc_id: str):
    d = _doc(doc_id)
    return FileResponse(pdf_path(doc_id), media_type="application/pdf", filename=d["filename"],
                        content_disposition_type="inline")


@app.get("/api/documents/{doc_id}/thumb")
def get_thumb(doc_id: str):
    p = thumb_path(doc_id)
    if not p.exists():
        raise HTTPException(404, "No thumbnail")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


# ---------------------------------------------------------------- jobs

class JobCreate(BaseModel):
    engine: str = "auto"
    pages: list[int] | None = None
    replace: bool = True


@app.post("/api/documents/{doc_id}/jobs")
def start_job(doc_id: str, body: JobCreate):
    d = _doc(doc_id)
    try:
        return jobs.create_job(d, body.engine, body.pages, body.replace)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with db.db() as con:
        j = db.row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if not j:
        raise HTTPException(404, "Job not found")
    return j


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    get_job(job_id)
    jobs.cancel(job_id)
    return {"ok": True}


class RegionExtract(BaseModel):
    page: int = Field(ge=1)
    bbox: list[float] | None = Field(None, min_length=4, max_length=4)  # points, top-left origin
    engine: str = "auto"
    save: bool = True


@app.post("/api/documents/{doc_id}/extract")
async def extract_region(doc_id: str, body: RegionExtract):
    d = _doc(doc_id)
    if body.page > d["page_count"]:
        raise HTTPException(400, "Page out of range")
    if body.engine not in ENGINES or not ENGINES[body.engine].available:
        raise HTTPException(400, f"Engine not available: {body.engine}")
    clip = None
    if body.bbox:
        x0, y0, x1, y1 = body.bbox
        clip = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def run():
        pdf = open_pdf(pdf_path(doc_id))
        res = extract_page(pdf, body.page, body.engine, clip)
        if res.get("error"):
            raise HTTPException(500, res["error"])
        if clip:
            for t in res["tables"]:  # the drawn region is the user's table boundary
                t.bbox = clip if len(res["tables"]) == 1 else t.bbox
        if not body.save:
            return {"tables": [t.to_dict() | {"page": body.page} for t in res["tables"]], "ms": res["ms"]}
        with db.db() as con:
            con.execute("BEGIN")
            ids = jobs.save_tables(con, doc_id, body.page, res["tables"], job_id=None,
                                   origin="region" if clip else "job", replace=not clip)
            con.execute("UPDATE documents SET updated_at=? WHERE id=?", (time.time(), doc_id))
            con.execute("COMMIT")
        return {"tables": [_table(i) for i in ids], "ms": res["ms"]}

    return await run_in_threadpool(run)


# ---------------------------------------------------------------- backup / restore (whole library)

@app.get("/api/backup/info")
def backup_info():
    return backup.info()


@app.get("/api/backup")
async def backup_download(pdfs: bool = True):
    """The whole library as one .zip (database + PDFs; pdfs=false: only the data)."""
    name = backup.backup_name() if pdfs else backup.backup_name().replace(".zip", "-data-only.zip")
    out = backup.BACKUPS_DIR / f".download-{db.new_id()}.zip"
    await run_in_threadpool(backup.export_zip, out, pdfs=pdfs)
    return FileResponse(out, media_type="application/zip", filename=name,
                        background=BackgroundTask(lambda: out.unlink(missing_ok=True)))


@app.post("/api/backup/restore")
async def backup_restore(file: UploadFile = File(...), mode: Literal["merge", "replace"] = Form("merge")):
    backup.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = backup.BACKUPS_DIR / f".upload-{db.new_id()}.zip"
    try:
        with open(tmp, "wb") as f:
            while chunk := await file.read(4 * 1024 * 1024):
                f.write(chunk)
        return await run_in_threadpool(backup.import_zip, tmp, mode)
    except backup.BackupError as e:
        raise HTTPException(400, str(e)) from e
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- frontend (production build)

if FRONTEND_DIST.exists():
    app.mount("/_astro", StaticFiles(directory=FRONTEND_DIST / "_astro"), name="astro") if (
        FRONTEND_DIST / "_astro").exists() else None

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (FRONTEND_DIST / path).resolve()
        if FRONTEND_DIST.resolve() in f.parents or f == FRONTEND_DIST.resolve():
            for cand in (f, f / "index.html", Path(str(f) + ".html")):
                if cand.is_file():
                    return FileResponse(cand)
        return FileResponse(FRONTEND_DIST / "index.html")
