"""BPS Web API client (https://webapi.bps.go.id/documentation/).

Only the two content types that carry PDFs are used:
  publication   -> Produk > Publikasi
  pressrelease  -> Produk > Berita Resmi Statistik (BRS)
The API key never leaves the server.
"""

from __future__ import annotations

import html
import re
import time
from typing import Literal

import httpx

from .config import BPS_BASE, BPS_KEY, MAX_PDF_MB

Kind = Literal["publication", "pressrelease"]
ID_FIELD = {"publication": "pub_id", "pressrelease": "brs_id"}
UA = {"User-Agent": "tabel-dari-pdfs/1.0 (+localhost)"}


class BpsError(RuntimeError):
    pass


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30, read=120), headers=UA, follow_redirects=True)


def _get(path: str, params: dict | None = None) -> dict:
    if not BPS_KEY:
        raise BpsError("WEBAPI_BPS_KEY is not set in .env")
    url = f"{BPS_BASE}/{path}/key/{BPS_KEY}"
    try:
        with _client() as c:
            r = c.get(url, params={k: v for k, v in (params or {}).items() if v not in (None, "")})
    except httpx.HTTPError as e:
        raise BpsError(f"BPS API unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise BpsError(f"BPS API HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError as e:
        raise BpsError("BPS API returned non-JSON") from e
    if body.get("status") != "OK":
        raise BpsError(str(body.get("message") or body.get("status") or "BPS API error"))
    return body


def _strip(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def _subjects(d: dict) -> tuple[list[str], list[str]]:
    """CSA subject ids + names: publications list them (id_subject_csa / subject_csa), press releases
    have one (subcsa_id / subcsa)."""
    ids = d.get("id_subject_csa") or ([d["subcsa_id"]] if d.get("subcsa_id") else [])
    names = d.get("subject_csa") or ([d["subcsa"]] if d.get("subcsa") else [])
    if isinstance(ids, (str, int)):
        ids = [ids]
    if isinstance(names, str):
        names = [names]
    return [str(i) for i in ids if i not in (None, "")], [str(n) for n in names if n]


def _item(kind: Kind, d: dict) -> dict:
    subject_ids, subjects = _subjects(d)
    return {
        "kind": kind,
        "id": str(d.get(ID_FIELD[kind])),
        "title": _strip(d.get("title")),
        "abstract": _strip(d.get("abstract"))[:1200],
        "date": d.get("rl_date"),
        "updated": d.get("updt_date"),
        "size": d.get("size"),
        "cover": d.get("cover") or d.get("thumbnail"),
        "pdf": d.get("pdf"),
        "subject": d.get("subj") or ", ".join(subjects) or None,
        "subject_ids": subject_ids,
        "issn": d.get("issn"),
        "catalog": d.get("kat_no"),
    }


_domains: tuple[float, list[dict]] | None = None


def domains() -> list[dict]:
    global _domains
    if _domains and time.time() - _domains[0] < 86400:
        return _domains[1]
    body = _get("domain/type/all")
    items = body["data"][1] if len(body.get("data", [])) > 1 else []
    out = [{"id": d["domain_id"], "name": d["domain_name"], "url": d.get("domain_url")} for d in items]
    _domains = (time.time(), out)
    return out


def search(kind: Kind, *, domain: str = "0000", keyword: str = "", year: int | None = None,
           month: int | None = None, page: int = 1, lang: str = "ind") -> dict:
    body = _get(
        f"list/model/{kind}/domain/{domain}",
        {"keyword": keyword.strip(), "year": year, "month": month, "page": page, "lang": lang},
    )
    data = body.get("data") or []
    if body.get("data-availability") != "available" or len(data) < 2:
        return {"page": page, "pages": 0, "total": 0, "items": []}
    meta, items = data[0], data[1]
    return {
        "page": meta.get("page", page),
        "pages": meta.get("pages", 1),
        "total": meta.get("total", len(items)),
        "items": [_item(kind, d) for d in items],
    }


def detail(kind: Kind, item_id: str, *, domain: str = "0000", lang: str = "ind") -> dict:
    body = _get(f"view/domain/{domain}/model/{kind}/lang/{lang}/id/{item_id}")
    if body.get("data-availability") != "available" or not isinstance(body.get("data"), dict):
        raise BpsError("Item not found")
    return _item(kind, body["data"])


class Cancelled(BpsError):
    pass


def download(url: str, progress=None, cancelled=None) -> bytes:
    """Download a PDF. progress(done_bytes, total_bytes|None) is called per chunk; cancelled() aborts."""
    if not url or not url.startswith(("https://", "http://")):
        raise BpsError("This item has no PDF to download")
    limit = MAX_PDF_MB * 1024 * 1024
    buf = bytearray()
    try:
        with _client() as c, c.stream("GET", url) as r:
            if r.status_code != 200:
                raise BpsError(f"BPS server refused the download (HTTP {r.status_code}). Try again later.")
            total = int(r.headers.get("content-length") or 0) or None
            for chunk in r.iter_bytes():
                if cancelled and cancelled():
                    raise Cancelled("Download cancelled")
                buf.extend(chunk)
                if progress:
                    progress(len(buf), total)
                if len(buf) > limit:
                    raise BpsError(f"The PDF is larger than {MAX_PDF_MB} MB")
    except httpx.HTTPError as e:
        raise BpsError(f"Could not reach the BPS server ({type(e).__name__}). Check the connection and retry.") from e
    return bytes(buf)


def parse_size(s: str | None) -> int | None:
    """'7.5 MB' -> bytes (BPS lists sizes; used when the server sends no Content-Length)."""
    import re

    m = re.match(r"([\d.,]+)\s*(KB|MB|GB)", s or "", re.I)
    if not m:
        return None
    return int(float(m.group(1).replace(",", ".")) * {"KB": 1e3, "MB": 1e6, "GB": 1e9}[m.group(2).upper()])
