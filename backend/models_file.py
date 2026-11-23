"""The model of each role: the optional models file, ``models.yml``.

    models:
      main:  { base_url: https://example/v1, model: some-model, api_key_env: LLM_API_KEY }
      small: { base_url: https://example/v1, model: some-small-model, api_key_env: LLM_API_KEY }
    roles:
      analyst: small
      planner: main
      supervisor: small

This is the one configuration file of the host; everything else is an
environment variable. Its path is ``MODELS_FILE``, by default ``models.yml`` in
``DATA_DIR`` (``WebSettings.models_file``).

- **No file at the default path**: nothing changes. Every role's model is the one of
  ``LLM_BASE_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY`` and the request's ``X-LLM-*``
  headers, exactly as without this module (``resolve`` calls ``backend.llm.resolve_llm``
  with the request's headers and the settings, nothing else).
- **``MODELS_FILE`` naming a path with no file**: the host refuses to start (the
  operator meant a file; running on another model instead would be a silent change).
- **A file**: read and checked once, at startup (``get_models`` from ``create_app``). Any
  problem refuses the start with a ``ConfigError`` naming the file, the field and the
  problem - never a value from the file, which could be a key someone pasted. An entry
  has ``base_url`` (``https://``, or ``http://`` to a loopback address - to any host with
  ``LLM_ALLOW_HTTP=1``; no user name, password, query or fragment), ``model``, ``api_key_env`` (the NAME of the environment
  variable that holds the key: capital letters, digits and ``_``) and optionally
  ``max_tokens`` and ``temperature``. Any other field is refused - ``api_key``, ``key``,
  ``token``, ``extra_headers`` included: keys never go in this file. Roles are exactly
  ``analyst``, ``planner`` (required) and ``supervisor``; a role left out uses the
  planner's entry; several roles may name one entry (that is "one model for all").
  YAML aliases and a key given twice in one mapping are refused.
- **Keys are read at call time**: ``api_key_env`` is looked up in the environment on
  every ``resolve``. A variable that is not set is no startup error (the browser may
  bring ``X-LLM-Key``); a call without any key is refused the way a missing
  ``LLM_API_KEY`` is (``chat.py``, 400).
- **The ``X-LLM-*`` headers keep their meaning**: they replace the entry the planner
  points to, for that request only, field by field as they replace ``LLM_*`` without
  the file - a request naming its own base URL must bring its own key, and the
  browser endpoint check (``backend.llm``: https on a public address unless
  ``LLM_ALLOW_HTTP=1``) applies to that URL as before. A role whose entry is another
  one is not affected. There are no per-role headers.
- **Base URL check**: the host checks a browser-supplied URL (``X-LLM-Base-Url``) and
  never ``LLM_BASE_URL``, the operator's own setting. The file's entries are the
  operator's settings too and are treated like ``LLM_BASE_URL``; they are parsed with
  the same parser (``backend.llm.endpoint``) and must be https, or http to loopback -
  or http to any host with ``LLM_ALLOW_HTTP=1``, the switch that lets the browser's
  endpoint use http, with the same meaning.
- An entry's ``max_tokens`` / ``temperature`` go with its endpoint and model: a request
  whose headers replace either gets ``stream_completion``'s defaults, as without the file.

``/config.json`` shows the model NAME of each role (``config_features``): no URL, no
key, no variable name.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from backend.config import ConfigError, WebSettings, get_settings
from backend.llm import LLMError, LLMSettings, endpoint, is_loopback_host, resolve_llm

_log = logging.getLogger("backend.models")

ANALYST, PLANNER, SUPERVISOR = "analyst", "planner", "supervisor"
ROLES = (ANALYST, PLANNER, SUPERVISOR)
REQUIRED_FIELDS = ("base_url", "model", "api_key_env")
OPTIONAL_FIELDS = ("max_tokens", "temperature")
MAX_FILE_BYTES = 64 * 1024
MAX_MODEL_NAME = 200                       # what llm_calls keeps of a model name
MAX_TOKENS_LIMIT = 2 ** 31 - 1
TEMPERATURE_RANGE = (0.0, 2.0)             # the OpenAI-compatible range
_ENTRY_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
# A field name of the file is shown in a message only when it cannot be a key: no digit,
# no dash, at most 32 characters. Anything else is "a field (name not shown)".
_SHOWN_NAME = re.compile(r"[a-z_]{1,32}")
# Names that could be a pasted key are not shown: an entry name longer than this is
# "<entry #N>" in messages and logs, a longer key variable name is not logged.
SHOWN_ENTRY_MAX = 16
SHOWN_ENV_MAX = 40
_SECRET_WORDS = ("key", "token", "secret", "password", "passwd", "auth", "bearer", "credential", "cookie")

NO_MODEL_FROM_FILE = (
    "No model configured: fill in Model settings on the Connect page, or set the environment "
    "variable the host's models file names for this role's model (api_key_env) on the server.")


@dataclass(frozen=True)
class ModelEntry:
    """One entry under ``models``; the key itself is never held, only its variable's name.
    ``label`` is the entry as messages and logs show it (``entry_label``)."""
    name: str
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int | None = None
    temperature: float | None = None
    label: str = ""

    def api_key(self) -> str:
        """The key, read now: the variable may be set or changed while the host runs."""
        return os.environ.get(self.api_key_env, "").strip()

    @property
    def shown_key_env(self) -> str:
        """The key variable's name as a log line shows it: a long one could be a pasted key."""
        return self.api_key_env if len(self.api_key_env) <= SHOWN_ENV_MAX else "(a long name, not shown)"


