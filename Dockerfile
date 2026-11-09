# syntax=docker/dockerfile:1
# Tabel dari PDF: one container serving the API and the built web UI on :14433.
#   docker compose up -d --build        (see docker-compose.yml)
# Library data (SQLite + PDFs) lives in /data: mount a volume there.

# ---------------------------------------------------------------- web UI (Astro, static)
FROM oven/bun:1.3 AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend/ ./
RUN bunx astro build

# ---------------------------------------------------------------- API (FastAPI + pdfspine)
FROM python:3.12-slim AS app
RUN pip install --no-cache-dir uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=14433
WORKDIR /app/backend
# dependencies first: this layer is reused while only the code changes
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY --from=web /src/frontend/dist /app/frontend/dist

# run as a normal user; a new named volume takes over the ownership of /data
RUN useradd --uid 1000 --create-home app && mkdir -p /data && chown app:app /data
USER app
VOLUME /data
EXPOSE 14433
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; import os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"14433\")}/api/health', timeout=4)" || exit 1
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
