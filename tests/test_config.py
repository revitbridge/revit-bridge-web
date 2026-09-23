"""Startup validation: a deployment that cannot work refuses to start."""
from __future__ import annotations

import pytest

from backend.config import ConfigError, WebSettings


def test_retired_slot_variables_refuse_to_start():
    """A stale .env must fail loudly: an ignored REQUIRE_SLOT_TOKEN=1 would be a
    silently lost security expectation, an ignored MAX_SLOTS a silently wrong limit."""
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env({"MAX_SLOTS": "5"})
    assert "MAX_SLOTS" in str(exc.value) and "MAX_DEVICES" in str(exc.value)

    for name in ("MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "MCP_BRIDGE_SLOT_TOKEN_1",
                 "MCP_BRIDGE_SLOT_TOKEN_FILE_1"):
        with pytest.raises(ConfigError) as exc:
            WebSettings.from_env({name: "1"})
        assert name in str(exc.value) and "devices/pair" in str(exc.value)

    assert WebSettings.from_env({"MAX_DEVICES": "3"}).max_devices == 3
    assert WebSettings.from_env({}).max_devices == 20


@pytest.mark.parametrize("env, needle", [
    ({"MAX_DEVICES": "many"}, "MAX_DEVICES must be an integer"),
    ({"PORT": "http"}, "PORT must be an integer"),
    ({"CHAT_RATE_LIMIT": "lots"}, "CHAT_RATE_LIMIT must be an integer"),
])
def test_bad_environment_raises_config_error(env, needle):
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env(env)
    assert needle in str(exc.value)


def test_app_exits_with_the_reason_instead_of_serving_500s(env, capsys):
    env.setenv("MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "1")  # a 0.1 .env the operator forgot to clean
    from backend.main import build_app_or_exit
    with pytest.raises(SystemExit) as exc:
        build_app_or_exit()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "cannot start: MCP_BRIDGE_REQUIRE_SLOT_TOKEN is gone" in err and "devices/pair" in err
