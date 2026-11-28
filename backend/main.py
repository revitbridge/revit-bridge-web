"""FastAPI application: API + the built SPA on one port.

    uv run revit-bridge-web         # local mode: http://127.0.0.1:7860
    python -m backend.main          # the same (the image's command)

Routes: ``/health``, ``/config.json``, ``/api/chat``, ``/api/skills``,
``/api/logs``, ``/api/usage``, ``/api/v1/bridge/*`` and the SPA from ``frontend/dist``. The
relay and device routes exist on a ``DEPLOY_MODE=cloud`` host only.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette._utils import get_route_path
from starlette.requests import HTTPConnection
from starlette.routing import Route

from revit_bridge.capabilities import ToolStore

from backend.api import bridge, chat, errors, logs, skills, usage
from backend.api.settings import ModelSettingsCacheControl, router as model_settings_router
from backend.api.workflows import router as workflows_router
from backend.config import CLOUD_ONLY_VARIABLES, ConfigError, WebSettings, get_settings, reset_settings
from backend.localguard import LocalModeGuard, host_names_for, with_frame_policy
from backend.llm import BROWSER_KEYS_DISABLED, HEADER_KEY
from backend.models_file import config_features, get_models, log_models
from backend.remote_mcp import RemoteMCP
from backend.api.remote_mcp import SETTINGS as MCP_SETTINGS, router as remote_mcp_router
from backend.upgrade import quarantine_legacy_packs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

APP_VERSION = "0.2.0"
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

_log = logging.getLogger("backend")


async def reject_browser_key(request: HTTPConnection):
    if HEADER_KEY in request.headers:
        raise HTTPException(400, BROWSER_KEYS_DISABLED)


class DisabledRemoteMCP:
    """Answer before CORS preflight, body parsing, device selection or settings I/O."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = get_route_path(scope) if scope["type"] == "http" else ""
        if path in ("/mcp", MCP_SETTINGS) or path.startswith(MCP_SETTINGS + "/"):
            response = JSONResponse({"error": "remote_mcp_disabled"}, status_code=404,
                                    headers={"Cache-Control": "no-store"})
            return await response(scope, receive, with_frame_policy(send))
        await self.app(scope, receive, send)


def create_app(frontend_dist: Path | None = None) -> FastAPI:
    settings = get_settings()
    # The models file, when there is one, is read and checked now: a bad one stops the
    # start (ConfigError) before anything else is touched.
    models = get_models()
    # A data volume written by the 0.1 host holds copies of the old built-in packs
    # in DATA_DIR/capabilities, where the package keeps user packs when its data root
    # is that volume (the image); park them aside. Only there: a host started without
    # REVIT_BRIDGE_DATA_DIR shares the package's per-user directory with the MCP server
    # and other hosts, whose v0 packs are theirs and stay where they are.
    user_dir = ToolStore().user_dir
    if own_capabilities_dir(user_dir, settings):
        quarantine_legacy_packs(user_dir)
    _log.info("user tools (capability packs) are read from and saved to %s", user_dir)
    remote = RemoteMCP(settings)
    app = FastAPI(
        lifespan=remote.lifespan,
        title="revit-bridge-web",
        dependencies=[] if settings.allow_browser_keys else [Depends(reject_browser_key)],
        version=APP_VERSION,
        description="Demo host for revit-bridge: a thin HTTP surface over the package, "
                    "a WebSocket relay for remote add-ins and a bring-your-own-model chat.",
    )

    app.state.selection_watches = {}
    app.state.remote_mcp = remote
    # An exact ASGI route keeps POST /mcp at its advertised URL (no slash redirect).
    app.router.routes.append(Route("/mcp", remote, name="remote_mcp"))
    if settings.cors_origins:
        # Credentials only for an explicit origin list: with "*" the middleware would
        # echo any origin back as allowed with credentials. Nothing here needs them
        # anyway - device keys, the admin token and model keys travel as headers.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials="*" not in settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Session-Id"],
        )
    if not settings.cloud:
        # A local host has no password: only a browser on this machine, talking to it
        # directly, gets through (backend/localguard.py). CORS is never on here.
        app.add_middleware(LocalModeGuard, host_names=host_names_for(settings.exposed),
                           check_peer=not settings.in_container)
    if not settings.remote_mcp:
        app.add_middleware(DisabledRemoteMCP)

    app.add_middleware(ModelSettingsCacheControl)
    errors.install(app)
    app.include_router(chat.router)
    app.include_router(model_settings_router)
    app.include_router(remote_mcp_router)
    app.include_router(skills.router)
    app.include_router(logs.router)
    app.include_router(usage.router)
    app.include_router(bridge.router)
    app.include_router(workflows_router)
    # The relay, pairing and the device list only on a cloud host; a local host
    # answers their paths 404 cloud_mode_only.
    app.include_router(bridge.relay_router if settings.cloud else bridge.cloud_only_router)

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/config.json", tags=["meta"])
    def config_json(request: Request):
        """Runtime configuration for the SPA (read once at startup). ``features.chatModes``
        lists the chat modes the caller may pick: on a cloud host every mode only with the
        right ``X-Admin-Token``, else ``auto`` alone."""
        settings = get_settings()
        body = settings.config_json(admin=settings.admin_token_ok(request.headers.get("x-admin-token")))
        # Each role's model name (from LLM_MODEL or the models file) and where it comes from.
        body["features"].update(config_features(settings, get_models()))
        return JSONResponse(body, headers={"Cache-Control": "no-cache"})

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
            if rest == "api" or rest.startswith(("api/", "mcp/")):
                # A mistyped API address is a 404, not the SPA page with 200 (registered
                # last, so every real route, /config.json and /health match first).
                raise errors.ApiError(404, "not_found", f"No API route GET /{rest}")
            try:
                candidate = (dist / rest).resolve()
                found = bool(rest) and candidate.is_file() and candidate.is_relative_to(dist.resolve())
            except (ValueError, OSError):              # a NUL byte or a name the OS refuses
                found = False
            if found:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

        _log.info("frontend served from %s", dist)
    else:
        _log.warning("frontend build not found at %s - API only", dist)

    log_deploy_mode(settings)
    log_models(settings, models)
    _log.info("chat: default mode %s (single: LLM_SINGLE_MODEL=1, roles_analyst: ROLES_ANALYST=on; "
              "planner tools: %s); a request's mode is honoured %s", settings.chat_mode_default,
              settings.roles_planner_tools, "for an admin only" if settings.cloud else "always")
    return app


