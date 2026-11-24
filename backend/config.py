"""Host settings, read once from environment variables.

Everything a deployment can change is an environment variable (see
``.env.example``), except the model of each role, which may come from one file,
``models.yml`` (``MODELS_FILE``, read and checked at startup by
``backend.models_file``; keys stay in the environment, the file names their
variables). ``/config.json`` republishes the subset the browser needs so one
frontend build works on any address.

    DEPLOY_MODE                 "local" (default) or "cloud". local: this machine
                                only - the server must be reachable on loopback
                                only, there is no relay, no pairing and no admin
                                password (changing packs and skills and reading
                                logs needs none), and backend.localguard refuses
                                every request that does not come straight from a
                                browser on this machine. cloud: the relay, pairing
                                and ADMIN_PASSWORD, which is then required
    HOST / PORT                 bind address of the server (127.0.0.1:7860 in
                                local mode, 0.0.0.0:7860 in cloud mode)
    BIND_ADDRESS                the host address docker-compose.yml publishes the
                                port on; compose passes it into the container,
                                where local mode checks it instead of HOST
    CORS_ORIGINS                cloud only: comma-separated origins when the SPA is
                                hosted elsewhere (empty = same-origin only)
    PUBLIC_WS_BASE              cloud only: wsBase announced in /config.json when
                                the relay is reached on another address than the
                                page ("" = derived from the page origin)
    DATA_DIR                    writable directory of this host: skills/,
                                interaction_logs.db (default ./data)
    SKILLS_DIR                  optional read-only directory replacing the built-in
                                skill directory (the wheel's skills/, laid out as
                                <skill>/SKILL.md + references/); every file in it
                                is enabled unless it says otherwise; an empty
                                directory lists no built-in skills
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
                                server-side defaults for the model; request
                                headers X-LLM-Base-Url / X-LLM-Model / X-LLM-Key
                                take precedence and are never stored. LLM_API_KEY
                                is only sent to LLM_BASE_URL: a request with its
                                own X-LLM-Base-Url must bring X-LLM-Key
    LLM_ALLOW_HTTP              "1" lets a browser-supplied base URL use plain http
                                and a loopback or private address, and a models
                                file entry use http to any host (not only loopback)
    MODELS_FILE                 the models file: an endpoint, model and key variable
                                per entry, an entry per role (analyst, planner,
                                supervisor); default DATA_DIR/models.yml. No file
                                there: LLM_* and the headers as above. Given and
                                missing, or invalid: no start (backend.models_file)
    LLM_SINGLE_MODEL            "1": the chat's default mode is single - one model loop
                                (the planner's model, the whole host text, every
                                tool), as before the roles split. Default: the roles
                                (backend.roles), each with its own text, tools and
                                model
    ROLES_PLANNER_TOOLS         with the roles: "route" (default) - the planner gets
                                and may call the tools of the analyst's route only;
                                "all" - all seven on every route (a prefix that stays
                                the same for a provider's cache). For measuring both
    ROLES_ANALYST               "off" (default): the roles are planner + supervisor -
                                every message goes straight to the planner with route
                                tool; "on": the analyst routes each message first
                                (analyst + planner + supervisor)

The chat's mode (``chat_mode_default``, what a request's ``mode: auto`` runs):
``single`` with LLM_SINGLE_MODEL=1, else ``roles_analyst`` with ROLES_ANALYST=on, else
``roles``. A request may name another one (``resolve_chat_mode``): a local host honours
it, a cloud host only for an admin-authenticated request.
    ADMIN_PASSWORD             cloud only, required there: the X-Admin-Token of
                                pack and skill changes, /api/logs and the device list
    MAX_DEVICES                 cloud only: how many paired add-ins may be
                                connected at once (default 20)
    CHAT_RATE_LIMIT             requests per minute per IP on /api/chat and on
                                /api/v1/bridge/spec/confirm (default 30)

A local host refuses to start with ADMIN_PASSWORD set (that is a cloud
deployment missing DEPLOY_MODE=cloud: started as local it would have no
password at all) or with CORS_ORIGINS set (a local host answers its own origin
only). Given MAX_DEVICES or PUBLIC_WS_BASE it starts anyway and logs one line
per variable it does not use. The image sets
REVIT_BRIDGE_WEB_CONTAINER=1: inside a container the server listens on the
container's own interface, and what is reachable from outside is the published
BIND_ADDRESS, so local mode checks that one there (see ``exposed_address``).

The package reads its own variables: REVIT_BRIDGE_DATA_DIR (its data root:
user capability packs, usage.json, the evidence ledger, auth/devices.json),
REVIT_BRIDGE_CAPABILITIES_DIR (the user pack directory alone),
REVIT_BRIDGE_HOST / PORT / TOKEN / TIMEOUT and REVIT_BRIDGE_CONFIRM_TTL. This
host never sets them: started with ``uv run revit-bridge-web`` it uses the
package's per-user data root (``revit_bridge.paths.data_root()``:
%LOCALAPPDATA%/revit-bridge on Windows), the same directory the package's MCP
server uses, so the tools saved on either side are the other's too. The image
sets REVIT_BRIDGE_DATA_DIR=/app/data (the ./data volume); docker-compose.yml
has a commented mount for sharing the machine's tool directory instead.

The 0.1 slot variables (MAX_SLOTS, MCP_BRIDGE_REQUIRE_SLOT_TOKEN,
MCP_BRIDGE_SLOT_TOKEN_*) are gone: a deployment that still sets one refuses to
start rather than silently dropping what its operator meant to configure.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from revit_bridge.revit.settings import env_flag

DEFAULT_PORT = 7860
DEFAULT_MAX_DEVICES = 20
MODELS_FILE_NAME = "models.yml"           # in DATA_DIR unless MODELS_FILE names another
DEFAULT_CHAT_RATE_LIMIT = 30

LOCAL = "local"
CLOUD = "cloud"
DEPLOY_MODES = (LOCAL, CLOUD)
DEFAULT_HOST = {LOCAL: "127.0.0.1", CLOUD: "0.0.0.0"}
# Set by the image (Dockerfile): HOST is the container's own interface, and what
# other machines can reach is the address the port is published on, BIND_ADDRESS.
CONTAINER_FLAG = "REVIT_BRIDGE_WEB_CONTAINER"
# Read only in cloud mode; a local host that is given one logs that it is not used.
# (ADMIN_PASSWORD and CORS_ORIGINS are cloud only too, but a local host refuses them.)
CLOUD_ONLY_VARIABLES = {
    "MAX_DEVICES": "there is no device relay in local mode",
    "PUBLIC_WS_BASE": "there is no device relay in local mode",
}

# Retired with the slot relay (phase 7): each names what replaces it, or nothing.
RETIRED_VARIABLES = {
    "MAX_SLOTS": "MAX_DEVICES",
    "MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "",
    "MCP_BRIDGE_SLOT_TOKEN": "",
}


class ConfigError(RuntimeError):
    """The environment describes a deployment that cannot run; refuse to start."""


# The chat's modes (backend.api.chat): the one loop, planner + supervisor, analyst + planner +
# supervisor; a request's "auto" (or none) is the host's default.
SINGLE, ROLES, ROLES_ANALYST = "single", "roles", "roles_analyst"
CHAT_MODES = (SINGLE, ROLES, ROLES_ANALYST)
AUTO = "auto"
REQUEST_MODES = (AUTO, *CHAT_MODES)


@dataclass(frozen=True)
class WebSettings:
    mode: str = LOCAL
    host: str = DEFAULT_HOST[LOCAL]
    port: int = DEFAULT_PORT
    cors_origins: tuple[str, ...] = ()
    public_ws_base: str = ""
    data_dir: Path = Path("data")
    skills_dir: Path | None = None
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_allow_http: bool = False
    # The models file (backend.models_file): MODELS_FILE, or DATA_DIR/models.yml. Only a
    # file MODELS_FILE names must exist; at the default path it is optional.
    models_file: Path = Path("data") / MODELS_FILE_NAME
    models_file_explicit: bool = False
    # LLM_SINGLE_MODEL=1: the one model loop of the chat instead of the three roles.
    single_model: bool = False
    # With the roles: the planner's tools ("route" or "all", ROLES_PLANNER_TOOLS) and whether
    # the analyst routes each message by default (ROLES_ANALYST off / on).
    roles_planner_tools: str = "route"
    roles_analyst: bool = False
    admin_password: str = ""
    max_devices: int = 0
    chat_rate_limit: int = DEFAULT_CHAT_RATE_LIMIT
    # Cloud-only variables a local host was given anyway (logged once at startup).
    unused_variables: tuple[str, ...] = ()
    # Inside the image (REVIT_BRIDGE_WEB_CONTAINER): connections arrive from Docker's
    # network, not from loopback, and the address browsers use is BIND_ADDRESS.
    in_container: bool = False
    # What other machines would reach (HOST, or BIND_ADDRESS in the image): local
    # mode has checked it is loopback; the request guard accepts it as a Host name.
    exposed: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WebSettings:
        """Read the environment and validate it; raise ``ConfigError`` on a
        deployment that cannot run (a retired 0.1 slot variable still set, an
        unknown DEPLOY_MODE, a non-numeric PORT, MAX_DEVICES or CHAT_RATE_LIMIT,
        a local host other machines could reach or given ADMIN_PASSWORD or
        CORS_ORIGINS, a cloud host without ADMIN_PASSWORD) so the process exits
        with the reason instead of answering 500 to every request - or serving
        what it should not."""
        env = os.environ if env is None else env
        _refuse_retired(env)
        mode = _deploy_mode(env.get("DEPLOY_MODE"))
        cloud = mode == CLOUD
        skills = env.get("SKILLS_DIR", "").strip()
        host = env.get("HOST", "").strip() or DEFAULT_HOST[mode]
        data_dir = Path(env.get("DATA_DIR", "").strip() or "data")
        models_file = env.get("MODELS_FILE", "").strip()
        settings = cls(
            mode=mode,
            host=host,
            port=_int(env.get("PORT"), DEFAULT_PORT, "PORT"),
            cors_origins=tuple(
                o.strip() for o in env.get("CORS_ORIGINS", "").split(",") if o.strip()
            ),
            public_ws_base=env.get("PUBLIC_WS_BASE", "").strip().rstrip("/") if cloud else "",
            data_dir=data_dir,
            skills_dir=Path(skills) if skills else None,
            llm_base_url=env.get("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_model=env.get("LLM_MODEL", "").strip(),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            llm_allow_http=env_flag("LLM_ALLOW_HTTP", env),
            models_file=Path(models_file) if models_file else data_dir / MODELS_FILE_NAME,
            models_file_explicit=bool(models_file),
            single_model=env_flag("LLM_SINGLE_MODEL", env),
            roles_planner_tools=_choice(env.get("ROLES_PLANNER_TOOLS"), "ROLES_PLANNER_TOOLS", ("route", "all")),
            roles_analyst=_choice(env.get("ROLES_ANALYST"), "ROLES_ANALYST", ("off", "on")) == "on",
            admin_password=env.get("ADMIN_PASSWORD", "") if cloud else "",
            max_devices=_int(env.get("MAX_DEVICES"), DEFAULT_MAX_DEVICES, "MAX_DEVICES") if cloud else 0,
            chat_rate_limit=_int(env.get("CHAT_RATE_LIMIT"), DEFAULT_CHAT_RATE_LIMIT, "CHAT_RATE_LIMIT"),
            unused_variables=() if cloud else tuple(
                name for name in CLOUD_ONLY_VARIABLES if env.get(name, "").strip()),
            in_container=env_flag(CONTAINER_FLAG, env),
            exposed=exposed_address(env, host)[1],
        )
        if cloud:
            if not settings.admin_password.strip():
                raise ConfigError(
                    "DEPLOY_MODE=cloud needs ADMIN_PASSWORD: other machines reach a cloud host, and "
                    "changing packs and skills, reading logs and listing devices take that password. "
                    "Set it, or run DEPLOY_MODE=local on one machine")
        else:
            _refuse_local_settings(env)
            _refuse_exposed_local(env, settings.host)
        return settings

    @property
    def cloud(self) -> bool:
        """Relay, pairing and the admin password exist only on a cloud host."""
        return self.mode == CLOUD

    # -- derived paths ---------------------------------------------------------

    @property
    def user_skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def log_db_path(self) -> Path:
        return self.data_dir / "interaction_logs.db"

    def admin_token_ok(self, provided: str | None) -> bool:
        """The one ``X-Admin-Token`` check of every admin route: ``ADMIN_PASSWORD`` is set
        and ``provided`` equals it, compared in constant time."""
        if not self.admin_password or not provided:
            return False
        return hmac.compare_digest(provided.encode(), self.admin_password.encode())

    @property
    def chat_mode_default(self) -> str:
        """What the chat runs when a request names no mode (or ``auto``): ``single`` with
        LLM_SINGLE_MODEL=1, else ``roles_analyst`` with ROLES_ANALYST=on, else ``roles``."""
        if self.single_model:
            return SINGLE
        return ROLES_ANALYST if self.roles_analyst else ROLES

    def chat_modes(self, admin: bool) -> list[str]:
        """The modes a caller may pick (``auto`` first): all four on a local host, and on a cloud
        host for an admin-authenticated caller; ``auto`` alone for anyone else on a cloud host."""
        return list(REQUEST_MODES) if admin or not self.cloud else [AUTO]

    def resolve_chat_mode(self, requested: str | None, admin: bool) -> str:
        """The mode a request runs in: the one it names, when this caller may pick it
        (``chat_modes``); else - none named, ``auto``, or a visitor's choice on a cloud host,
        which is ignored - the default."""
        if requested in CHAT_MODES and requested in self.chat_modes(admin):
            return requested
        return self.chat_mode_default

    @property
    def server_model_configured(self) -> bool:
        """The server's model runs without any browser setting. A key without a server
        URL is not that: the key never goes to an endpoint the browser names."""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    def config_json(self, admin: bool = False) -> dict:
        """What the SPA fetches at startup (``/config.json``); ``admin``: the request carried
        the right ``X-Admin-Token`` (``chatModes`` lists every mode then, on a cloud host too).

        Served by this backend the SPA is same-origin by construction, so
        ``apiBase`` is always empty here. A split deployment (SPA on a static
        host, API elsewhere) ships its own static ``config.json`` of the same
        shape next to ``index.html``; see ``frontend/public/config.example.json``.
        """
        return {
            "apiBase": "",
            "wsBase": self.public_ws_base,
            "features": {
                # local: no pairing, no device list, no admin password; admin is false
                # and the device counts are 0 there (nothing to unlock, no relay).
                "mode": self.mode,
                "byoModel": True,
                "serverModel": self.server_model_configured,
                "admin": bool(self.admin_password),
                "maxDevices": self.max_devices,
                # the chat splits each message into roles by default (false: LLM_SINGLE_MODEL=1)
                "roles": not self.single_model,
                # the chat modes this caller may pick, and what auto runs here
                "chatModes": self.chat_modes(admin),
                "chatModeDefault": self.chat_mode_default,
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


def _deploy_mode(raw: str | None) -> str:
    mode = (raw or "").strip().lower() or LOCAL
    if mode not in DEPLOY_MODES:
        raise ConfigError(f"DEPLOY_MODE must be local or cloud, got {raw!r}")
    return mode


def is_loopback(address: str) -> bool:
    """``127.0.0.0/8``, ``::1`` or ``localhost``; anything else (``0.0.0.0``, a LAN
    address, another name) can be reached from other machines or cannot be told."""
    address = address.strip()
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    if address.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def exposed_address(env: Mapping[str, str], host: str) -> tuple[str, str]:
    """``(variable, value)``: the address other machines would reach this server on.

    On the machine itself that is the listen address, ``HOST``. Inside the image
    (``REVIT_BRIDGE_WEB_CONTAINER=1``) the server listens on the container's own
    interface (``0.0.0.0``), which nothing outside reaches except through the
    published port; docker-compose.yml publishes it on ``BIND_ADDRESS`` and passes
    the same value in, so that one is what counts there ("" when it was not passed).
    """
    if env_flag(CONTAINER_FLAG, env):
        return "BIND_ADDRESS", env.get("BIND_ADDRESS", "").strip()
    return "HOST", host


def _refuse_local_settings(env: Mapping[str, str]) -> None:
    """Variables that make no sense on a local host and would be dangerous to ignore.

    ADMIN_PASSWORD set means a deployment meant to be guarded - typically a cloud
    host upgraded without DEPLOY_MODE=cloud, published on loopback behind a proxy
    the loopback check cannot see. Started as local it would have no password at
    all, so it fails closed. CORS_ORIGINS would let other origins' pages call a
    host that has no password; a local host answers its own origin only.
    """
    if env.get("ADMIN_PASSWORD", "").strip():
        raise ConfigError(
            "DEPLOY_MODE=local with ADMIN_PASSWORD set looks like a cloud deployment: a local host "
            "has no password at all. Set DEPLOY_MODE=cloud to keep the password, or remove "
            "ADMIN_PASSWORD to run on this machine only")
    if env.get("CORS_ORIGINS", "").strip():
        raise ConfigError(
            "DEPLOY_MODE=local answers its own origin only, but CORS_ORIGINS is set: remove it, or "
            "use DEPLOY_MODE=cloud with ADMIN_PASSWORD for a split deployment")


def _refuse_exposed_local(env: Mapping[str, str], host: str) -> None:
    """A local host has no admin password: only this machine may reach it."""
    name, address = exposed_address(env, host)
    if not address:
        raise ConfigError(
            "DEPLOY_MODE=local in a container needs BIND_ADDRESS, the host address the port is "
            "published on: docker-compose.yml passes it in; with docker run publish with "
            "-p 127.0.0.1:7860:7860 and add -e BIND_ADDRESS=127.0.0.1")
    if not is_loopback(address):
        default = "the default" if name == "BIND_ADDRESS" else "the local default"
        raise ConfigError(
            f"DEPLOY_MODE=local serves this machine only, but {name} is {address}, which other "
            f"machines can reach: use {name}=127.0.0.1 ({default}), or DEPLOY_MODE=cloud with "
            f"ADMIN_PASSWORD to serve other machines")


def _choice(raw: str | None, name: str, choices: tuple[str, ...]) -> str:
    """One of ``choices`` (any case, surrounding space ignored); unset or empty is the first."""
    value = (raw or "").strip().lower()
    if not value:
        return choices[0]
    if value not in choices:
        raise ConfigError(f"{name} must be {' or '.join(choices)} (default {choices[0]})")
    return value


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
