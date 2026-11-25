"""The mounted package, its real SDK transport and the host's paired-device boundary."""
import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backend.main import create_app
from backend.relay import get_relay
from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.test_api_v1 import B, WALL, code_spec, spec_for
from tests.test_workbench import CODE, addin, workbench_handler  # noqa: F401 - fake add-in fixture

SETTINGS = "/api/v1/settings/remote-mcp"
TRIALS = "/api/v1/bridge/remote-trials"
LOCAL = "http://127.0.0.1:7860"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25",
        "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def sdk(http, headers=None):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=http.app),
                                  base_url=http.base_url, headers=headers) as transport:
        async with streamable_http_client(str(http.base_url.join("/mcp")), http_client=transport) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def host(base_url=LOCAL, peer="127.0.0.1"):
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app, client=(peer, 5000)),
                                      base_url=base_url) as http:
            http.app = app
            yield http


async def call(session, tool_name, **args):
    reply = await session.call_tool(tool_name, args)
    assert not reply.is_error, reply.content
    return json.loads(reply.content[0].text)


def test_local_sdk_authoring_and_gate(local_env, addin):
    local_env.setenv("REVIT_BRIDGE_ALLOW_UNCONFIRMED", "1")

    async def scenario():
        async with host() as http, sdk(http) as session:
            assert "check_code" in {t.name for t in (await session.list_tools()).tools}
            assert (await call(session, "check_code", code=CODE))["compiled"]
            trial = await call(session, "dry_run_code", code=CODE)
            assert trial["success"] and trial["rolled_back"]
            eid = trial["evidence_id"]
            rows = (await http.get(TRIALS)).json()["trials"]
            assert rows == [{"evidence_id": eid, "time": rows[0]["time"], "rolled_back": True,
                             "code_sha256": hashlib.sha256(CODE.encode()).hexdigest()}]
            loaded = (await http.get(f"{TRIALS}/{eid}")).json()["code"]
            pack = {"schema_version": 1, "version": "1.0.0", "name": "count_trial_walls",
                    "code_template": loaded, "parameters": [], "description": "Wall count"}
            assert loaded == CODE
            assert (await http.post(B + "/check", json={"pack": pack})).json()["compiled"]
            assert (await http.post(B + "/dry-run", json={"pack": pack})).json()["rolled_back"]
            saved = await http.post(B + "/solidify", json={"name": pack["name"], "code": loaded,
                                                           "description": "Wall count", "parameters": []})
            assert saved.json()["name"] in {t["name"] for t in await call(session, "list_tools")}
            await call(session, "solidify_tool", name="remote_wall_count", code=CODE, description="Count walls")
            assert (await http.get(B + "/tools/remote_wall_count")).status_code == 200
            assert (await call(session, "execute_code", code=CODE))["error"] == "confirmation_required"
            token = (await call(session, "confirm_spec", spec=code_spec(CODE)))["token"]
            assert (await call(session, "execute_code", code=CODE, token=token))["success"]
            assert any(r["params"].get("confirm") and r["params"].get("code") == CODE for r in addin.requests)
            records = (await http.get(SETTINGS)).json()["calls"]
            assert [r["tool"] for r in records] == ["execute_code", "dry_run_code"]
            assert set(records[0]) == {"evidence_id", "time", "tool", "success"}
            assert (await call(session, "evidence"))[0]["host"] == "remote-mcp"
            # A confirmation spent over MCP is also spent in the local web gate.
            web_token = (await http.post(B + "/spec/confirm", json={"spec": code_spec(CODE)})).json()["token"]
            assert (await call(session, "execute_code", code=CODE, token=web_token))["success"]
            replay = await http.post(B + "/execute", json={"code": CODE, "token": web_token})
            assert replay.json()["error"] == "confirmation_invalid"
            args = {"name": "create_wall", "params": json.dumps(WALL)}
            before = len(addin.requests)
            assert (await call(session, "run_tool", **args))["error"] == "confirmation_required"
            assert len(addin.requests) == before
            confirmed = await call(session, "confirm_spec", spec=spec_for("create_wall", **WALL))
            assert (await call(session, "run_tool", **args, token=confirmed["token"]))["success"]
            assert (await call(session, "evidence"))[0]["tool"] == "create_wall"
    asyncio.run(scenario())


