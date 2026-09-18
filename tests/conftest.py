"""Shared fixtures: an isolated DATA_DIR and a fresh app per test."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import capabilities, config, log_store, relay, skill_store, session


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the host at a private data dir and no Revit; returns monkeypatch."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REVIT_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("REVIT_BRIDGE_PORT", "1")  # nothing listens here
    monkeypatch.setenv("REVIT_BRIDGE_TIMEOUT", "2")
    for var in ("REVIT_BRIDGE_TOKEN", "REVIT_BRIDGE_CAPABILITIES_DIR", "SKILLS_DIR",
                "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_ALLOW_HTTP",
                "ADMIN_PASSWORD", "MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "MCP_BRIDGE_SLOT_TOKEN_1",
                "MCP_BRIDGE_SLOT_TOKEN_FILE_1", "CORS_ORIGINS",
                "PUBLIC_WS_BASE", "MAX_SLOTS", "CHAT_RATE_LIMIT"):
        monkeypatch.delenv(var, raising=False)
    _reset()
    yield monkeypatch
    _reset()


def _reset() -> None:
    from backend.api import chat
    chat._rate_hits.clear()
    config.reset_settings()
    capabilities.reset_tool_store()
    skill_store.reset_skill_store()
    log_store.reset_log_store()
    relay.reset_slot_manager()
    session._store = None


@pytest.fixture
def client(env):
    from backend.main import create_app
    with TestClient(create_app()) as tc:
        yield tc


@pytest.fixture
def make_client(env):
    """For tests that must set more environment before the app is built."""
    def build() -> TestClient:
        _reset()
        from backend.main import create_app
        return TestClient(create_app())
    return build