def own_capabilities_dir(user_dir: Path, settings: WebSettings) -> bool:
    """The package's user pack directory is this host's ``DATA_DIR/capabilities`` (the 0.1
    host's copy target), not a directory shared with other hosts."""
    own = settings.data_dir / "capabilities"
    try:
        return user_dir.resolve() == own.resolve()
    except OSError:
        return user_dir == own


def log_deploy_mode(settings: WebSettings) -> None:
    """One line for the mode, one per cloud-only variable a local host was given anyway."""
    if settings.cloud:
        _log.info("deploy mode: cloud - device relay, pairing and the admin password are on")
        if settings.remote_mcp:
            _log.warning("REMOTE_MCP is enabled in cloud mode. Per-device disk quotas are not enforced yet.")
        return
    _log.info("deploy mode: local - this machine only; no relay, no pairing, no admin password")
    for name in settings.unused_variables:
        _log.warning("%s is set but not used: %s", name, CLOUD_ONLY_VARIABLES[name])


def build_app_or_exit() -> FastAPI:
    """Create the app, or print the configuration error and exit with 2.

    A deployment whose environment cannot work (a retired 0.1 slot variable
    still set, a non-numeric port or limit, a local host other machines could
    reach, a cloud host without ADMIN_PASSWORD, a models file that is missing
    where MODELS_FILE points or not valid) must fail at startup with the
    reason, not answer 500 to every request.
    """
    try:
        return create_app()
    except ConfigError as exc:
        print(f"revit-bridge-web: cannot start: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def __getattr__(name: str):
    """``uvicorn backend.main:app`` still finds an app. It is built on first access, not
    on import, so ``main()`` can apply ``--host`` / ``--port`` before the environment is
    read and checked (and importing this module never exits)."""
    if name == "app":
        built = build_app_or_exit()
        globals()["app"] = built
        return built
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="revit-bridge-web",
        description="Serve the revit-bridge-web API and the built frontend (frontend/dist). "
                    "Every setting is an environment variable (see .env.example); "
                    "these options override HOST and PORT.")
    parser.add_argument("--host", help="interface to listen on (HOST; default 127.0.0.1 in local mode, "
                                       "which accepts loopback only)")
    parser.add_argument("--port", type=int, help="port to listen on (PORT; default 7860)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """``revit-bridge-web`` (``[project.scripts]``) and ``python -m backend.main``."""
    args = parse_arguments(argv)
    if args.host is not None:
        os.environ["HOST"] = args.host
    if args.port is not None:
        os.environ["PORT"] = str(args.port)
    reset_settings()
    app = build_app_or_exit()
    settings = get_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        reload=False,
        proxy_headers=True,
        # Whose X-Forwarded-For / -Proto to believe: the client address of rate limits
        # and logs. Outside a container a local reverse proxy connects from 127.0.0.1.
        # The image sets 127.0.0.1,172.16.0.0/12: with the port published on loopback
        # (BIND_ADDRESS=127.0.0.1) a proxy on the Docker host arrives from the bridge
        # gateway. When BIND_ADDRESS is not loopback, set FORWARDED_ALLOW_IPS to the
        # proxy's address, or to 127.0.0.1 with no proxy, so a visitor cannot choose
        # their own address. An explicit value always wins.
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
