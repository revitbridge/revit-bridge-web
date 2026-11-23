"""DEPLOY_MODE: a local host (the default) and a cloud host.

local: reachable from this machine only, no relay, no pairing, no device headers,
no admin password. cloud: the relay and pairing as before, and ADMIN_PASSWORD is
required. The pre-existing suite runs as a cloud host (conftest ``env``); this
file covers what differs between the two and the startup refusals.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from starlette.testclient import WebSocketDenialResponse

from backend.config import ConfigError, WebSettings, exposed_address, is_loopback
from tests.test_api_v1 import B, spec_for

CLOUD_ONLY = {
    "error": "cloud_mode_only",
    "message": "Pairing, devices and the add-in relay exist only on a host started with "
               "DEPLOY_MODE=cloud; this host is local and talks to the add-in on this machine",
}
CONTAINER = {"REVIT_BRIDGE_WEB_CONTAINER": "1", "HOST": "0.0.0.0"}


# -- settings and startup refusals -------------------------------------------------

def test_local_is_the_default_and_reports_no_admin_and_no_devices():
    s = WebSettings.from_env({})
    assert (s.mode, s.cloud, s.host, s.port, s.max_devices, s.admin_password) == (
        "local", False, "127.0.0.1", 7860, 0, "")
    assert s.unused_variables == ()
    assert s.config_json() == {"apiBase": "", "wsBase": "", "features": {
        "mode": "local", "byoModel": True, "serverModel": False, "admin": False, "maxDevices": 0,
        "slotTokenRequired": False, "maxSlots": 0}}
    assert WebSettings.from_env({"DEPLOY_MODE": " Local "}).mode == "local"
    assert WebSettings.from_env({"DEPLOY_MODE": "CLOUD", "ADMIN_PASSWORD": "pw"}).mode == "cloud"


def test_an_unknown_mode_refuses_to_start():
    with pytest.raises(ConfigError, match="DEPLOY_MODE must be local or cloud, got 'remote'"):
        WebSettings.from_env({"DEPLOY_MODE": "remote"})


@pytest.mark.parametrize("password", [None, "", "   "])
def test_cloud_without_admin_password_refuses_to_start(password):
    env = {"DEPLOY_MODE": "cloud"}
    if password is not None:
        env["ADMIN_PASSWORD"] = password
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env(env)
    assert str(exc.value).startswith("DEPLOY_MODE=cloud needs ADMIN_PASSWORD")


def test_cloud_listens_anywhere_and_reads_its_variables():
    s = WebSettings.from_env({"DEPLOY_MODE": "cloud", "ADMIN_PASSWORD": "pw", "HOST": "0.0.0.0",
                              "BIND_ADDRESS": "0.0.0.0", "REVIT_BRIDGE_WEB_CONTAINER": "1",
                              "MAX_DEVICES": "3", "PUBLIC_WS_BASE": "wss://relay.example/ws/"})
    assert (s.host, s.max_devices, s.public_ws_base, s.admin_password, s.unused_variables) == (
        "0.0.0.0", 3, "wss://relay.example/ws", "pw", ())
    assert s.config_json()["features"]["admin"] is True


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost", "LOCALHOST"])
def test_local_accepts_a_loopback_host(host):
    assert WebSettings.from_env({"HOST": host}).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "my-pc", "example.com"])
def test_local_refuses_a_host_other_machines_could_reach(host):
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env({"HOST": host})
    message = str(exc.value)
    assert message.startswith(f"DEPLOY_MODE=local serves this machine only, but HOST is {host}")
    assert "HOST=127.0.0.1" in message and "DEPLOY_MODE=cloud with ADMIN_PASSWORD" in message


def test_in_the_container_local_checks_the_published_address_not_the_listen_address():
    """The image listens on 0.0.0.0 inside the container; compose publishes the port on
    BIND_ADDRESS and passes it in. That one decides."""
    ok = WebSettings.from_env({**CONTAINER, "BIND_ADDRESS": "127.0.0.1"})
    assert (ok.mode, ok.host) == ("local", "0.0.0.0")
    assert exposed_address({**CONTAINER, "BIND_ADDRESS": "127.0.0.1"}, "0.0.0.0") == ("BIND_ADDRESS", "127.0.0.1")

    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env({**CONTAINER, "BIND_ADDRESS": "0.0.0.0"})
    assert str(exc.value).startswith("DEPLOY_MODE=local serves this machine only, but BIND_ADDRESS is 0.0.0.0")
    assert "BIND_ADDRESS=127.0.0.1 (the default)" in str(exc.value)

    # docker run without BIND_ADDRESS: what is published cannot be told, so no start.
    for missing in ({**CONTAINER}, {**CONTAINER, "BIND_ADDRESS": "  "}):
        with pytest.raises(ConfigError) as exc:
            WebSettings.from_env(missing)
        assert "needs BIND_ADDRESS" in str(exc.value) and "-e BIND_ADDRESS=127.0.0.1" in str(exc.value)


def test_outside_the_container_the_listen_address_decides():
    """On the machine itself BIND_ADDRESS publishes nothing: HOST is what others reach."""
    assert exposed_address({"BIND_ADDRESS": "0.0.0.0"}, "127.0.0.1") == ("HOST", "127.0.0.1")
    assert WebSettings.from_env({"BIND_ADDRESS": "0.0.0.0"}).host == "127.0.0.1"
    with pytest.raises(ConfigError, match="HOST is 0.0.0.0"):
        WebSettings.from_env({"HOST": "0.0.0.0", "BIND_ADDRESS": "127.0.0.1"})


def test_is_loopback():
    assert all(is_loopback(a) for a in ("127.0.0.1", "127.255.0.9", "::1", "[::1]", " localhost "))
    assert not any(is_loopback(a) for a in ("", "0.0.0.0", "::", "192.168.0.1", "localhost.example", "[::]"))


def test_a_local_host_ignores_and_names_the_cloud_variables():
    """Set anyway they are not an error - not even an unparsable MAX_DEVICES - and not used."""
    s = WebSettings.from_env({"ADMIN_PASSWORD": "secret", "MAX_DEVICES": "many",
                              "PUBLIC_WS_BASE": "wss://relay.example/ws", "CHAT_RATE_LIMIT": "5"})
    assert s.unused_variables == ("ADMIN_PASSWORD", "MAX_DEVICES", "PUBLIC_WS_BASE")
    assert (s.admin_password, s.max_devices, s.public_ws_base, s.chat_rate_limit) == ("", 0, "", 5)
    assert not s.admin_token_ok("secret")
    assert s.config_json()["features"]["admin"] is False and s.config_json()["wsBase"] == ""
    # Empty values are unset, not "given anyway" (an .env copied from .env.example).
    assert WebSettings.from_env({"ADMIN_PASSWORD": "", "MAX_DEVICES": " ", "PUBLIC_WS_BASE": ""}).unused_variables == ()


def test_the_startup_log_says_which_mode_and_what_is_not_used(make_client, local_env, caplog):
    local_env.setenv("ADMIN_PASSWORD", "secret")
    local_env.setenv("MAX_DEVICES", "5")
    with caplog.at_level(logging.INFO, logger="backend"):
        make_client()
    lines = [r.getMessage() for r in caplog.records if r.name == "backend"]
    assert "deploy mode: local - this machine only; no relay, no pairing, no admin password" in lines
    assert ("ADMIN_PASSWORD is set but not used: in local mode changing packs and skills and reading "
            "logs needs no password") in lines
    assert "MAX_DEVICES is set but not used: there is no device relay in local mode" in lines
    assert not [line for line in lines if line.startswith("PUBLIC_WS_BASE")]
    assert "secret" not in "\n".join(lines)


def test_a_cloud_host_logs_its_mode(make_client, env, caplog):
    with caplog.at_level(logging.INFO, logger="backend"):
        make_client()
    lines = [r.getMessage() for r in caplog.records if r.name == "backend"]
    assert "deploy mode: cloud - device relay, pairing and the admin password are on" in lines
    assert not [line for line in lines if "not used" in line]


@pytest.mark.parametrize("variables, needle", [
    ({"DEPLOY_MODE": "cloud"}, "cannot start: DEPLOY_MODE=cloud needs ADMIN_PASSWORD"),
    ({"DEPLOY_MODE": "local", "HOST": "0.0.0.0"}, "cannot start: DEPLOY_MODE=local serves this machine only, but HOST is 0.0.0.0"),
    ({"DEPLOY_MODE": "local", "REVIT_BRIDGE_WEB_CONTAINER": "1", "HOST": "0.0.0.0", "BIND_ADDRESS": "0.0.0.0"},
     "cannot start: DEPLOY_MODE=local serves this machine only, but BIND_ADDRESS is 0.0.0.0"),
])
def test_the_process_exits_with_the_reason(env, capsys, variables, needle):
    from backend.main import build_app_or_exit

    env.delenv("ADMIN_PASSWORD", raising=False)
    for name, value in variables.items():
        env.setenv(name, value)
    with pytest.raises(SystemExit) as exc:
        build_app_or_exit()
    assert exc.value.code == 2
    assert needle in capsys.readouterr().err


# -- the console entry ---------------------------------------------------------------

def test_the_console_entry_serves_on_loopback_and_takes_a_port(local_env, monkeypatch):
    import uvicorn

    from backend import main as main_module

    for name in ("HOST", "PORT"):        # main() writes the options to os.environ: undone after the test
        local_env.setenv(name, "")
        local_env.delenv(name)
    served: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    main_module.main([])
    assert (served["host"], served["port"]) == ("127.0.0.1", 7860)
    assert {"/health", "/config.json", "/api/v1/bridge/tools"} <= set(served["app"].openapi()["paths"])

    main_module.main(["--port", "7871"])
    assert (served["host"], served["port"]) == ("127.0.0.1", 7871)

    # --host goes through the same check as HOST.
    served.clear()
    with pytest.raises(SystemExit) as exc:
        main_module.main(["--host", "0.0.0.0"])
    assert exc.value.code == 2 and served == {}


def test_importing_main_builds_nothing_and_app_is_built_on_access(local_env):
    import fastapi

    from backend import main as main_module

    assert "app" not in vars(main_module)
    try:
        assert isinstance(main_module.app, fastapi.FastAPI)      # what `uvicorn backend.main:app` reads
        assert main_module.app is main_module.app
    finally:
        vars(main_module).pop("app", None)
    with pytest.raises(AttributeError):
        main_module.no_such_name  # noqa: B018


# -- routes of a local host ------------------------------------------------------------

def test_relay_and_device_paths_answer_cloud_mode_only(make_client, local_env):
    client = make_client()
    calls = [
        ("post", f"{B}/devices/pair", {"label": "Studio PC"}),
        ("post", f"{B}/devices/redeem", {"code": "ABCD-EFGH"}),
        ("get", f"{B}/devices", None),
        ("get", f"{B}/devices/dev_aaaaaaaaaaaa", None),
        ("delete", f"{B}/devices/dev_aaaaaaaaaaaa", None),
        ("get", f"{B}/ws/dev_aaaaaaaaaaaa", None),          # a plain request to the socket's address
    ]
    for method, path, body in calls:
        kwargs = {"json": body} if body is not None else {}
        resp = getattr(client, method)(path, **kwargs)
        assert (resp.status_code, resp.json()) == (404, CLOUD_ONLY), (method, path)

    # An add-in that tries the socket is turned away before the upgrade, with the same 404.
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(f"{B}/ws/dev_aaaaaaaaaaaa"):
            pass
    assert denied.value.status_code == 404 and denied.value.json() == CLOUD_ONLY


def test_the_local_contract_has_no_relay_routes_and_the_cloud_one_has_them(make_client, env):
    cloud = {p for p in make_client().get("/openapi.json").json()["paths"] if "/devices" in p}
    assert cloud == {f"{B}/devices", f"{B}/devices/{{device_id}}", f"{B}/devices/pair", f"{B}/devices/redeem"}

    env.setenv("DEPLOY_MODE", "local")
    env.delenv("ADMIN_PASSWORD")
    paths = set(make_client().get("/openapi.json").json()["paths"])
    assert not [p for p in paths if "/devices" in p or "/ws/" in p]
    assert {f"{B}/tools", f"{B}/snapshot", f"{B}/slots", f"{B}/service-health", "/api/logs"} <= paths


def test_cloud_mode_only_wins_over_the_spa_fallback(local_env, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app
    from tests.test_app import _spa

    client = TestClient(create_app(frontend_dist=_spa(tmp_path)))
    assert client.get(f"{B}/devices").json() == CLOUD_ONLY
    assert client.get(f"{B}/nope").json()["error"] == "not_found"
    assert "spa" in client.get("/connect").text


def test_a_local_host_publishes_no_relay(make_client, local_env):
    client = make_client()
    assert client.get("/config.json").json()["features"] == {
        "mode": "local", "byoModel": True, "serverModel": False, "admin": False, "maxDevices": 0,
        "slotTokenRequired": False, "maxSlots": 0}
    assert client.get(f"{B}/slots").json() == {"max_devices": 0, "connected": 0}
    service = client.get(f"{B}/service-health").json()
    assert service == {"status": "ok", "remote_relay_ready": False, "connected_devices": 0,
                       "websocket_endpoint": None, "devices": {"max_devices": 0, "connected": 0}}
    health = client.get(f"{B}/revit-health").json()
    assert health["revit_connected"] is False and health["mode"] == "waiting_for_revit"
    assert "paired" not in health["detail"] and health["detail"].startswith("Local add-in unreachable (")


def test_a_cloud_host_still_mentions_its_paired_devices(client):
    health = client.get(f"{B}/revit-health").json()
    assert health["detail"].endswith("; 0 paired device(s) connected")
    service = client.get(f"{B}/service-health").json()
    assert service["remote_relay_ready"] is True and service["websocket_endpoint"] == "/api/v1/bridge/ws/{device_id}"


# -- device headers on a local host ------------------------------------------------------

BOGUS_DEVICE = {"X-Device-Id": "dev_aaaaaaaaaaaa", "X-Device-Key": "not-a-key"}


def test_a_local_host_reads_no_device_header(revit, make_client, local_env):
    """A cloud host answers 403 invalid_device_key (or 422 for the 0.1 slot headers);
    a local host does not look at them and drives the add-in on this machine."""
    client = make_client()
    for headers in (BOGUS_DEVICE, {"X-Device-Id": "dev_aaaaaaaaaaaa"}, {"X-Slot-Id": "1", "X-Slot-Token": "x"}):
        assert client.get(f"{B}/tools", headers=headers).status_code == 200
        levels = client.post(f"{B}/query", json={"kind": "levels"}, headers=headers)
        assert levels.status_code == 200, levels.text
        assert [lv["name"] for lv in levels.json()["items"]] == ["L1", "L2"]     # the fake add-in over TCP
        health = client.get(f"{B}/revit-health", headers=headers).json()
        assert (health["revit_connected"], health["mode"]) == (True, "tcp")

    # Every confirmation and ledger line belongs to the local add-in.
    from backend.api.bridge import get_gate
    token = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")},
                        headers=BOGUS_DEVICE).json()["token"]
    assert get_gate().peek(token).scope == "local"
    run = client.post(f"{B}/tools/query_levels/run", json={"params": {}, "token": token}, headers=BOGUS_DEVICE)
    assert run.status_code == 200 and run.json()["success"] is True
    assert client.get(f"{B}/evidence", headers=BOGUS_DEVICE).json()[0]["scope"] == "local"


def test_the_same_headers_on_a_cloud_host_are_refused(client):
    assert client.get(f"{B}/tools", headers=BOGUS_DEVICE).status_code == 403
    assert client.get(f"{B}/tools", headers={"X-Slot-Id": "1"}).status_code == 422


def test_get_revit_client_is_the_tcp_client_on_a_local_host_whatever_the_context(revit, make_client, local_env):
    """Even with a device in the request context (as a cloud host's dependency would set
    it), a local host never hands out a relay client."""
    from revit_bridge.revit import RevitClient, RevitClientPool

    from backend.api import bridge, devices
    from backend.relay import WebSocketRevitClient

    make_client()

    async def pick():
        devices.request_device.set("dev_aaaaaaaaaaaa")
        try:
            return bridge.request_device(), bridge.current_scope(), await bridge.get_revit_client()
        finally:
            await RevitClientPool.disconnect()       # the connection belongs to this loop

    device, scope, revit_client = asyncio.run(pick())
    assert (device, scope) == (None, "local")
    assert isinstance(revit_client, RevitClient) and not isinstance(revit_client, WebSocketRevitClient)


def test_the_chat_reads_no_device_header_on_a_local_host(make_client, local_env):
    """/api/chat checks the device headers itself on a cloud host (403 for a wrong key);
    a local host goes on to the model settings (400: no model configured)."""
    resp = make_client().post("/api/chat", json={"message": "hi"}, headers=BOGUS_DEVICE)
    assert resp.status_code == 400 and "No model configured" in resp.json()["detail"]


def test_the_chat_on_a_cloud_host_still_checks_the_device(client):
    assert client.post("/api/chat", json={"message": "hi"}, headers=BOGUS_DEVICE).status_code == 403


# -- admin on a local host ---------------------------------------------------------------

SOLIDIFY = {"name": "count_walls", "description": "How many walls", "parameters": [],
            "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();"}


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_a_local_host_changes_packs_without_a_password(make_client, local_env, token):
    client = make_client()
    headers = {} if token is None else {"X-Admin-Token": token}
    updated = client.put(f"{B}/tools/query_levels", json={"description": "changed"}, headers=headers)
    assert updated.status_code == 200 and updated.json()["description"] == "changed"
    assert client.delete(f"{B}/tools/create_beam", headers=headers).json() == {"status": "deleted", "name": "create_beam"}
    solid = client.post(f"{B}/solidify", json=SOLIDIFY, headers=headers)
    assert solid.status_code == 200 and solid.json()["name"] == "count_walls"
    # The routes' own refusals are unchanged: the admin check was the only one lifted.
    blocked = client.put(f"{B}/tools/query_levels", json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"},
                         headers=headers)
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"


def test_a_local_host_edits_skills_and_reads_logs_without_a_password(make_client, local_env, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    local_env.setenv("SKILLS_DIR", str(empty))
    local_env.setenv("ADMIN_PASSWORD", "secret")          # set anyway: not used
    client = make_client()

    created = client.post("/api/skills", json={"name": "Office layout", "content": "Desks face north."})
    assert created.status_code == 200 and created.json()["id"] == "office-layout"
    assert client.patch("/api/skills/office-layout", json={"enabled": False}).json()["enabled"] is False
    assert client.put("/api/skills/office-layout", json={"content": "Desks face south."}).status_code == 200
    assert client.delete("/api/skills/office-layout").json()["deleted"] == "office-layout"

    assert client.get("/api/logs").status_code == 200
    assert client.get("/api/logs", headers={"X-Admin-Token": "wrong"}).json()["total"] == 0
    assert client.get("/api/logs/stats").status_code == 200
    assert client.get("/api/logs/verify").json() == {"valid": True}
    assert client.delete("/api/logs?before=2030-01-01").json()["deleted"] == 0


def test_a_cloud_host_still_wants_the_password(client):
    assert client.put(f"{B}/tools/query_levels", json={"description": "changed"}).status_code == 403
    assert client.get("/api/logs").status_code == 403
    assert client.post("/api/skills", json={"name": "x", "content": "y"}).status_code == 403
    assert client.get("/api/logs/verify", headers={"X-Admin-Token": "wrong"}).status_code == 403
