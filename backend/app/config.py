from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # repository root
load_dotenv(ROOT / ".env")

BPS_KEY = os.getenv("WEBAPI_BPS_KEY", "")
BPS_BASE = os.getenv("WEBAPI_BPS_BASE", "https://webapi.bps.go.id/v1/api")
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()
DOCS_DIR = DATA_DIR / "docs"
DB_PATH = DATA_DIR / "library.db"
FRONTEND_DIST = ROOT / "frontend" / "dist"
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "250"))
JOB_WORKERS = int(os.getenv("JOB_WORKERS", "2"))
JOBS_PER_USER = int(os.getenv("JOBS_PER_USER", "2"))  # documents one user may have queued / running at once

AUTH_DB_PATH = DATA_DIR / "auth.db"  # users + sessions: separate from library.db, so never in a backup

# login
ADMIN_USER = os.getenv("ADMIN_USER", "").strip()          # first admin, created at startup if missing
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "7"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip().rstrip("/")  # e.g. https://tabel-dari-pdf.statapps.dev

# requests per minute per IP for visitors who are not logged in
RATE_PUBLIC = int(os.getenv("RATE_PUBLIC", "300"))
RATE_PUBLIC_FILES = int(os.getenv("RATE_PUBLIC_FILES", "30"))  # exports (xlsx / zip / json files) and PDFs

DOCS_DIR.mkdir(parents=True, exist_ok=True)
