"""Exercise model settings through HTTP, with a fake transport and an isolated config home."""
import asyncio
import json
import logging
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

import httpx
import pytest

from backend.log_store import get_log_store
from backend.models_file import resolve
from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.test_deploy_mode import LOCAL

URL = "/api/v1/settings/models"
ADMIN = {"X-Admin-Token": FIXTURE_ADMIN_PASSWORD}
FILE_KEY = "fake-stored-test-only-1234"
ENV_KEY = "fake-env-test-only-5678"
GUEST_KEY = "fake-browser-test-only-9012"
GROUP = {"base_url": "https://file.example/v1", "model": "file-model", "api_key": FILE_KEY}
GUEST = {"x-llm-base-url": "https://guest.example/v1", "x-llm-model": "guest-model", "x-llm-key": GUEST_KEY}


def save(client, **fields):
    response = client.put(URL, headers=ADMIN, json={**GROUP, **fields})
    assert response.status_code == 200, response.text
    return response


def test_cloud_settings_authorization(client):
    save(client)
    for method in ("get", "put", "delete"):
        for token in ({}, {"X-Admin-Token": "wrong"}):
            r = client.request(method, URL, headers=token, **({"json": GROUP} if method == "put" else {}))
            assert r.status_code == 403 and FILE_KEY[-4:] not in r.text
    assert client.get(URL + "?status_only=true").json() == {"configured": True}
    assert client.put(URL, content=b"{", headers={"Content-Type": "application/json"}).status_code == 403


def test_local_settings_guard(local_env, make_client):
    remote = make_client(**{**LOCAL, "client": ("192.0.2.4", 1234)})
    assert remote.put(URL, json=GROUP).status_code == remote.delete(URL).status_code == 403
    local = make_client(**LOCAL)
    assert local.put(URL, json=GROUP).status_code == 200
    assert local.delete(URL).json()["configured"] is False


@pytest.mark.parametrize("mode,switch,allowed", [("cloud", None, True), ("local", None, False),
                                                  ("cloud", "off", False), ("local", "on", True)])
def test_browser_key_switch(env, make_client, mode, switch, allowed):
    env.setenv("DEPLOY_MODE", mode)
    if mode == "local":
        env.delenv("ADMIN_PASSWORD")
    if switch:
        env.setenv("ALLOW_BROWSER_KEYS", switch)
    client = make_client(**(LOCAL if mode == "local" else {}))
    assert client.get("/config.json").json()["features"]["byoModel"] is allowed
    if allowed:
        assert asyncio.run(resolve("planner", GUEST)).api_key == GUEST_KEY
    else:
        r = client.post("/api/chat", json={"message": "hi", "bridge": False}, headers=GUEST)
        assert r.status_code == 400 and "ALLOW_BROWSER_KEYS" in r.text and GUEST_KEY not in r.text
        assert client.get("/health", headers=GUEST).status_code == 400


def test_precedence_and_live_rotation(env, make_client):
    client = make_client()
    def target(role="planner"):
        t = asyncio.run(resolve(role, GUEST))
        return t.base_url, t.model, t.api_key
    assert target() == (GUEST["x-llm-base-url"], "guest-model", GUEST_KEY)
    save(client)
    assert target() == (GROUP["base_url"], "file-model", FILE_KEY)
    save(client, role="analyst", model="small", api_key="fake-analyst-9876")
    assert target("analyst") == (GROUP["base_url"], "small", "fake-analyst-9876")
    save(client, api_key="fake-rotated-1111")
    assert target()[2] == "fake-rotated-1111"
    assert client.delete(URL + "?role=analyst", headers=ADMIN).status_code == 200
    assert target("analyst") == target()
    for k, v in {"LLM_BASE_URL": "https://env.example/v1", "LLM_MODEL": "env-model", "LLM_API_KEY": ENV_KEY}.items():
        env.setenv(k, v)
    client = make_client()
    assert target() == ("https://env.example/v1", "env-model", ENV_KEY)
    assert client.delete(URL, headers=ADMIN).json()["configured"] is True
    assert target()[2] == ENV_KEY


