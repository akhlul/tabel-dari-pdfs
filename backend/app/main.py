from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from . import api_v1, auth, backup, bps, categories, db, geo, imports, jobs
from .common import PUBLIC_DOC_SQL, get_doc, get_doc_for, get_table
from .queue_api import router as queue_router
from .tables_api import router as tables_router
from .config import BPS_KEY, FRONTEND_DIST, MAX_PDF_MB
from .documents import PdfError, clean_tags, delete_document, guess_year, open_pdf, pdf_path, store_pdf, thumb_path
from .extract import ENGINES, extract_page, list_engines

app = FastAPI(title="Tabel dari PDF", version="1.1")
db.init()
auth.init()
app.add_middleware(auth.AuthMiddleware)  # login, roles, CSRF, rate limits: see auth.py
app.include_router(auth.router)
app.include_router(auth.users_router)
app.include_router(api_v1.router)
app.include_router(tables_router)
app.include_router(queue_router)
imports.start()  # the import queue worker


@app.exception_handler(bps.BpsError)
async def _bps_error(_, e: bps.BpsError):
    return JSONResponse({"detail": str(e)}, status_code=502)


@app.exception_handler(jobs.JobConflict)
async def _job_conflict(_, e: jobs.JobConflict):
    return JSONResponse({"detail": str(e), "job": e.job}, status_code=409)


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
        pos = imports.positions(con)
        queued = {r["source_key"]: r for r in db.rows(con.execute(
            "SELECT id, source_key, status, done, total, error FROM imports WHERE status IN "
            "('queued','downloading','processing','extracting','error') ORDER BY created_at"))}
    for it in res["items"]:
        key = f"bps:{kind}:{domain}:{it['id']}"
        it["document_id"] = keys.get(key)
        q = queued.get(key)
        it["queue"] = None if not q or it["document_id"] and q["status"] == "error" else \
            {"id": q["id"], "status": q["status"], "position": pos.get(q["id"]), "done": q["done"],
             "total": q["total"], "error": q["error"]}
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


def _import_state(imp: dict) -> dict:
    """An import of the queue, in the shape of the earlier one-off import API."""
    status = {"queued": "starting", "extracting": "done"}.get(imp["status"], imp["status"])
    doc = _doc(imp["document_id"]) if imp["document_id"] and status == "done" else None
    return {"id": imp["id"], "status": status, "done": imp["done"], "total": imp["total"], "document": doc,
            "job": None, "error": imp["error"]}


@app.post("/api/bps/import")
def bps_import(body: BpsImport):
    """Add one BPS item to the import queue (see /api/queue/imports for several)."""
    r = imports.enqueue([body.model_dump()], auth.current_user_id())[0]
    if r["existing"]:
        return {"document": _doc(r["document_id"]), "job": None, "existing": True}
    return {"import": _import_state(r["import"]), "existing": False}


@app.get("/api/bps/import/{iid}")
def import_status(iid: str):
    imp = imports.get(iid)
    if not imp:
        raise HTTPException(404, "Import not found")
    return _import_state(imp)


@app.post("/api/bps/import/{iid}/cancel")
def import_cancel(iid: str):
    if not imports.get(iid):
        raise HTTPException(404, "Import not found")
    imports.cancel(iid)
    return {"ok": True}


def _job_or_running(doc: dict, engine: str, by: str | None) -> dict | None:
    """Start finding tables after an upload; when a job already runs on the document, report that one.
    Uploads are not held to JOBS_PER_USER (several files are uploaded at once): their searches wait in line for a
    free worker (JOB_WORKERS) and show on the queue page. The limit applies to starting a search by hand."""
    try:
        return jobs.create_job(doc, engine, None, by=by, limit=False)
    except jobs.JobConflict as e:
        return e.job


# ---------------------------------------------------------------- documents

@app.get("/api/documents")
def list_documents(request: Request, q: str = ""):
    sql = """
      SELECT d.*, (SELECT COUNT(*) FROM tables t WHERE t.document_id=d.id AND t.deleted=0) AS table_count,
             (SELECT COUNT(*) FROM tables t WHERE t.document_id=d.id AND t.deleted=0 AND t.status='valid') AS valid_count,
             (SELECT status FROM jobs j WHERE j.document_id=d.id ORDER BY created_at DESC LIMIT 1) AS last_job_status
      FROM documents d {where} ORDER BY d.updated_at DESC"""
    where = PUBLIC_DOC_SQL if auth.is_public(request) else "d.deleted_at IS NULL"
    with db.db() as con:
        if q.strip():
            rs = con.execute(sql.format(where=f"WHERE {where} AND (d.title LIKE ? OR d.filename LIKE ?)"),
                             (f"%{q}%", f"%{q}%"))
        else:
            rs = con.execute(sql.format(where=f"WHERE {where}"))
        docs = db.rows(rs)
    if auth.is_public(request):  # visitors see only the valid tables, and not who did what
        for d in docs:
            d.update(table_count=d["valid_count"], last_job_status=None)
            d.pop("created_by", None)
            d.pop("updated_by", None)
    else:
        names = auth.user_names()
        for d in docs:
            auth.with_names(d, names)
    return docs


def _json_list(s: str, what: str) -> list[str] | None:
    if not s.strip():
        return None
    try:
        v = json.loads(s)
    except ValueError as e:
        raise HTTPException(422, f"{what}: not a JSON list") from e
    if not isinstance(v, list):
        raise HTTPException(422, f"{what}: not a JSON list")
    return [str(x) for x in v]


