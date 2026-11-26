"""Shared fixtures: an isolated DATA_DIR and a fresh app per test."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config, log_store, relay, skill_store, session
from backend.api import devices


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """No test resolves a name: the host name in a browser-supplied model endpoint answers
    one global address (the model transport is mocked, nothing connects to it). Tests of
    the endpoint check install their own answers."""
    from backend import llm

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(llm, "resolve_addresses", public)


@pytest.fixture(autouse=True)
def _fresh_usage_option_memory():
    """Which endpoints refused ``stream_options`` is process state: no test inherits it."""
    from backend import llm
    llm.reset_usage_option_memory()
    yield
    llm.reset_usage_option_memory()


# The cloud host's ADMIN_PASSWORD in every test that does not set its own (a cloud host
# refuses to start without one). Deliberately not the "hunter2" the tests send.
FIXTURE_ADMIN_PASSWORD = "fixture-admin-password"


def builtin_pack_names() -> set[str]:
    """The packs the installed revit-bridge wheel ships, read from the package's own built-in
    directory (the store's rule: ``*.yaml`` not starting with ``_``) - what a host lists,
    read-only, before anyone saves a pack. A test compares a listing with this set, so it does
    not depend on how many packs a release ships or which name sorts first."""
    from revit_bridge.paths import builtin_capabilities_dir
    names = {p.stem for p in builtin_capabilities_dir().glob("*.yaml") if p.is_file() and not p.name.startswith("_")}
    # The suite uses these as built-in packs (read-only, listed, runnable).
    assert {"create_wall", "create_beam", "query_levels"} <= names, sorted(names)
    return names


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the host at a private data dir and no Revit; returns monkeypatch.

    The host is a cloud one (DEPLOY_MODE=cloud with FIXTURE_ADMIN_PASSWORD): the
    suite written before deploy modes describes that host. Tests of a local host
    use ``local_env``. Its chat is the one model loop (LLM_SINGLE_MODEL=1): the suite
    written before the roles describes that loop, and passing unchanged is what shows
    the switch keeps it. Tests of the roles use ``roles_env``."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The package's own data root (user packs, usage.json, evidence) must not
    # be the developer's real one.
    monkeypatch.setenv("REVIT_BRIDGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens here
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
    for var in ("REVIT_BRIDGE_TOKEN", "REVIT_BRIDGE_CAPABILITIES_DIR", "REVIT_BRIDGE_EVIDENCE_DIR",
                "REVIT_BRIDGE_GUIDANCE_COMPANY", "REVIT_BRIDGE_GUIDANCE_PROJECT", "SKILLS_DIR",
                "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_ALLOW_HTTP", "LLM_MAX_TOKENS",
                "ADMIN_PASSWORD", "CAPTURE_CONTEXT", "REMOTE_MCP", "MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "MCP_BRIDGE_SLOT_TOKEN_1",
                "MCP_BRIDGE_SLOT_TOKEN_FILE_1", "CORS_ORIGINS",
                "PUBLIC_WS_BASE", "MAX_SLOTS", "MAX_DEVICES", "CHAT_RATE_LIMIT",
                "DEPLOY_MODE", "HOST", "PORT", "BIND_ADDRESS", "REVIT_BRIDGE_WEB_CONTAINER", "MODELS_FILE",
                "LLM_SINGLE_MODEL", "ROLES_PLANNER_TOOLS", "ROLES_ANALYST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEPLOY_MODE", "cloud")
    monkeypatch.setenv("ADMIN_PASSWORD", FIXTURE_ADMIN_PASSWORD)
    monkeypatch.setenv("LLM_SINGLE_MODEL", "1")
    _reset()
    yield monkeypatch
    _reset()


@pytest.fixture
def local_env(env):
    """A local host (DEPLOY_MODE=local, no ADMIN_PASSWORD); returns monkeypatch."""
    env.setenv("DEPLOY_MODE", "local")
    env.delenv("ADMIN_PASSWORD", raising=False)
    _reset()
    return env


@pytest.fixture
def roles_env(env):
    """The three roles: no LLM_SINGLE_MODEL and ROLES_ANALYST=on (mode roles_analyst, what
    tests/test_roles.py describes; the host's default is planner + supervisor, mode roles);
    returns monkeypatch."""
    env.delenv("LLM_SINGLE_MODEL", raising=False)
    env.setenv("ROLES_ANALYST", "on")
    _reset()
    return env


def _reset() -> None:
    from backend import models_file, ratelimit
    from backend.api import bridge
    ratelimit.reset_all()
    bridge.reset_bridge_state()
    config.reset_settings()
    models_file.reset_models()
    skill_store.reset_skill_store()
    log_store.reset_log_store()
    relay.reset_relay()
    devices.reset_device_store()
    session._store = None


@pytest.fixture
def revit(env):
    """A fake add-in behind a small model (tests/fake_revit.py), reachable over TCP."""
    from tests.fake_revit import FakeRevit, model_handler
    with FakeRevit(model_handler()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        yield fake


@pytest.fixture
def client(env):
    from backend.main import create_app
    with TestClient(create_app()) as tc:
        yield tc


@pytest.fixture
def device(client):
    """A paired, redeemed device: ``(headers, device_id, device_token)``.

    ``headers`` are what a browser holding the pairing's key sends; the token is
    what the add-in authenticates its WebSocket with.
    """
    paired = client.post("/api/v1/bridge/devices/pair", json={"label": "Studio PC"}).json()
    redeemed = client.post("/api/v1/bridge/devices/redeem",
                           json={"code": paired["code"], "addin_version": "0.2.0"}).json()
    headers = {"X-Device-Id": paired["device_id"], "X-Device-Key": paired["browser_key"]}
    return headers, paired["device_id"], redeemed["device_token"]


@pytest.fixture
def make_client(env):
    """For tests that must set more environment before the app is built. Keyword
    arguments go to TestClient: a local host answers only a loopback Host and peer
    (``tests.test_deploy_mode.LOCAL``), not TestClient's ``testserver``."""
    def build(**options) -> TestClient:
        _reset()
        from backend.main import create_app
        return TestClient(create_app(), **options)
    return build
