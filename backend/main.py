"""FastAPI application: API + the built SPA on one port.

    python -m backend.main          # serves http://0.0.0.0:7860

Routes: ``/health``, ``/config.json``, ``/api/chat``, ``/api/skills``,
``/api/logs``, ``/api/v1/bridge/*`` and the SPA from ``frontend/dist``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api import bridge, chat, logs, skills
from backend.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_VERSION = "0.1.0"
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app(frontend_dist: Path | None = None) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="revit-bridge-web",
        version=APP_VERSION,
        description="Demo host for revit-bridge: a thin HTTP surface over the package, "
                    "a WebSocket relay for remote add-ins and a bring-your-own-model chat.",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Session-Id"],
        )

    app.include_router(chat.router)
    app.include_router(skills.router)
    app.include_router(logs.router)
    app.include_router(bridge.router)

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/config.json", tags=["meta"])
    async def config_json():
        """Runtime configuration for the SPA (read once at startup)."""
        return JSONResponse(get_settings().config_json(), headers={"Cache-Control": "no-cache"})

    dist = frontend_dist or FRONTEND_DIST
    if dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", include_in_schema=False)
        async def spa_root():
            return FileResponse(dist / "index.html")

        @app.get("/{rest:path}", include_in_schema=False)
        async def spa_fallback(rest: str = ""):
            candidate = (dist / rest).resolve()
            if rest and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

        logging.getLogger("backend").info("frontend served from %s", dist)
    else:
        logging.getLogger("backend").warning("frontend build not found at %s - API only", dist)

    return app


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
