# revit-bridge-web: one image, Node builds the SPA, Python serves it with the API.
# No vector store, no model SDK: the host only needs revit-bridge, FastAPI and httpx.

## Stage 1: build the React frontend
FROM node:20-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

## Stage 2: resolve Python dependencies from the lock file (revit-bridge from PyPI)
FROM python:3.12-slim AS python-deps
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

## Stage 3: runtime
FROM python:3.12-slim
WORKDIR /app

COPY --from=python-deps /opt/venv /opt/venv
COPY backend/ backend/
COPY --from=frontend-build /build/dist/ frontend/dist/
COPY docker-entrypoint.sh /usr/local/bin/revit-bridge-web-entrypoint

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    HOST=0.0.0.0 \
    PORT=7860

# The entrypoint starts as root only long enough to copy root-owned secret
# files and fix the ownership of the data volume, then drops to uid 1000.
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app && \
    chmod 0555 /usr/local/bin/revit-bridge-web-entrypoint

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/health')" || exit 1

ENTRYPOINT ["/usr/local/bin/revit-bridge-web-entrypoint"]
CMD ["python", "-m", "backend.main"]