def test_no_key_in_answers_events_errors_logs_usage_or_schema(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [save(client), client.get(URL, headers=ADMIN), client.get("/config.json"),
                 client.get("/openapi.json"), client.put(URL, headers=ADMIN, json={**GROUP, "role": FILE_KEY})]
    assert responses[0].json()["models"]["planner"]["key_tail"] == "1234"
    assert responses[-1].status_code == 422
    replies = [httpx.Response(200, text="".join(
        "data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n"
        for part in ("Hello ", FILE_KEY[:10], FILE_KEY[10:])) + "data: [DONE]\n\n"),
        httpx.Response(401, text="Bad bearer " + FILE_KEY)]
    def handler(request):
        assert request.headers["authorization"] == "Bearer " + FILE_KEY
        return replies.pop(0)
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    for _ in range(2):
        chat = client.post("/api/chat", json={"message": "hi", "bridge": False})
        assert chat.status_code == 200 and "event: error" in chat.text
        responses.append(chat)
    assert not replies and "HTTP 401" in responses[-1].text
    assert FILE_KEY not in "".join(json.loads(line[6:]) for line in responses[-2].text.splitlines()
                                   if line.startswith('data: "'))
    rows = get_log_store().calls_of_session(responses[-2].headers["X-Session-Id"])
    assert rows
    usage = client.get("/api/usage", params={"session_id": rows[0]["session_id"]}, headers=ADMIN)
    assert usage.status_code == 200 and usage.json()["items"]
    responses += [client.get("/api/logs", headers=ADMIN), usage,
                  client.delete(URL, headers=ADMIN)]
    for surface in [r.text for r in responses] + [str(rows), caplog.text, Path("docs/api-v1.json").read_text()]:
        assert FILE_KEY not in surface


def test_concurrent_updates_and_permissions(client, tmp_path):
    roles = ["shared", "planner", "analyst", "supervisor"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda role: save(client, role=role, model=role), roles * 5))
    path = tmp_path / "web-home" / "secrets.json"
    assert set(json.loads(path.read_bytes())) == set(roles)
    assert {role: info["model"] for role, info in client.get(URL, headers=ADMIN).json()["saved"].items()} == {
        role: role for role in roles}
    if os.name == "nt":
        import win32api, win32security as sec
        from ntsecuritycon import TOKEN_QUERY
        sid = sec.GetTokenInformation(sec.OpenProcessToken(win32api.GetCurrentProcess(), TOKEN_QUERY), sec.TokenUser)[0]
        sd = sec.GetNamedSecurityInfo(str(path), sec.SE_FILE_OBJECT, sec.DACL_SECURITY_INFORMATION)
        acl = sd.GetSecurityDescriptorDacl()
        assert sd.GetSecurityDescriptorControl()[0] & sec.SE_DACL_PROTECTED
        assert acl.GetAceCount() == 1 and acl.GetAce(0)[2] == sid
    else:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("unreadable", [False, True])
def test_bad_store_safe_at_startup(make_client, tmp_path, caplog, unreadable):
    path = tmp_path / "web-home" / "secrets.json"
    path.parent.mkdir()
    if unreadable:
        path.mkdir()  # opening this path as a file fails even for root
    else:
        path.write_bytes(b"{" + FILE_KEY.encode())
    client = make_client()
    assert client.get("/health").status_code == 200
    config = client.get("/config.json")
    assert config.status_code == 200 and not config.json()["features"]["serverModel"]
    detail = client.get(URL, headers=ADMIN)
    assert detail.status_code == 503
    assert client.get(URL + "?status_only=true").json() == {"configured": False}
    assert FILE_KEY not in detail.text + config.text + caplog.text


def test_location_and_checkout_exclusion(env, tmp_path, make_client):
    from backend import model_settings
    env.delenv("REVIT_BRIDGE_WEB_HOME")
    for name in ("APPDATA", "XDG_CONFIG_HOME", "HOME", "USERPROFILE"):
        env.setenv(name, str(tmp_path))
    root = tmp_path / "Library/Application Support" if sys.platform == "darwin" else tmp_path
    assert model_settings.settings_home() == root / "revit-bridge-web"
    env.setenv("REVIT_BRIDGE_WEB_HOME", str(model_settings.APP_ROOT / "accidental"))
    assert make_client().put(URL, headers=ADMIN, json=GROUP).status_code == 503
    assert not (model_settings.APP_ROOT / "accidental").exists()


def test_omitted_key_is_bound_to_saved_address_and_short_keys_are_masked(client):
    assert save(client, api_key="F4KE").json()["saved"]["shared"]["key_tail"] is None
    r = client.put(URL, headers=ADMIN, json={"model": "renamed", "base_url": GROUP["base_url"]})
    assert "F4KE" not in r.text and r.json()["configured"]
    r = client.put(URL, headers=ADMIN, json={"model": "renamed", "base_url": GUEST["x-llm-base-url"]})
    assert not r.json()["saved"]["shared"]["key_set"] and not r.json()["configured"]


def test_settings_overlay_preserves_yaml_and_provider_options(env, make_client, tmp_path):
    from tests.test_models_file import MIXED, write_models
    path = write_models(tmp_path, MIXED)
    before = path.read_bytes()
    env.setenv("W5A_FAKE_SMALL_KEY", ENV_KEY)
    client = make_client()
    save(client, role="analyst", base_url="https://small.example/v1", model="small-model")
    target = asyncio.run(resolve("analyst", GUEST))
    assert target.api_key == ENV_KEY and target.options == {"max_tokens": 512, "temperature": 0.0}
    save(client, role="analyst")
    target = asyncio.run(resolve("analyst", GUEST))
    assert target.api_key == FILE_KEY and target.options == {}
    client.delete(URL, headers=ADMIN)
    assert asyncio.run(resolve("analyst", {})).api_key == ENV_KEY
    assert path.read_bytes() == before


def test_legacy_environment_credential_in_public_fields(env, make_client, caplog):
    env.setenv("LLM_API_KEY", ENV_KEY)
    env.setenv("LLM_BASE_URL", "https://private:fake-password@env.example/v1?auth=" + ENV_KEY)
    env.setenv("LLM_MODEL", ENV_KEY)
    client = make_client()
    detail = client.get(URL, headers=ADMIN)
    assert detail.json()["models"]["planner"]["base_url"] == "https://env.example/v1"
    refused = client.post("/api/chat", json={"message": "hi", "bridge": False})
    assert refused.status_code == 400
    for value in (detail.text, refused.text, client.get("/config.json").text, caplog.text):
        assert ENV_KEY not in value and "fake-password" not in value
