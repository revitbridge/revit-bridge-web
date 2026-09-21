"""FastAPI application: API + the built SPA on one port.

    python -m backend.main          # serves http://0.0.0.0:7860

Routes: ``/health``, ``/config.json``, ``/api/chat``, ``/api/skills``,
``/api/logs``, ``/api/v1/bridge/*`` and the SPA from ``frontend/dist``.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from revit_bridge.capabilities import ToolStore

from backend.api import bridge, chat, logs, skills
from backend.config import ConfigError, get_settings
from backend.upgrade import quarantine_legacy_packs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_VERSION = "0.1.0"
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app(frontend_dist: Path | None = None) -> FastAPI:
    settings = get_settings()
    # A data volume written by the 0.1 host holds copies of the old built-in
    # packs where the package now keeps user packs; park them aside.
    quarantine_legacy_packs(ToolStore().user_dir)
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


def build_app_or_exit() -> FastAPI:
    """Create the app, or print the configuration error and exit with 2.

    A deployment whose environment cannot work (slot tokens demanded but not
    provided, a token file missing or empty, a non-numeric port) must fail at
    startup with the reason, not answer 500 to every request.
    """
    try:
        return create_app()
    except ConfigError as exc:
        print(f"revit-bridge-web: cannot start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


app = build_app_or_exit()


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