@dataclass(frozen=True)
class ModelsFile:
    path: Path
    entries: Mapping[str, ModelEntry]
    roles: Mapping[str, str]               # every role in ROLES -> the name of its entry

    def entry(self, role: str) -> ModelEntry:
        return self.entries[self.roles[role]]

    def model_names(self) -> dict[str, str]:
        return {role: self.entry(role).model for role in ROLES}


@dataclass(frozen=True)
class ModelTarget:
    """Where one model call of a role goes: ``llm`` (base URL, model, key) for
    ``stream_completion``, the models file's entry (None without the file) and the
    entry's completion options, when they apply."""
    llm: LLMSettings
    entry: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None

    @property
    def base_url(self) -> str:
        return self.llm.base_url

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def api_key(self) -> str:
        return self.llm.api_key

    @property
    def options(self) -> dict:
        """The keyword arguments for ``stream_completion`` beyond its defaults: only what
        the entry sets (nothing without the file, so the request is the same as before)."""
        return {name: value for name, value in (("max_tokens", self.max_tokens),
                                                ("temperature", self.temperature)) if value is not None}


# -- reading the file ----------------------------------------------------------------------

class ModelsYamlLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` without aliases (as ``bridge.PackYamlLoader``: ``*name`` lets a
    few bytes stand for a large or self-containing structure) and without a key given
    twice in one mapping, where the second would silently win."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(None, None, "aliases (*name) are not allowed", event.start_mark)
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        seen: set[str] = set()
        for key_node, _ in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                if key_node.value in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, "a key is given twice in one mapping", key_node.start_mark)
                seen.add(key_node.value)
        return super().construct_mapping(node, deep=deep)


def _yaml_problem(exc: Exception) -> str:
    """What PyYAML found and where, without the line itself (``str(exc)`` quotes it, and
    the line may hold a key): the problem with anything quoted in it blanked out."""
    if isinstance(exc, RecursionError):
        return "not valid YAML (nested too deep)"
    problem = getattr(exc, "problem", None) or getattr(exc, "context", None) or type(exc).__name__
    problem = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "...", str(problem)).strip()[:120]
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark is not None else ""
    return f"not valid YAML ({where}{problem})"


def _shown(name) -> str:
    """A key of the file as a message may show it: quoted when it cannot be a key value."""
    return f"'{name}'" if isinstance(name, str) and _SHOWN_NAME.fullmatch(name) else "a field (name not shown)"


def entry_label(name: str, position: int) -> str:
    """An entry as messages and logs name it: its name when it is at most
    ``SHOWN_ENTRY_MAX`` characters, else ``<entry #N>`` (N counts from 1 in the file)."""
    return name if len(name) <= SHOWN_ENTRY_MAX else f"<entry #{position}>"


def _looks_secret(name) -> bool:
    return isinstance(name, str) and any(word in name.lower() for word in _SECRET_WORDS)


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) else None


def _base_url(field: str, value, problems: list[str], allow_http: bool = False) -> str | None:
    text = _text(value)
    if not text:
        problems.append(f"{field}: must be a URL (text)")
        return None
    text = text.rstrip("/")
    try:
        parsed = httpx.URL(text)
        scheme, host, _ = endpoint(text)
    except (LLMError, httpx.InvalidURL, ValueError, UnicodeError):
        problems.append(f"{field}: not a valid URL")
        return None
    if parsed.userinfo:
        problems.append(f"{field}: must not carry a user name or password (keys never go in this file)")
        return None
    if parsed.query or parsed.fragment:
        problems.append(f"{field}: must not have a query (?...) or a fragment (#...)")
        return None
    if scheme == "https" or (scheme == "http" and (allow_http or is_loopback_host(host))):
        return text
    problems.append(f"{field}: must be https:// (http:// only to a loopback address such as "
                    f"127.0.0.1 or localhost, or to any host with LLM_ALLOW_HTTP=1)")
    return None


def _model(field: str, value, problems: list[str]) -> str | None:
    text = _text(value)
    if not text:
        problems.append(f"{field}: must be the model's name (text)")
    elif len(text) > MAX_MODEL_NAME or any(ord(c) < 32 or ord(c) == 127 for c in text):
        problems.append(f"{field}: must be at most {MAX_MODEL_NAME} characters, without control characters")
    else:
        return text
    return None


def _api_key_env(field: str, value, problems: list[str]) -> str | None:
    text = _text(value)
    if text and _ENV_NAME.fullmatch(text):
        return text
    problems.append(f"{field}: must be the NAME of an environment variable (capital letters, digits and _, "
                    f"not starting with a digit) - never the key itself")
    return None


def _max_tokens(field: str, value, problems: list[str]) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_TOKENS_LIMIT:
        return value
    problems.append(f"{field}: must be a whole number from 1 to {MAX_TOKENS_LIMIT}")
    return None


def _temperature(field: str, value, problems: list[str]) -> float | None:
    low, high = TEMPERATURE_RANGE
    # The range alone: it compares an int of any size exactly (no float conversion that
    # could overflow), and refuses NaN and the infinities.
    if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
        return float(value)
    problems.append(f"{field}: must be a number from {low:g} to {high:g}")
    return None


def _entry(name: str, position: int, raw, problems: list[str],
           allow_http: bool = False) -> ModelEntry | None:
    label = entry_label(name, position)
    prefix = f"models.{label}"
    if not isinstance(raw, dict):
        problems.append(f"{prefix}: must be a mapping with base_url, model and api_key_env")
        return None
    before = len(problems)
    for key in raw:
        if key in REQUIRED_FIELDS or key in OPTIONAL_FIELDS:
            continue
        if key == "extra_headers":
            problems.append(f"{prefix}.extra_headers: not allowed (headers could carry a key)")
        elif _looks_secret(key):
            where = f"{prefix}.{key}" if _SHOWN_NAME.fullmatch(key) else f"{prefix}: a field (name not shown)"
            problems.append(f"{where}: not allowed - keys never go in this file: put the key in an "
                            f"environment variable and name that variable in api_key_env")
        else:
            problems.append(f"{prefix}: {_shown(key)} is not a field of an entry "
                            f"(base_url, model, api_key_env, max_tokens, temperature)")
    for key in REQUIRED_FIELDS:
        if key not in raw:
            problems.append(f"{prefix}.{key}: missing")
    base_url = (_base_url(f"{prefix}.base_url", raw["base_url"], problems, allow_http)
                if "base_url" in raw else None)
    model = _model(f"{prefix}.model", raw["model"], problems) if "model" in raw else None
    key_env = _api_key_env(f"{prefix}.api_key_env", raw["api_key_env"], problems) if "api_key_env" in raw else None
    max_tokens = _max_tokens(f"{prefix}.max_tokens", raw["max_tokens"], problems) if "max_tokens" in raw else None
    temperature = (_temperature(f"{prefix}.temperature", raw["temperature"], problems)
                   if "temperature" in raw else None)
    if len(problems) > before:
        return None
    return ModelEntry(name=name, base_url=base_url, model=model, api_key_env=key_env,
                      max_tokens=max_tokens, temperature=temperature, label=label)


def parse_models(data, path: Path, *, allow_http: bool = False) -> ModelsFile:
    """The file's data (as YAML gave it) checked and read; ``ConfigError`` listing every
    problem found, each as ``<field>: <problem>``. ``allow_http`` is ``LLM_ALLOW_HTTP``:
    an http base URL to any host, not only to loopback."""
    problems: list[str] = []
    entries: dict[str, ModelEntry] = {}
    roles: dict[str, str] = {}
    if not isinstance(data, dict):
        problems.append("the file must be a mapping with models and roles (roles.planner at least)"
                        if data is None else "the top level must be a mapping with models and roles")
    else:
        for key in data:
            if key not in ("models", "roles"):
                problems.append(f"{_shown(key)}: not a field of the file (only models and roles)")
        models = data.get("models")
        if "models" not in data:
            problems.append("models: missing")
        elif not isinstance(models, dict) or not models:
            problems.append("models: must be a mapping of entry names to entries, with one entry at least")
        else:
            for position, (name, raw) in enumerate(models.items(), start=1):
                if not isinstance(name, str) or not _ENTRY_NAME.fullmatch(name):
                    problems.append(f"models: the name of entry {position} must be 1-32 letters, digits, '.', "
                                    f"'_' or '-' (not shown)")
                    continue
                entry = _entry(name, position, raw, problems, allow_http)
                if entry is not None:
                    entries[name] = entry
        named = data.get("roles")
        if "roles" not in data:
            problems.append("roles: missing (roles.planner is required)")
        elif not isinstance(named, dict):
            problems.append("roles: must be a mapping of roles (analyst, planner, supervisor) to entry names")
        else:
            for role, target in named.items():
                if role not in ROLES:
                    problems.append(f"roles: {_shown(role)} is not a role (analyst, planner, supervisor)")
                elif not isinstance(target, str) or not isinstance(models, dict) or target not in models:
                    problems.append(f"roles.{role}: must name an entry under models")
                else:
                    roles[role] = target
            if PLANNER not in named:
                problems.append("roles.planner: missing (required; a role left out uses the planner's entry)")
    if problems:
        shown = problems[:20] + ([f"... and {len(problems) - 20} more"] if len(problems) > 20 else [])
        raise ConfigError(f"models file {path}: " + "; ".join(shown))
    for role in ROLES:
        roles.setdefault(role, roles[PLANNER])
    return ModelsFile(path=path, entries=entries, roles=roles)


def load_models(path: Path, *, explicit: bool = False, allow_http: bool = False) -> ModelsFile | None:
    """The models file at ``path``, checked; None when there is no file and ``explicit`` is
    false (the default path); ``ConfigError`` for anything else that is not a valid file.
    ``allow_http``: ``LLM_ALLOW_HTTP`` (``parse_models``)."""
    where = f"models file {path}"
    try:
        if not path.exists():
            if explicit:
                raise ConfigError(f"MODELS_FILE names {path}, where there is no file: create it (see "
                                  f"models.example.yml) or unset MODELS_FILE to take the model from LLM_*")
            return None
        if not path.is_file():
            raise ConfigError(f"{where}: not a file")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ConfigError(f"{where}: larger than {MAX_FILE_BYTES // 1024} KB")
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{where}: cannot be read ({type(exc).__name__})") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"{where}: not UTF-8 text") from None
    try:
        data = yaml.load(text, Loader=ModelsYamlLoader)  # noqa: S506 - a SafeLoader subclass
    except (yaml.YAMLError, RecursionError) as exc:
        raise ConfigError(f"{where}: {_yaml_problem(exc)}") from None
    except Exception as exc:  # noqa: BLE001
        # PyYAML's constructors raise plain ValueError, KeyError, AttributeError... for a
        # scalar of the wrong form (!!int text, !!bool x, 2024-02-30, a 5000-digit int), and
        # their text quotes the value: the exception's type only.
        raise ConfigError(f"{where}: not valid YAML (a value YAML cannot read: {type(exc).__name__})") from None
    try:
        return parse_models(data, path, allow_http=allow_http)
    except ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever the data was, never its text
        raise ConfigError(f"{where}: cannot be checked ({type(exc).__name__})") from None


_loaded: tuple[WebSettings, ModelsFile | None] | None = None


def get_models() -> ModelsFile | None:
    """The models file of the current settings, read once (``create_app`` reads it first,
    so a bad file stops the start); None when there is none."""
    global _loaded
    settings = get_settings()
    if _loaded is None or _loaded[0] is not settings:
        _loaded = (settings, load_models(settings.models_file, explicit=settings.models_file_explicit,
                                         allow_http=settings.llm_allow_http))
    return _loaded[1]


def reset_models() -> None:
    global _loaded
    _loaded = None


def log_models(settings: WebSettings, models: ModelsFile | None) -> None:
    """One line for where the models come from; one per role entry whose key variable is
    not set now. Model names, entry labels and short variable names only."""
    if models is None:
        _log.info("no models file at %s: every role uses LLM_* and the request's X-LLM-* headers",
                  settings.models_file)
        return
    names = models.model_names()
    _log.info("models file %s: analyst %s, planner %s, supervisor %s", models.path,
              names[ANALYST], names[PLANNER], names[SUPERVISOR])
    for name in dict.fromkeys(models.roles[role] for role in ROLES):
        entry = models.entries[name]
        if not entry.api_key():
            _log.warning("models file entry %s: %s is not set; its roles need X-LLM-Key from the browser "
                         "until it is", entry.label, entry.shown_key_env)


# -- resolving a role ----------------------------------------------------------------------

_STARTUP = object()


async def resolve(role: str, request_overrides: Mapping[str, str], *, settings: WebSettings | None = None,
                  models: ModelsFile | None | object = _STARTUP) -> ModelTarget:
    """Where a call of ``role`` goes for this request. ``request_overrides`` are the
    request's headers (a case-insensitive mapping such as Starlette's, or lower-case keys).

    Without a models file: ``resolve_llm(request_overrides, settings)``, as before the file
    existed. With one: the role's entry stands where ``LLM_*`` stood - its key read from
    the environment now - and the headers apply when the role's entry is the planner's;
    ``LLMError`` (400) as ``resolve_llm`` raises it."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    settings = get_settings() if settings is None else settings
    if models is _STARTUP:
        models = get_models()
    if models is None:
        return ModelTarget(llm=await resolve_llm(request_overrides, settings))
    entry = models.entry(role)
    overrides = request_overrides if entry.name == models.roles[PLANNER] else {}
    defaults = dataclasses.replace(settings, llm_base_url=entry.base_url, llm_model=entry.model,
                                   llm_api_key=entry.api_key())
    llm = await resolve_llm(overrides, defaults)
    own = llm.base_url == entry.base_url and llm.model == entry.model
    return ModelTarget(llm=llm, entry=entry.name,
                       max_tokens=entry.max_tokens if own else None,
                       temperature=entry.temperature if own else None)


def config_features(settings: WebSettings, models: ModelsFile | None) -> dict:
    """``/config.json``'s ``features`` about models: ``serverModel`` (the planner runs with
    no browser setting), ``models`` (each role's model NAME - no URL, no key, no variable
    name; null for a role with none) and ``modelsFile`` (they come from the models file)."""
    if models is None:
        name = settings.llm_model or None
        return {"serverModel": settings.server_model_configured,
                "models": {role: name for role in ROLES}, "modelsFile": False}
    return {"serverModel": bool(models.entry(PLANNER).api_key()),
            "models": models.model_names(), "modelsFile": True}
