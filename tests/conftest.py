"""Shared fixtures: an isolated DATA_DIR and a fresh app per test."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config, log_store, relay, skill_store, session
from backend.api import devices


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the host at a private data dir and no Revit; returns monkeypatch."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # The package's own data root (user packs, usage.json, evidence) must not
    # be the developer's real one.
    monkeypatch.setenv("REVIT_BRIDGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens here
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
    for var in ("REVIT_BRIDGE_TOKEN", "REVIT_BRIDGE_CAPABILITIES_DIR", "REVIT_BRIDGE_EVIDENCE_DIR",
                "SKILLS_DIR",
                "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_ALLOW_HTTP",
                "ADMIN_PASSWORD", "MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "MCP_BRIDGE_SLOT_TOKEN_1",
                "MCP_BRIDGE_SLOT_TOKEN_FILE_1", "CORS_ORIGINS",
                "PUBLIC_WS_BASE", "MAX_SLOTS", "MAX_DEVICES", "CHAT_RATE_LIMIT"):
        monkeypatch.delenv(var, raising=False)
    _reset()
    yield monkeypatch
    _reset()


def _reset() -> None:
    from backend import ratelimit
    from backend.api import bridge
    ratelimit.reset_all()
    bridge.reset_bridge_state()
    config.reset_settings()
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
    """For tests that must set more environment before the app is built."""
    def build() -> TestClient:
        _reset()
        from backend.main import create_app
        return TestClient(create_app())
    return build
