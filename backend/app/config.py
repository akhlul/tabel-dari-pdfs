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

DOCS_DIR.mkdir(parents=True, exist_ok=True)