def test_cloud_default_off(env, tmp_path):
    env.setenv("CORS_ORIGINS", "https://host.example")
    # An installed SPA must not turn the disabled endpoint into an HTML 200.
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA")
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(create_app(frontend_dist=dist)),
                                      base_url=LOCAL) as http:
            for method in ("GET", "POST", "DELETE"):
                assert (await http.request(method, "/mcp")).status_code == 404
            assert (await http.options("/mcp", headers={"Origin": "https://host.example",
                          "Access-Control-Request-Method": "POST"})).status_code == 404
    asyncio.run(scenario())


async def paired(http):
    response = await http.post(B + "/devices/pair", json={"label": "Test Revit"})
    data = response.json()
    headers = {"X-Device-Id": data["device_id"], "X-Device-Key": data["browser_key"]}
    assert (await http.post(SETTINGS + "/tokens", json={"label": "unredeemed"}, headers=headers)).status_code == 403
    assert (await http.post(B + "/devices/redeem", json={"code": data["code"]})).status_code == 200
    return headers


async def issue(http, headers):
    response = await http.post(SETTINGS + "/tokens", headers=headers, json={"label": "Test client"})
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    assert set(response.json()["info"]) == {"id", "device_id", "created_at", "last_used_at", "label"}
    return response.json()


class FakeSocket:
    client_state = SimpleNamespace(name="CONNECTED")

    def __init__(self, device):
        self.device = device
        self.commands = []
        self.handler = workbench_handler()

    async def send_text(self, message):
        request = json.loads(message)
        self.commands.append(request)
        get_relay().resolve_response(self.device, json.dumps(self.handler(request)))


def test_cloud_device_isolation_and_revocation(env, tmp_path, caplog):
    env.setenv("REMOTE_MCP", "on")
    env.setenv("CORS_ORIGINS", "https://host.example")
    caplog.set_level(logging.DEBUG)

    async def scenario():
        async with host("https://host.example", "192.0.2.7") as http:
            a, b = await paired(http), await paired(http)
            admin = {"X-Admin-Token": FIXTURE_ADMIN_PASSWORD, "X-Device-Id": b["X-Device-Id"]}
            forged = {**a, "X-Device-Id": b["X-Device-Id"]}
            for headers in [{}, forged, {"X-Admin-Token": FIXTURE_ADMIN_PASSWORD}]:
                assert (await http.post(SETTINGS + "/tokens", json={"label": "x"}, headers=headers)).status_code == 403
            ta, tb = await issue(http, a), await issue(http, admin)
            ha = {"Authorization": "Bearer " + ta["token"], **b}
            hb = {"Authorization": "Bearer " + tb["token"]}
            sa, sb = FakeSocket(a["X-Device-Id"]), FakeSocket(b["X-Device-Id"])
            for sock in (sa, sb):
                get_relay().register(sock.device, sock)
            for bad in ({}, {"Authorization": "Bearer wrong"}, a, admin):
                assert (await http.post("/mcp", headers=bad)).status_code == 401
            for headers, status in [({"Host": "other.example"}, 421), ({"Origin": "https://other.example"}, 403)]:
                assert (await http.post("/mcp", headers={**ha, **headers}, json={})).status_code == status
            async with sdk(http, hb) as client_b:
                await call(client_b, "solidify_tool", name="device_b_pack", code=CODE, description="Read walls")
                eb = (await call(client_b, "dry_run_code", code=CODE))["evidence_id"]
            async with sdk(http, ha) as client_a:
                assert "device_b_pack" not in {t["name"] for t in await call(client_a, "list_tools")}
                assert await call(client_a, "evidence") == []
                ea = (await call(client_a, "dry_run_code", code="return 7;"))["evidence_id"]
                assert [r["id"] for r in await call(client_a, "evidence")] == [ea]
            assert sa.commands[-1]["params"]["code"] == "return 7;"
            assert sb.commands[-1]["params"]["code"] == CODE
            assert [r["evidence_id"] for r in (await http.get(TRIALS, headers=a)).json()["trials"]] == [ea]
            assert (await http.get(f"{TRIALS}/{eb}", headers=a)).status_code == 404
            assert (await http.get(f"{TRIALS}/{ea}", headers=b)).status_code == 404
            status = await http.get(SETTINGS, headers=a)
            assert [r["evidence_id"] for r in status.json()["calls"]] == [ea]
            assert [t["id"] for t in status.json()["tokens"]] == [ta["info"]["id"]]
            assert status.json()["tokens"][0]["last_used_at"]
            assert (await http.delete(SETTINGS + "/tokens/" + tb["info"]["id"], headers=a)).status_code == 404
            assert (await http.delete(SETTINGS + "/tokens/" + ta["info"]["id"], headers=a)).json() == {"revoked": True}
            assert (await http.post("/mcp", headers=ha)).status_code == 401
            assert (await http.delete(B + "/devices/" + b["X-Device-Id"], headers=b)).status_code == 200
            assert (await http.post("/mcp", headers=hb)).status_code == 401
            config = await http.get("/config.json")
            disk = (tmp_path / "web-home" / "secrets.json").read_text()
            assert '"salt"' in disk and '"digest"' in disk
            for value in [ta["token"], tb["token"]]:
                assert value not in status.text + config.text + disk + caplog.text
    asyncio.run(scenario())