@app.post("/api/documents")
async def upload(file: UploadFile = File(...), title: str = Form(""), extract: bool = Form(False),
                 engine: str = Form("auto"), year: int | None = Form(None, ge=1900, le=2100),
                 tags: str = Form("", description='JSON list, e.g. ["IPM"]'),
                 category: str = Form("", description='JSON path, e.g. ["Sosial"]; empty = keep / uncategorised')):
    """Upload a PDF. The publication year is required: give it, or have it in the title or file name."""
    name = file.filename or "upload.pdf"
    tag_list, cat = _json_list(tags, "tags"), _json_list(category, "category")
    year = year or guess_year(title, None, name)
    if not year:
        raise HTTPException(422, f"Set the publication year of {name}.")
    data = await file.read(MAX_PDF_MB * 1024 * 1024 + 1)
    if len(data) > MAX_PDF_MB * 1024 * 1024:
        raise HTTPException(413, f"PDF larger than {MAX_PDF_MB} MB")
    by = auth.current_user_id()
    doc = await run_in_threadpool(store_pdf, data, filename=name, title=title.strip(), source="upload", by=by)
    with db.db() as con:  # the same file uploaded again keeps what it has, and gets what it lacks
        con.execute("UPDATE documents SET year=? WHERE id=? AND (year IS NULL OR source='upload' AND created_at>?)",
                    (year, doc["id"], time.time() - 5))
        if tag_list:
            con.execute("UPDATE documents SET tags=? WHERE id=?", (db.dumps(clean_tags((doc.get("tags") or []) + tag_list)), doc["id"]))
        if cat is not None and not doc.get("category"):
            con.execute("UPDATE documents SET category=? WHERE id=?", (db.dumps(categories.clean(cat)), doc["id"]))
    doc = _doc(doc["id"])
    job = _job_or_running(doc, engine, by) if extract else None
    return {"document": doc, "job": job}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, request: Request):
    d = get_doc_for(request, doc_id)
    if auth.is_public(request):
        return {k: v for k, v in d.items() if k not in ("created_by", "updated_by")} | {"jobs": []}
    with db.db() as con:
        d["jobs"] = db.rows(con.execute(
            "SELECT * FROM jobs WHERE document_id=? ORDER BY created_at DESC LIMIT 10", (doc_id,)))
    names = auth.user_names()
    for j in d["jobs"]:
        auth.with_names(j, names)
    return auth.with_names(d, names)


class DocPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=500)
    category: list[str] | None = Field(None, max_length=6)  # [] = uncategorised
    year: int | None = Field(None, ge=1900, le=2100)        # publication year
    tags: list[str] | None = Field(None, max_length=60)


@app.patch("/api/documents/{doc_id}")
def patch_document(doc_id: str, body: DocPatch, request: Request):
    d = _doc(doc_id)
    if (body.tags is not None or body.year is not None) and d["source"] != "upload":
        # editors set the year and tags of uploads; those of BPS documents only an admin
        u = auth.current_user(request)
        if not u or u["role"] != "admin":
            raise HTTPException(403, "Only an admin can change the year and tags of a document from BPS.")
    with db.db() as con:
        if body.title is not None:
            con.execute("UPDATE documents SET title=?, updated_at=?, updated_by=? WHERE id=?",
                        (body.title.strip(), time.time(), auth.current_user_id(), doc_id))
        if body.category is not None:  # moving does not count as an edit (keeps the "recent" order)
            con.execute("UPDATE documents SET category=? WHERE id=?", (db.dumps(categories.clean(body.category)), doc_id))
        if body.year is not None:
            con.execute("UPDATE documents SET year=? WHERE id=?", (body.year, doc_id))
        if body.tags is not None:
            con.execute("UPDATE documents SET tags=? WHERE id=?", (db.dumps(clean_tags(body.tags)), doc_id))
    return auth.with_names(_doc(doc_id))


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
def remove_document(doc_id: str, request: Request, purge: bool = False):
    """Soft delete (restorable for an hour, see db.init); purge=true (admin) removes files immediately."""
    _doc(doc_id)
    if purge:
        auth.need(request, "admin")
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
def get_pdf(doc_id: str, request: Request):
    d = get_doc_for(request, doc_id)
    return FileResponse(pdf_path(doc_id), media_type="application/pdf", filename=d["filename"],
                        content_disposition_type="inline")


@app.get("/api/documents/{doc_id}/thumb")
def get_thumb(doc_id: str, request: Request):
    get_doc_for(request, doc_id)
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
        return jobs.create_job(d, body.engine, body.pages, body.replace, by=auth.current_user_id())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with db.db() as con:
        j = db.row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if not j:
        raise HTTPException(404, "Job not found")
    return auth.with_names(j)


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
    by = auth.current_user_id()
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
        with db.db() as con, db.tx(con):
            if not clip and (running := jobs.active_job(con, doc_id)):  # a whole page is replaced
                raise jobs.JobConflict("Tables are being found in this document right now. Wait until that "
                                       "finishes, then extract this page again.", running)
            ids = jobs.save_tables(con, doc_id, body.page, res["tables"], job_id=None,
                                   origin="region" if clip else "job", replace=not clip, by=by)
            con.execute("UPDATE documents SET updated_at=? WHERE id=?", (time.time(), doc_id))
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
