"""Startup validation: a deployment that cannot work refuses to start."""
from __future__ import annotations

import pytest

from backend.config import ConfigError, WebSettings


def test_slot_tokens_are_loaded_once_at_startup(tmp_path):
    token_file = tmp_path / "slot-1.token"
    token_file.write_text("abc123\n", encoding="utf-8")
    settings = WebSettings.from_env({
        "MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "1",
        "MCP_BRIDGE_SLOT_TOKEN_FILE_1": str(token_file),
        "MCP_BRIDGE_SLOT_TOKEN_2": "direct",
    })
    assert settings.slot_token_required is True
    assert dict(settings.slot_tokens) == {"1": "abc123", "2": "direct"}
    with pytest.raises(TypeError):
        settings.slot_tokens["3"] = "x"  # read-only view: the request path cannot mutate it

    assert dict(WebSettings.from_env({}).slot_tokens) == {}


@pytest.mark.parametrize("env, needle", [
    ({"MCP_BRIDGE_REQUIRE_SLOT_TOKEN": "1"}, "required but not configured"),
    ({"MCP_BRIDGE_SLOT_TOKEN_FILE_1": "/nonexistent/slot-1.token"}, "Cannot read slot token file"),
    ({"MAX_SLOTS": "many"}, "MAX_SLOTS must be an integer"),
    ({"PORT": "http"}, "PORT must be an integer"),
])
def test_bad_environment_raises_config_error(env, needle):
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env(env)
    assert needle in str(exc.value)


def test_empty_token_file_is_a_config_error(tmp_path):
    empty = tmp_path / "slot-1.token"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env({"MCP_BRIDGE_SLOT_TOKEN_FILE_1": str(empty)})
    assert "empty" in str(exc.value)


def test_app_exits_with_the_reason_instead_of_serving_500s(env, capsys):
    env.setenv("MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "1")  # demanded, none configured
    from backend.main import build_app_or_exit
    with pytest.raises(SystemExit) as exc:
        build_app_or_exit()
    assert exc.value.code == 2
    assert "cannot start: slot tokens: Slot token is required but not configured" in capsys.readouterr().err