def test_loopback_guard_failure_limit_and_model_store_coexistence(local_env, make_client):
    from tests.test_deploy_mode import LOCAL as OPTIONS
    with make_client(**OPTIONS) as local:
        item = local.post(SETTINGS + "/tokens", json={"label": "Local"}).json()
        assert "token" in item
        model = "/api/v1/settings/models"
        assert local.put(model, json={"model": "test", "base_url": "https://models.example"}).is_success
        assert local.delete(model).is_success
        assert local.get(SETTINGS).json()["tokens"][0]["id"] == item["info"]["id"]
    headers = {"Authorization": "Bearer " + item["token"]}
    with make_client(**{**OPTIONS, "client": ("192.0.2.9", 123)}) as remote:
        assert remote.post("/mcp", json={}).status_code == 401
        assert remote.post("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert remote.post("/mcp", json=INIT, headers={**headers, **ACCEPT}).json()["result"]["serverInfo"]
        assert remote.post(SETTINGS + "/tokens", headers=headers, json={"label": "Denied"}).status_code == 403
        for extra in ({"Host": "other.example"}, {"Forwarded": "for=127.0.0.1"},
                      {"Origin": "https://foreign.example"}, {"Sec-Fetch-Site": "cross-site"}):
            assert remote.post("/mcp", json={}, headers={**headers, **extra}).status_code == 403
        for _ in range(8):
            assert remote.post("/mcp").status_code == 401
        assert remote.post("/mcp").status_code == 429


def test_local_container_loopback_publish(local_env, make_client):
    from tests.test_deploy_mode import LOCAL as OPTIONS
    local_env.setenv("REVIT_BRIDGE_WEB_CONTAINER", "1")
    local_env.setenv("HOST", "0.0.0.0")
    local_env.setenv("BIND_ADDRESS", "127.0.0.1")
    with make_client(**{**OPTIONS, "client": ("172.17.0.1", 111)}) as client:
        assert client.post("/mcp", json=INIT, headers=ACCEPT).json()["result"]["serverInfo"]
        assert client.post("/mcp", json={}, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
        assert client.post("/mcp", headers={"Authorization": "Bearer wrong"}).status_code == 401


@pytest.mark.parametrize("origin_setting", ["", "*"])
def test_public_host_needs_concrete_configuration(env, origin_setting):
    env.setenv("REMOTE_MCP", "on")
    env.setenv("CORS_ORIGINS", origin_setting)

    async def scenario():
        async with host("https://public.example") as http:
            token = (await issue(http, await paired(http)))["token"]
            r = await http.post("/mcp", json=INIT, headers={**ACCEPT, "Authorization": "Bearer " + token})
            assert r.status_code == 421
    asyncio.run(scenario())


def test_expiry_capacity_and_rollback_proof(monkeypatch):
    from backend import remote_trials

    async def scenario():
        cache = remote_trials.RemoteTrials()
        def put(index, source, scope="local", **changes):
            evidence = {"id": str(index), "ts": "now", "scope": scope, "action": "dry_run",
                        "host": "remote-mcp", "success": True, "result_summary": {"rolled_back": True},
                        "code_sha256": hashlib.sha256(source.encode()).hexdigest(), **changes}
            cache.remember(scope, source, evidence)
        put(1, "source", success=False)
        put(2, "source", result_summary={"rolled_back": False})
        put(3, "source", host="web")
        put(4, "source", code_sha256="wrong")
        put(5, "\u00e9" * 32769)
        assert cache.devices == {}
        for i in range(21):
            put(i, "\u00e9" * 32768)
        assert list(cache.devices["local"]) == list(map(str, range(1, 21)))
        monkeypatch.setattr(remote_trials, "TTL_SECONDS", 0.01)
        put(22, "source", "device-a")
        await asyncio.sleep(0.03)
        assert set(cache.devices) == {"local"}
        cache.close()
    asyncio.run(scenario())
