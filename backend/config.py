"""Host settings, read once from environment variables.

There is no config file. Everything a deployment can change is an
environment variable (see ``.env.example``); ``/config.json`` republishes the
subset the browser needs so one frontend build works on any address.

    HOST / PORT                 bind address of the server (0.0.0.0:7860)
    CORS_ORIGINS                comma-separated origins when the SPA is hosted
                                elsewhere (empty = same-origin only)
    PUBLIC_API_BASE             apiBase announced in /config.json ("" = same origin)
    PUBLIC_WS_BASE              wsBase announced in /config.json ("" = derived)
    DATA_DIR                    writable directory: skills/, capabilities/,
                                interaction_logs.db (default ./data)
    SKILLS_DIR                  optional read-only skill directory (e.g. a mounted
                                checkout of the plugin's skills)
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
                                server-side defaults for the model; request
                                headers X-LLM-Base-Url / X-LLM-Model / X-LLM-Key
                                take precedence and are never stored
    LLM_ALLOW_HTTP              "1" lets a browser-supplied base URL use plain http
    ADMIN_PASSWORD              enables /api/logs and skill edits (X-Admin-Token)
    MAX_SLOTS                   number of remote add-in slots (default 5)
    CHAT_RATE_LIMIT             requests per minute per IP on /api/chat (default 30)

The package reads its own variables: REVIT_BRIDGE_HOST / PORT / TOKEN /
TIMEOUT / CAPABILITIES_DIR and MCP_BRIDGE_REQUIRE_SLOT_TOKEN /
MCP_BRIDGE_SLOT_TOKEN_N / MCP_BRIDGE_SLOT_TOKEN_FILE_N.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from revit_bridge.auth import load_slot_tokens, slot_token_required
from revit_bridge.revit.settings import env_flag

DEFAULT_PORT = 7860
DEFAULT_MAX_SLOTS = 5
DEFAULT_CHAT_RATE_LIMIT = 30


@dataclass(frozen=True)
class WebSettings:
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    cors_origins: tuple[str, ...] = ()
    public_api_base: str = ""
    public_ws_base: str = ""
    data_dir: Path = Path("data")
    skills_dir: Path | None = None
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_allow_http: bool = False
    admin_password: str = ""
    max_slots: int = DEFAULT_MAX_SLOTS
    chat_rate_limit: int = DEFAULT_CHAT_RATE_LIMIT
    slot_token_required: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        env = os.environ if env is None else env
        skills = env.get("SKILLS_DIR", "").strip()
        return cls(
            host=env.get("HOST", "").strip() or "0.0.0.0",
            port=_int(env.get("PORT"), DEFAULT_PORT, "PORT"),
            cors_origins=tuple(
                o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()
            ),
            public_api_base=env.get("PUBLIC_API_BASE", "").strip().rstrip("/"),
            public_ws_base=env.get("PUBLIC_WS_BASE", "").strip().rstrip("/"),
            data_dir=Path(env.get("DATA_DIR", "").strip() or "data"),
            skills_dir=Path(skills) if skills else None,
            llm_base_url=env.get("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_model=env.get("LLM_MODEL", "").strip(),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            llm_allow_http=env_flag("LLM_ALLOW_HTTP", env),
            admin_password=env.get("ADMIN_PASSWORD", ""),
            max_slots=_int(env.get("MAX_SLOTS"), DEFAULT_MAX_SLOTS, "MAX_SLOTS"),
            chat_rate_limit=_int(env.get("CHAT_RATE_LIMIT"), DEFAULT_CHAT_RATE_LIMIT, "CHAT_RATE_LIMIT"),
            slot_token_required=slot_token_required(env),
        )

    # -- derived paths ---------------------------------------------------------

    @property
    def user_skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def capabilities_dir(self) -> Path:
        return self.data_dir / "capabilities"

    @property
    def log_db_path(self) -> Path:
        return self.data_dir / "interaction_logs.db"

    @property
    def server_model_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    def slot_tokens(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        """Pre-shared slot tokens, resolved by the package from the environment."""
        return load_slot_tokens(env, max_slots=self.max_slots)

    def config_json(self) -> dict:
        """What the SPA fetches at startup (``/config.json``)."""
        return {
            "apiBase": self.public_api_base,
            "wsBase": self.public_ws_base,
            "features": {
                "byoModel": True,
                "serverModel": self.server_model_configured,
                "admin": bool(self.admin_password),
                "slotTokenRequired": self.slot_token_required,
                "maxSlots": self.max_slots,
            },
        }


def _int(raw: str | None, default: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@lru_cache(maxsize=1)
def get_settings() -> WebSettings:
    return WebSettings.from_env()


def reset_settings() -> None:
    """Drop the cached settings (tests change the environment between cases)."""
    get_settings.cache_clear()
