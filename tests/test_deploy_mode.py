"""DEPLOY_MODE: a local host (the default) and a cloud host.

local: reachable from this machine only, no relay, no pairing, no device headers,
no admin password. cloud: the relay and pairing as before, and ADMIN_PASSWORD is
required. The pre-existing suite runs as a cloud host (conftest ``env``); this
file covers what differs between the two and the startup refusals.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from starlette.testclient import WebSocketDenialResponse

from backend.config import ConfigError, WebSettings, exposed_address, is_loopback
from tests.conftest import builtin_pack_names
from tests.test_api_v1 import B, spec_for

CLOUD_ONLY = {
    "error": "cloud_mode_only",
    "message": "Pairing, devices and the add-in relay exist only on a host started with "
               "DEPLOY_MODE=cloud; this host is local and talks to the add-in on this machine",
}
CONTAINER = {"REVIT_BRIDGE_WEB_CONTAINER": "1", "HOST": "0.0.0.0"}
# A browser on this machine: what a local host's request guard lets through
# (TestClient's default Host "testserver" and peer "testclient" are refused).
LOCAL = {"base_url": "http://127.0.0.1:7860", "client": ("127.0.0.1", 50000)}


# -- settings and startup refusals -------------------------------------------------

def test_local_is_the_default_and_reports_no_admin_and_no_devices():
    s = WebSettings.from_env({})
    assert (s.mode, s.cloud, s.host, s.port, s.max_devices, s.admin_password) == (
        "local", False, "127.0.0.1", 7860, 0, "")
    assert s.unused_variables == ()
    assert s.config_json() == {"apiBase": "", "wsBase": "", "features": {
        "mode": "local", "byoModel": True, "serverModel": False, "admin": False, "maxDevices": 0,
        "runModes": ["confirm", "auto"],
        "autoRun": {"max_steps": 8, "excluded_tools": ["delete_elements", "delete_selected", "delete_elements_by_category"]},
        "roles": True, "chatModes": ["auto", "single", "roles", "roles_analyst"], "chatModeDefault": "roles",
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
    s = WebSettings.from_env({"MAX_DEVICES": "many", "PUBLIC_WS_BASE": "wss://relay.example/ws",
                              "CHAT_RATE_LIMIT": "5"})
    assert s.unused_variables == ("MAX_DEVICES", "PUBLIC_WS_BASE")
    assert (s.admin_password, s.max_devices, s.public_ws_base, s.chat_rate_limit) == ("", 0, "", 5)
    assert s.config_json()["features"]["admin"] is False and s.config_json()["wsBase"] == ""
    # Empty values are unset, not "given anyway" (an .env copied from .env.example).
    assert WebSettings.from_env({"ADMIN_PASSWORD": "", "MAX_DEVICES": " ", "PUBLIC_WS_BASE": "",
                                 "CORS_ORIGINS": ""}).unused_variables == ()


@pytest.mark.parametrize("password", ["secret", " x "])
def test_a_local_host_given_an_admin_password_refuses_to_start(password):
    """ADMIN_PASSWORD on a local host is a cloud deployment missing DEPLOY_MODE=cloud
    (an upgraded site behind a proxy): it fails closed instead of running open."""
    for extra in ({}, {"HOST": "0.0.0.0"}, {**CONTAINER, "BIND_ADDRESS": "127.0.0.1"}):
        with pytest.raises(ConfigError) as exc:
            WebSettings.from_env({"ADMIN_PASSWORD": password, **extra})
        assert str(exc.value) == (
            "DEPLOY_MODE=local with ADMIN_PASSWORD set looks like a cloud deployment: a local host has no "
            "password at all. Set DEPLOY_MODE=cloud to keep the password, or remove ADMIN_PASSWORD to "
            "run on this machine only")
        assert password.strip() not in str(exc.value)
    # Only blanks is no password (a cloud host refuses it as missing, a local one ignores it).
    assert WebSettings.from_env({"ADMIN_PASSWORD": "   "}).mode == "local"


def test_a_local_host_refuses_cors_origins():
    with pytest.raises(ConfigError) as exc:
        WebSettings.from_env({"CORS_ORIGINS": "https://spa.example"})
    assert str(exc.value) == ("DEPLOY_MODE=local answers its own origin only, but CORS_ORIGINS is set: remove "
                              "it, or use DEPLOY_MODE=cloud with ADMIN_PASSWORD for a split deployment")
    cloud = WebSettings.from_env({"DEPLOY_MODE": "cloud", "ADMIN_PASSWORD": "pw", "CORS_ORIGINS": "https://spa.example"})
    assert cloud.cors_origins == ("https://spa.example",)


def test_the_startup_log_says_which_mode_and_what_is_not_used(make_client, local_env, caplog):
    local_env.setenv("MAX_DEVICES", "5")
    with caplog.at_level(logging.INFO, logger="backend"):
        make_client()
    lines = [r.getMessage() for r in caplog.records if r.name == "backend"]
    assert "deploy mode: local - this machine only; no relay, no pairing, no admin password" in lines
    assert "MAX_DEVICES is set but not used: there is no device relay in local mode" in lines
    assert not [line for line in lines if line.startswith("PUBLIC_WS_BASE")]


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
    ({"DEPLOY_MODE": "local", "ADMIN_PASSWORD": "secret"},
     "cannot start: DEPLOY_MODE=local with ADMIN_PASSWORD set looks like a cloud deployment"),
    ({"DEPLOY_MODE": "local", "CORS_ORIGINS": "https://spa.example"},
     "cannot start: DEPLOY_MODE=local answers its own origin only, but CORS_ORIGINS is set"),
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

    from uvicorn.importer import import_from_string

    assert "app" not in vars(main_module)
    try:
        # Exactly what `uvicorn backend.main:app` does with its argument.
        app = import_from_string("backend.main:app")
        assert isinstance(app, fastapi.FastAPI) and app is main_module.app
        from fastapi.testclient import TestClient
        assert TestClient(app, **LOCAL).get("/health").json()["status"] == "ok"
    finally:
        vars(main_module).pop("app", None)
    with pytest.raises(AttributeError):
        main_module.no_such_name  # noqa: B018


# -- routes of a local host ------------------------------------------------------------

def test_relay_and_device_paths_answer_cloud_mode_only(make_client, local_env):
    client = make_client(**LOCAL)
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
        with client.websocket_connect(f"ws://127.0.0.1:7860{B}/ws/dev_aaaaaaaaaaaa"):
            pass
    assert denied.value.status_code == 404 and denied.value.json() == CLOUD_ONLY


def test_the_local_contract_has_no_relay_routes_and_the_cloud_one_has_them(make_client, env):
    cloud = {p for p in make_client().get("/openapi.json").json()["paths"] if "/devices" in p}
    assert cloud == {f"{B}/devices", f"{B}/devices/{{device_id}}", f"{B}/devices/pair", f"{B}/devices/redeem"}

    env.setenv("DEPLOY_MODE", "local")
    env.delenv("ADMIN_PASSWORD")
    paths = set(make_client(**LOCAL).get("/openapi.json").json()["paths"])
    assert not [p for p in paths if "/devices" in p or "/ws/" in p]
    assert {f"{B}/tools", f"{B}/snapshot", f"{B}/slots", f"{B}/service-health", "/api/logs"} <= paths


def test_cloud_mode_only_wins_over_the_spa_fallback(local_env, tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app
    from tests.test_app import _spa

    client = TestClient(create_app(frontend_dist=_spa(tmp_path)), **LOCAL)
    assert client.get(f"{B}/devices").json() == CLOUD_ONLY
    assert client.get(f"{B}/nope").json()["error"] == "not_found"
    assert "spa" in client.get("/connect").text


def test_a_local_host_publishes_no_relay(make_client, local_env):
    client = make_client(**LOCAL)
    assert client.get("/config.json").json()["features"] == {
        "mode": "local", "byoModel": True, "serverModel": False, "admin": False, "maxDevices": 0,
        "runModes": ["confirm", "auto"],
        "autoRun": {"max_steps": 8, "excluded_tools": ["delete_elements", "delete_selected", "delete_elements_by_category"]},
        "roles": False, "chatModes": ["auto", "single", "roles", "roles_analyst"], "chatModeDefault": "single",
        "slotTokenRequired": False, "maxSlots": 0,
        "models": {"analyst": None, "planner": None, "supervisor": None}, "modelsFile": False}
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
    client = make_client(**LOCAL)
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
    resp = make_client(**LOCAL).post("/api/chat", json={"message": "hi"}, headers=BOGUS_DEVICE)
    assert resp.status_code == 400 and "No model configured" in resp.json()["detail"]


def test_the_chat_on_a_cloud_host_still_checks_the_device(client):
    assert client.post("/api/chat", json={"message": "hi"}, headers=BOGUS_DEVICE).status_code == 403


# -- admin on a local host ---------------------------------------------------------------

SOLIDIFY = {"name": "count_walls", "description": "How many walls", "parameters": [],
            "code": "return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Walls).GetElementCount();"}


IMPORT_YAML = ("schema_version: 1\nname: count_doors\ndescription: How many doors\nversion: 1.0.0\n"
               "code_template: return new FilteredElementCollector(document).OfCategory(BuiltInCategory.OST_Doors)"
               ".GetElementCount();\nparameters: []\n")


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_a_local_host_changes_packs_without_a_password(make_client, local_env, token):
    client = make_client(**LOCAL)
    headers = {} if token is None else {"X-Admin-Token": token}
    solid = client.post(f"{B}/solidify", json=SOLIDIFY, headers=headers)
    assert solid.status_code == 200 and solid.json()["name"] == "count_walls"
    updated = client.put(f"{B}/tools/count_walls", json={"description": "changed"}, headers=headers)
    assert updated.status_code == 200 and updated.json()["description"] == "changed"
    imported = client.post(f"{B}/tools/import", content=IMPORT_YAML, headers={**headers, "Content-Type": "text/yaml"})
    assert imported.status_code == 200 and imported.json()["name"] == "count_doors"
    # The routes' own refusals are unchanged: the admin check was the only one lifted.
    blocked = client.put(f"{B}/tools/count_walls", json={"code_template": "System.IO.File.Delete(\"x\"); return 1;"},
                         headers=headers)
    assert blocked.status_code == 400 and blocked.json()["error"] == "blocked"
    assert client.delete(f"{B}/tools/count_walls", headers=headers).json() == {"status": "deleted", "name": "count_walls"}
    assert client.delete(f"{B}/tools/count_doors", headers=headers).json()["status"] == "deleted"
    # No password needed, and none lets a built-in pack change.
    assert client.put(f"{B}/tools/query_levels", json={"description": "x"}, headers=headers).status_code == 409
    assert client.delete(f"{B}/tools/create_beam", headers=headers).json()["error"] == "builtin_read_only"


def test_a_local_host_edits_skills_and_reads_logs_without_a_password(make_client, local_env, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    local_env.setenv("SKILLS_DIR", str(empty))
    client = make_client(**LOCAL)

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


# -- the request guard of a local host (backend/localguard.py) ---------------------------

FORWARDING = ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "CF-Connecting-IP"]
# API, static files, the SPA page, the runtime config, the health check, the contract.
EVERY_KIND_OF_PATH = ["/health", "/config.json", "/", "/task", "/assets/app.js", "/favicon.svg",
                      f"{B}/tools", f"{B}/slots", "/api/logs", "/api/skills", "/openapi.json"]


@pytest.fixture
def local_spa(local_env, tmp_path):
    """A local host serving a small SPA build; call it with TestClient options."""
    from fastapi.testclient import TestClient

    from backend.main import create_app
    from tests.test_app import _spa

    dist = _spa(tmp_path)
    return lambda **options: TestClient(create_app(frontend_dist=dist), **{**LOCAL, **options})


def refused(resp, error: str) -> bool:
    return resp.status_code == 403 and resp.json()["error"] == error and resp.json()["message"]


def test_a_browser_on_this_machine_reaches_everything(local_spa):
    client = local_spa()
    # (Host given as a header: TestClient cannot parse an IPv6 base_url.)
    for host in ("127.0.0.1:7860", "localhost:7860", "[::1]:7860", "127.0.0.1", "LOCALHOST:9999", "[::1]"):
        for path in EVERY_KIND_OF_PATH:
            assert client.get(path, headers={"Host": host}).status_code == 200, (host, path)


def test_proxy_forwarding_headers_are_refused_on_every_path(local_spa):
    """/health is guarded too: the image's HEALTHCHECK sends Host 127.0.0.1:7860 and no
    forwarding header (test_the_container_health_check_gets_through)."""
    client = local_spa()
    for header in FORWARDING:
        for path in EVERY_KIND_OF_PATH:
            resp = client.get(path, headers={header: "for=203.0.113.9" if header == "Forwarded" else "203.0.113.9"})
            assert refused(resp, "local_mode_no_proxy"), (header, path, resp.status_code)
            assert header.lower() in resp.json()["message"] and "DEPLOY_MODE=cloud" in resp.json()["message"]
        # Writes too, whatever else the request carries.
        resp = client.post(f"{B}/solidify", json=SOLIDIFY, headers={header: "x", "Origin": "http://127.0.0.1:7860"})
        assert refused(resp, "local_mode_no_proxy")
    assert "count_walls" not in {t["name"] for t in client.get(f"{B}/tools").json()}


@pytest.mark.parametrize("host", ["example.com", "attacker.example:7860", "192.168.1.20:7860", "10.0.0.5",
                                  "127.0.0.1.nip.io:7860", "localhost.attacker.example", "0.0.0.0:7860",
                                  "[::]:7860", "127.0.0.2:7860", "127.0.0.1:abc", "[::1"])
def test_a_foreign_host_name_is_refused(local_spa, host):
    """DNS rebinding: a page on attacker.example whose name now resolves to 127.0.0.1
    still sends Host: attacker.example."""
    client = local_spa()
    for path in ("/", "/config.json", "/health", f"{B}/tools"):
        resp = client.get(path, headers={"Host": host})
        assert refused(resp, "local_mode_host"), (host, path, resp.status_code)
    resp = client.delete(f"{B}/tools/create_beam", headers={"Host": host})
    assert refused(resp, "local_mode_host")
    assert "create_beam" in {t["name"] for t in client.get(f"{B}/tools").json()}


def test_no_host_header_at_all_is_refused():
    from backend.localguard import LocalModeGuard, host_name

    guard = LocalModeGuard(app=None)
    scope = {"type": "http", "method": "GET", "headers": [], "client": ("127.0.0.1", 1)}
    assert guard.refusal(scope)[0] == "local_mode_host"
    assert guard.refusal({**scope, "headers": [(b"host", b"127.0.0.1:7860")]}) is None
    assert [host_name(h) for h in ("127.0.0.1:7860", "LocalHost", "[::1]:80", "[::1", "a:b", "", ":7860")] == [
        "127.0.0.1", "localhost", "[::1]", None, None, None, None]


def test_the_peer_must_be_this_machine_outside_a_container(local_spa):
    """A server started another way (uvicorn backend.main:app --host 0.0.0.0) is not
    covered by the startup check: a client elsewhere writing Host: 127.0.0.1 is still
    refused, by its address."""
    for peer in (("192.168.1.20", 5000), ("203.0.113.9", 5000), ("testclient", 50000)):
        resp = local_spa(client=peer).get("/config.json")
        assert refused(resp, "local_mode_host"), peer
        assert resp.json()["message"] == "This host runs in local mode and answers this machine only"
    for peer in (("127.0.0.1", 5000), ("::1", 5000), ("127.0.0.5", 5000)):
        assert local_spa(client=peer).get("/config.json").status_code == 200, peer


def test_the_container_health_check_gets_through(make_client, local_env):
    """In the image the peer is Docker's gateway (not checked); the HEALTHCHECK and the
    documented address use Host 127.0.0.1:7860. No other name is accepted there either."""
    for name, value in {**CONTAINER, "BIND_ADDRESS": "127.0.0.1"}.items():
        local_env.setenv(name, value)
    client = make_client(base_url="http://127.0.0.1:7860", client=("172.17.0.1", 40000))
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/config.json").json()["features"]["mode"] == "local"
    assert client.get("/health", headers={"Host": "localhost:7860"}).status_code == 200
    assert refused(client.get("/health", headers={"Host": "host.docker.internal:7860"}), "local_mode_host")
    assert refused(client.get("/health", headers={"X-Forwarded-For": "203.0.113.9"}), "local_mode_no_proxy")


def test_the_configured_loopback_address_is_a_host_name_too(make_client, local_env):
    from backend.localguard import host_names_for

    assert host_names_for("127.0.0.1") == ("127.0.0.1", "localhost", "[::1]")
    assert host_names_for("::1") == ("127.0.0.1", "localhost", "[::1]")
    assert host_names_for("127.0.0.2") == ("127.0.0.1", "localhost", "[::1]", "127.0.0.2")
    assert host_names_for("0.0.0.0") == host_names_for("localhost") == ("127.0.0.1", "localhost", "[::1]")
    local_env.setenv("HOST", "127.0.0.2")
    client = make_client(base_url="http://127.0.0.2:7860", client=("127.0.0.2", 1))
    assert client.get("/health").status_code == 200
    assert refused(client.get("/health", headers={"Host": "127.0.0.3:7860"}), "local_mode_host")


def test_the_guard_covers_websocket_upgrades(make_client, local_env):
    client = make_client(**LOCAL)
    for headers, error in (({"X-Forwarded-For": "203.0.113.9"}, "local_mode_no_proxy"),
                           ({"Host": "attacker.example"}, "local_mode_host")):
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(f"ws://127.0.0.1:7860{B}/ws/dev_aaaaaaaaaaaa", headers=headers):
                pass
        assert denied.value.status_code == 403 and denied.value.json()["error"] == error


def test_a_cloud_host_has_no_such_guard(client):
    """Behind its proxy a cloud host sees forwarding headers and public names all the time."""
    headers = {**{h: "203.0.113.9" for h in FORWARDING}, "Host": "demo.example"}
    for path in ("/health", "/config.json", f"{B}/tools", f"{B}/slots"):
        assert client.get(path, headers=headers).status_code == 200, path
    from fastapi.testclient import TestClient

    from backend.main import create_app
    remote = TestClient(create_app(), base_url="https://demo.example", client=("203.0.113.9", 5000))
    assert remote.get(f"{B}/tools").status_code == 200
    assert remote.post(f"{B}/devices/pair", json={}, headers={"Origin": "https://demo.example"}).status_code == 200


# -- a page on another origin cannot change a local host -----------------------------------

SIMPLE_TYPES = {"text/plain": "{}", "application/x-www-form-urlencoded": "name=x&code=return+1%3B&content=y"}
SKILL = "office-layout"


def write_calls(body: str) -> list[tuple[str, str, str | None]]:
    """Every state-changing route of a local host, with ``body`` (None: the route takes none)."""
    return [
        ("POST", f"{B}/solidify", body),
        ("PUT", f"{B}/tools/query_levels", body),
        ("DELETE", f"{B}/tools/create_beam", None),
        ("POST", f"{B}/tools/import", body),
        ("POST", f"{B}/check", body),
        ("POST", f"{B}/dry-run", body),
        ("POST", "/api/skills", body),
        ("PUT", f"/api/skills/{SKILL}", body),
        ("PATCH", f"/api/skills/{SKILL}", body),
        ("DELETE", f"/api/skills/{SKILL}", None),
        ("POST", "/api/skills/import", body),
        ("POST", "/api/chat", body),
        ("POST", f"{B}/spec/confirm", body),
        ("POST", f"{B}/tools/query_levels/run", body),
        ("POST", f"{B}/execute", body),
        ("POST", f"{B}/trigger-selection", None),
        ("POST", f"{B}/evidence/ev_x/validate", None),
        ("DELETE", "/api/logs?before=2030-01-01", None),
    ]


@pytest.fixture
def local_with_a_skill(make_client, local_env, tmp_path):
    empty = tmp_path / "no-skills"
    empty.mkdir()
    local_env.setenv("SKILLS_DIR", str(empty))
    client = make_client(**LOCAL)
    assert client.post("/api/skills", json={"name": "Office layout", "content": "Desks face north."}).json()["id"] == SKILL
    return client


def nothing_changed(client) -> None:
    # Exactly the wheel's packs, each listed once: none added (count_walls, count_doors, "x" from a
    # form body, any other name) and none deleted or hidden (create_beam among them).
    names = [t["name"] for t in client.get(f"{B}/tools").json()]
    assert sorted(names) == sorted(builtin_pack_names()) and "create_beam" in names
    assert client.get(f"{B}/tools/query_levels").json()["description"] != "changed"
    skill = client.get(f"/api/skills/{SKILL}").json()
    assert (skill["content"], skill["enabled"]) == ("Desks face north.", True)
    assert [s["id"] for s in client.get("/api/skills").json()["skills"]] == [SKILL]


@pytest.mark.parametrize("origin", ["http://evil.example", "https://evil.example", "http://localhost:3000",
                                    "http://127.0.0.1:7861", "https://127.0.0.1:7860", "http://localhost:7860",
                                    "null"])
def test_a_cross_site_page_cannot_change_anything(local_with_a_skill, origin):
    """What a browser sends for a page on another origin: its Origin. Every state-changing
    route refuses it first - with the simple content types a cross-site form or fetch can
    use without a preflight, and with JSON too - and nothing changes. (A page on
    localhost:7860 is another origin than 127.0.0.1:7860 for the browser as well.)"""
    client = local_with_a_skill
    for content_type, body in {**SIMPLE_TYPES, "application/json": json.dumps(SOLIDIFY)}.items():
        for method, path, payload in write_calls(body):
            headers = {"Origin": origin, "Content-Type": content_type}
            resp = client.request(method, path, content=payload, headers=headers)
            assert refused(resp, "local_mode_origin"), (origin, content_type, method, path, resp.status_code)
    nothing_changed(client)


def test_simple_content_types_alone_do_not_protect_the_routes_without_a_body(local_with_a_skill):
    """Why the Origin check exists. Without Origin (no browser does that on these methods;
    curl on this machine does) the routes with a JSON body refuse text/plain and form
    data as 422 before any effect - but the routes without a body take the request."""
    client = local_with_a_skill
    for content_type, body in SIMPLE_TYPES.items():
        for method, path, payload in write_calls(body):
            if payload is None:
                continue
            resp = client.request(method, path, content=payload, headers={"Content-Type": content_type})
            # (The import takes YAML text, so these bodies reach it and fail as packs.)
            assert resp.status_code == 422, (content_type, method, path, resp.status_code, resp.text)
        nothing_changed(client)
    # A body-less route and the import (a pack file is text) take a simple request; only the
    # Origin check keeps another site's page from sending one.
    taken = client.request("DELETE", f"/api/skills/{SKILL}", content="x", headers={"Content-Type": "text/plain"})
    assert taken.status_code == 200 and taken.json()["deleted"] == SKILL
    pack = client.request("POST", f"{B}/tools/import", content=IMPORT_YAML, headers={"Content-Type": "text/plain"})
    assert pack.status_code == 200 and pack.json()["name"] == "count_doors"


def test_the_host_s_own_page_changes_things(local_with_a_skill):
    client = local_with_a_skill
    own = {"Origin": "http://127.0.0.1:7860"}
    assert client.post(f"{B}/solidify", json=SOLIDIFY, headers=own).json()["name"] == "count_walls"
    assert client.put(f"{B}/tools/count_walls", json={"description": "changed"}, headers=own).status_code == 200
    assert client.post(f"{B}/tools/import", content=IMPORT_YAML, headers={**own, "Content-Type": "text/yaml"}).status_code == 200
    assert client.delete(f"{B}/tools/count_walls", headers=own).json()["status"] == "deleted"
    assert client.patch(f"/api/skills/{SKILL}", json={"enabled": False}, headers=own).json()["enabled"] is False
    assert client.delete(f"/api/skills/{SKILL}", headers=own).json()["deleted"] == SKILL
    # The same page opened as localhost: its origin and its Host agree again.
    assert client.delete("/api/logs?before=2030-01-01",
                         headers={"Host": "localhost:7860", "Origin": "http://localhost:7860"}).status_code == 200
    # Reading carries no Origin check (a cross-origin page cannot read the answer: no CORS).
    assert client.get(f"{B}/tools", headers={"Origin": "http://evil.example"}).status_code == 200


def test_no_cors_answer_for_another_origin(local_with_a_skill):
    """A cross-origin PUT, PATCH or DELETE (or a JSON POST) needs a preflight first; a
    local host answers it without any Access-Control-Allow-* header, so the browser
    never sends the request, and it never lets the page read an answer."""
    client = local_with_a_skill
    for method in ("PUT", "PATCH", "DELETE", "POST"):
        pre = client.options(f"{B}/tools/create_beam", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type"})
        assert not [h for h in pre.headers if h.lower().startswith("access-control-")], (method, pre.headers)
    read = client.get("/config.json", headers={"Origin": "http://evil.example"})
    assert read.status_code == 200 and "access-control-allow-origin" not in read.headers
    nothing_changed(client)


def test_a_cloud_host_keeps_its_own_protection(make_client, env):
    """No Origin check on a cloud host: the admin token is what a cross-site page lacks."""
    from tests.conftest import FIXTURE_ADMIN_PASSWORD

    client = make_client()
    evil = {"Origin": "http://evil.example", "Content-Type": "text/plain"}
    assert client.request("POST", f"{B}/solidify", content="{}", headers=evil).status_code == 403   # admin first
    assert client.request("POST", "/api/chat", content="{}", headers=evil).status_code == 422
    assert client.request("DELETE", f"{B}/tools/create_beam", headers=evil).status_code == 403
    # With the token the request reaches the route, which keeps a built-in pack as it is.
    through = client.request("DELETE", f"{B}/tools/create_beam", headers={**evil, "X-Admin-Token": FIXTURE_ADMIN_PASSWORD})
    assert through.status_code == 409 and through.json()["error"] == "builtin_read_only"
    assert client.request("POST", f"{B}/tools/import", content=IMPORT_YAML, headers=evil).status_code == 403


# -- Sec-Fetch-Site, WebSocket Origin, ASCII ports -------------------------------------------

# What another site's page makes a browser send, on every method: GETs that reach Revit,
# body-less POSTs, the page itself, the runtime config.
FOREIGN_FETCHES = [("GET", f"{B}/snapshot"), ("GET", f"{B}/tools/create_wall/choices"),
                   ("POST", f"{B}/trigger-selection"), ("POST", f"{B}/evidence/ev_x/validate"),
                   ("POST", f"{B}/query"), ("DELETE", f"{B}/tools/create_beam"),
                   ("POST", f"{B}/check"), ("POST", f"{B}/dry-run"), ("POST", f"{B}/tools/import"), ("POST", f"{B}/choices"),
                   ("GET", f"{B}/tools/create_wall/export"),
                   ("GET", "/config.json"), ("GET", "/health"), ("GET", f"{B}/tools")]


@pytest.mark.parametrize("site", ["cross-site", "same-site", "CROSS-SITE"])
def test_another_site_s_page_is_refused_before_revit_is_touched(revit, make_client, local_env, site):
    """Sec-Fetch-Site cross-site / same-site (a page on another origin of this machine, say
    localhost:3000, is same-site) is refused on every method, with or without Origin."""
    client = make_client(**LOCAL)
    for method, path in FOREIGN_FETCHES:
        for extra in ({}, {"Origin": "http://127.0.0.1:7860"}):
            resp = client.request(method, path, headers={"Sec-Fetch-Site": site, **extra})
            assert refused(resp, "local_mode_origin"), (site, method, path, resp.status_code)
            assert f"(Sec-Fetch-Site: {site.lower()})" in resp.json()["message"]
    assert revit.requests == []                                   # nothing reached the add-in
    assert "create_beam" in {t["name"] for t in client.get(f"{B}/tools").json()}


def test_the_page_itself_and_non_browser_clients_still_reach_revit(revit, make_client, local_env):
    """same-origin (the host's own page), none (a typed address or a bookmark) and no header
    at all (curl, the bench runner) pass."""
    client = make_client(**LOCAL)
    for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {}):
        snap = client.get(f"{B}/snapshot", headers=headers)
        assert snap.status_code == 200 and snap.json()["document"]["title"] == "Project1", headers
    assert revit.requests                                          # these did reach the add-in
    own = {"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:7860"}
    assert client.delete("/api/logs?before=2030-01-01", headers=own).status_code == 200


def test_a_cloud_host_ignores_sec_fetch_site(client):
    for site in ("cross-site", "same-site"):
        assert client.get(f"{B}/tools", headers={"Sec-Fetch-Site": site}).status_code == 200
        assert client.get("/config.json", headers={"Sec-Fetch-Site": site}).status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:3000", "http://localhost:7860", "null"])
def test_a_websocket_from_another_origin_is_refused(make_client, local_env, origin):
    """Browsers apply no same-origin rule to WebSockets: the guard checks Origin there too.
    (The only local WebSocket path answers 404 behind it, which is what an own-origin or
    an Origin-less upgrade reaches.)"""
    client = make_client(**LOCAL)
    url = f"ws://127.0.0.1:7860{B}/ws/dev_aaaaaaaaaaaa"
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(url, headers={"Origin": origin}):
            pass
    assert denied.value.status_code == 403 and denied.value.json()["error"] == "local_mode_origin"
    assert "takes connections only from its own page" in denied.value.json()["message"]
    for headers in ({"Origin": "http://127.0.0.1:7860"}, {}):
        with pytest.raises(WebSocketDenialResponse) as through:
            with client.websocket_connect(url, headers=headers):
                pass
        assert through.value.status_code == 404 and through.value.json()["error"] == "cloud_mode_only"


def test_a_future_websocket_route_would_be_guarded():
    """A WebSocket route added to a local host later sits behind the same checks."""
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute
    from starlette.testclient import TestClient

    from backend.localguard import LocalModeGuard

    async def echo(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    client = TestClient(LocalModeGuard(Starlette(routes=[WebSocketRoute("/echo", echo)])), **LOCAL)
    url = "ws://127.0.0.1:7860/echo"
    for headers in ({"Origin": "http://127.0.0.1:7860", "Sec-Fetch-Site": "same-origin"}, {}):
        with client.websocket_connect(url, headers=headers) as ws:
            assert ws.receive_text() == "hi"
    for headers in ({"Origin": "http://evil.example"},
                    {"Origin": "http://127.0.0.1:7860", "Sec-Fetch-Site": "cross-site"}):
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(url, headers=headers):
                pass
        assert denied.value.status_code == 403 and denied.value.json()["error"] == "local_mode_origin"


def test_a_port_must_be_ascii_digits():
    """str.isdigit() alone takes a superscript two (a latin-1 byte on the wire), an
    Arabic-Indic three or fullwidth digits."""
    from backend.localguard import LocalModeGuard, host_name

    assert host_name("127.0.0.1:7860") == "127.0.0.1" and host_name("[::1]:0080") == "[::1]"
    for host in ("127.0.0.1:\u00b2", "127.0.0.1:7\u00b2", "localhost:\u0663", "[::1]:\uff17\uff18"):
        assert host_name(host) is None, ascii(host)
    guard = LocalModeGuard(app=None)
    scope = {"type": "http", "method": "GET", "client": ("127.0.0.1", 1), "headers": [(b"host", b"127.0.0.1:\xb2")]}
    assert guard.refusal(scope)[0] == "local_mode_host"
    assert guard.refusal({**scope, "headers": [(b"host", b"127.0.0.1:7860")]}) is None


# -- a link on another site opens the page, and nothing else -------------------------------

NAVIGATION = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document",
              "Sec-Fetch-User": "?1"}


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_link_on_another_site_opens_the_page_shell(local_spa, site):
    """Clicking http://127.0.0.1:7860 in a README or a chat tool: the SPA shell and the
    static files a navigation can name."""
    client = local_spa()
    headers = {**NAVIGATION, "Sec-Fetch-Site": site}
    for path in ("/", "/task", "/capabilities/packs"):
        page = client.get(path, headers=headers)
        assert page.status_code == 200 and "<title>spa</title>" in page.text, path
    assert client.get("/favicon.svg", headers=headers).text == "<svg/>"
    assert client.get("/assets/app.js", headers=headers).text == "console.log(1)"
    # HEAD passes the guard too; the SPA route answers GET only (405), as it always has.
    assert client.head("/", headers=headers).status_code == 405


@pytest.mark.parametrize("path", [f"{B}/snapshot", f"{B}/tools", f"{B}/tools/create_wall/export", "/api/logs", "/api",
                                  "/config.json", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/health"])
def test_the_same_navigation_to_data_is_refused(revit, make_client, local_env, path):
    client = make_client(**LOCAL)
    resp = client.get(path, headers=NAVIGATION)
    assert refused(resp, "local_mode_origin"), (path, resp.status_code)
    assert revit.requests == []


@pytest.mark.parametrize("change", [
    {"method": "POST"},                                         # a cross-site form posted as a navigation
    {"headers": {"Sec-Fetch-Dest": "iframe"}},                  # another page framing the host
    {"headers": {"Sec-Fetch-Dest": "frame"}},
    {"headers": {"Sec-Fetch-Mode": "no-cors"}},                 # <img src>, <script src>
    {"headers": {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}},   # fetch()
    {"headers": {"Sec-Fetch-Mode": None}},
])
def test_anything_but_a_page_navigation_from_another_site_is_refused(local_spa, change):
    client = local_spa()
    headers = {k: v for k, v in {**NAVIGATION, **change.get("headers", {})}.items() if v is not None}
    resp = client.request(change.get("method", "GET"), "/", headers=headers)
    assert refused(resp, "local_mode_origin"), (change, resp.status_code)


def test_the_other_checks_still_apply_to_a_page_navigation(local_spa):
    client = local_spa()
    assert refused(client.get("/", headers={**NAVIGATION, "Host": "attacker.example:7860"}), "local_mode_host")
    assert refused(client.get("/", headers={**NAVIGATION, "X-Forwarded-For": "203.0.113.9"}), "local_mode_no_proxy")
    assert refused(local_spa(client=("192.168.1.20", 5000)).get("/", headers=NAVIGATION), "local_mode_host")


def test_no_other_page_may_frame_a_local_host(local_spa):
    """Every local response says so, refusals included; a frame is refused anyway."""
    client = local_spa()
    for resp in (client.get("/"), client.get("/assets/app.js"), client.get("/config.json"),
                 client.get(f"{B}/tools"), client.get("/", headers=NAVIGATION),
                 client.get("/config.json", headers={"X-Forwarded-For": "203.0.113.9"})):
        assert resp.headers["content-security-policy"] == "frame-ancestors 'self'", resp.request.url
        assert resp.headers["x-frame-options"] == "SAMEORIGIN"


def test_a_cloud_host_sends_no_frame_policy(client):
    """Unchanged: the cloud host's pages are as before (a proxy in front may add its own)."""
    resp = client.get("/config.json")
    assert "content-security-policy" not in resp.headers and "x-frame-options" not in resp.headers
    assert client.get(f"{B}/tools", headers=NAVIGATION).status_code == 200
