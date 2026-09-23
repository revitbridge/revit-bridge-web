"""Host settings, read once from environment variables.

There is no config file. Everything a deployment can change is an
environment variable (see ``.env.example``); ``/config.json`` republishes the
subset the browser needs so one frontend build works on any address.

    HOST / PORT                 bind address of the server (0.0.0.0:7860)
    CORS_ORIGINS                comma-separated origins when the SPA is hosted
                                elsewhere (empty = same-origin only)
    PUBLIC_WS_BASE              wsBase announced in /config.json when the relay is
                                reached on another address than the page ("" =
                                derived from the page origin)
    DATA_DIR                    writable directory of this host: skills/,
                                interaction_logs.db (default ./data)
    SKILLS_DIR                  optional read-only directory replacing the built-in
                                skill directory (the wheel's skills/, laid out as
                                revit-bridge/SKILL.md + references/); an empty
                                directory lists no built-in skills
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
                                server-side defaults for the model; request
                                headers X-LLM-Base-Url / X-LLM-Model / X-LLM-Key
                                take precedence and are never stored
    LLM_ALLOW_HTTP              "1" lets a browser-supplied base URL use plain http
    ADMIN_PASSWORD              enables /api/logs and skill edits (X-Admin-Token)
    MAX_DEVICES                 how many paired add-ins may be connected at once
                                (default 20)
    CHAT_RATE_LIMIT             requests per minute per IP on /api/chat and on
                                /api/v1/bridge/spec/confirm (default 30)

The package reads its own variables: REVIT_BRIDGE_DATA_DIR (its data root:
user capability packs, usage.json, the evidence ledger, auth/devices.json; the
container sets /app/data), REVIT_BRIDGE_HOST / PORT / TOKEN / TIMEOUT and
REVIT_BRIDGE_CONFIRM_TTL.

The 0.1 slot variables (MAX_SLOTS, MCP_BRIDGE_REQUIRE_SLOT_TOKEN,
MCP_BRIDGE_SLOT_TOKEN_*) are gone: a deployment that still sets one refuses to
start rather than silently dropping what its operator meant to configure.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from revit_bridge.revit.settings import env_flag

DEFAULT_PORT = 7860
DEFAULT_MAX_DEVICES = 20
DEFAULT_CHAT_RATE_LIMIT = 30

# Retired with the slot relay (phase 7): each names what replaces it, or nothing.
RETIRED_VARIABLES = {
    "MAX_SLOTS": "MAX_DEVICES",
    "MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "",
    "MCP_BRIDGE_SLOT_TOKEN": "",
}


class ConfigError(RuntimeError):
    """The environment describes a deployment that cannot run; refuse to start."""


@dataclass(frozen=True)
class WebSettings:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    cors_origins: tuple[str, ...] = ()
    public_ws_base: str = ""
    data_dir: Path = Path("data")
    skills_dir: Path | None = None
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_allow_http: bool = False
    admin_password: str = ""
    max_devices: int = DEFAULT_MAX_DEVICES
    chat_rate_limit: int = DEFAULT_CHAT_RATE_LIMIT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        """Read the environment and validate it; raise ``ConfigError`` on a
        deployment that cannot run (e.g. tokens required but none configured,
        a token file missing or empty) so the process exits with the reason
        instead of answering 500 to every request."""
        env = os.environ if env is None else env
        _refuse_retired(env)
        skills = env.get("SKILLS_DIR", "").strip()
        return cls(
            host=env.get("HOST", "").strip() or "0.0.0.0",
            port=_int(env.get("PORT"), DEFAULT_PORT, "PORT"),
            cors_origins=tuple(
                o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()
            ),
            public_ws_base=env.get("PUBLIC_WS_BASE", "").strip().rstrip("/"),
            data_dir=Path(env.get("DATA_DIR", "").strip() or "data"),
            skills_dir=Path(skills) if skills else None,
            llm_base_url=env.get("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_model=env.get("LLM_MODEL", "").strip(),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            llm_allow_http=env_flag("LLM_ALLOW_HTTP", env),
            admin_password=env.get("ADMIN_PASSWORD", ""),
            max_devices=_int(env.get("MAX_DEVICES"), DEFAULT_MAX_DEVICES, "MAX_DEVICES"),
            chat_rate_limit=_int(env.get("CHAT_RATE_LIMIT"), DEFAULT_CHAT_RATE_LIMIT, "CHAT_RATE_LIMIT"),
        )

    # -- derived paths ---------------------------------------------------------

    @property
    def user_skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def log_db_path(self) -> Path:
        return self.data_dir / "interaction_logs.db"

    @property
    def server_model_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    def config_json(self) -> dict:
        """What the SPA fetches at startup (``/config.json``).

        Served by this backend the SPA is same-origin by construction, so
        ``apiBase`` is always empty here. A split deployment (SPA on a static
        host, API elsewhere) ships its own static ``config.json`` of the same
        shape next to ``index.html``; see ``frontend/public/config.example.json``.
        """
        return {
            "apiBase": "",
            "wsBase": self.public_ws_base,
            "features": {
                "byoModel": True,
                "serverModel": self.server_model_configured,
                "admin": bool(self.admin_password),
                "maxDevices": self.max_devices,
                # 0.1 names the pre-7.3 frontend still reads; they go with the connect page.
                "slotTokenRequired": False,
                "maxSlots": self.max_devices,
            },
        }


def _refuse_retired(env: Mapping[str, str]) -> None:
    """A deployment that still configures the slot relay must not start quietly.

    An ignored ``MCP_BRIDGE_REQUIRE_SLOT_TOKEN=1`` would be a silently lost
    security expectation, and an ignored ``MAX_SLOTS`` a silently wrong limit.
    """
    for name in sorted(env):
        for retired, replacement in RETIRED_VARIABLES.items():
            if name != retired and not name.startswith(retired + "_"):
                continue
            if replacement:
                raise ConfigError(f"{name} is gone in 0.2: use {replacement} instead")
            raise ConfigError(
                f"{name} is gone in 0.2: remote add-ins are paired devices now "
                f"(POST /api/v1/bridge/devices/pair); delete the variable and pair the Revit again"
            )


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


@lru_cache(maxsize=1)
def get_settings() -> WebSettings:
    return WebSettings.from_env()


def reset_settings() -> None:
    """Drop the cached settings (tests change the environment between cases)."""
    get_settings.cache_clear()
