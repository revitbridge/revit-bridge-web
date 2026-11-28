"""The host model library, its private atomic selections and their effect on real turns."""
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.config import ConfigError
from backend.llm import LLMError
from backend.log_store import get_log_store
from backend.models_file import ALL_ROLES, ROLES, resolve
from tests.fake_model import json_turn, text_turn
from tests.test_deploy_mode import LOCAL
from tests.test_model_settings import ADMIN, ENV_KEY, FILE_KEY, GROUP, GUEST, GUEST_KEY, URL
from tests.test_models_file import MAIN_KEY, MIXED, SMALL_KEY, keys, recorder, write_models
from tests.test_roles import DONE, OK

LIBRARY = URL + "/library/"
SELECT = URL + "/selection"
SAVED = {**GROUP, "api": "responses", "vision": True}


def select(client, role, name):
    return client.put(SELECT, json={"role": role, "entry": name}, headers=ADMIN)


def put(client, name="Studio", body=SAVED):
    return client.put(LIBRARY + name, json=body, headers=ADMIN)


def test_library_crud_and_key_binding(client):
    assert put(client).json()["library"] == [
        {"name": "Studio", "source": "saved", "base_url": GROUP["base_url"], "model": GROUP["model"],
         "api": "responses", "vision": True, "key_set": True, "key_tail": FILE_KEY[-4:], "shadowed": False, "selectable": True}]
    same = {"base_url": GROUP["base_url"] + "/", "model": "revised"}
    assert put(client, body=same).json()["library"][0]["key_set"]
    assert put(client, body={**same, "base_url": "https://changed.example/v1"}).json()["library"][0] == {
        "name": "Studio", "source": "saved", "base_url": "https://changed.example/v1", "model": "revised",
        "api": "chat", "vision": False, "key_set": False, "key_tail": None, "shadowed": False, "selectable": True}
    for role in ALL_ROLES:
        assert select(client, role, "Studio").status_code == 200
    assert select(client, "planner", None).json()["selection"]["planner"]["entry"] is None
    assert select(client, "planner", "absent").json() == {"error": "unknown_entry"}
    assert select(client, "planner", "absent").status_code == 404
    deleted = client.delete(LIBRARY + "Studio", headers=ADMIN).json()
    assert deleted["library"] == []
    assert deleted["selection"] == {r: {"entry": None, "selected_missing": False} for r in ALL_ROLES}
    assert client.delete(LIBRARY + "Studio", headers=ADMIN).status_code == 404


def test_library_limits_names_and_clear(client):
    for name in ["7starts", "_first", "-first", "has.dot", "has space", "a" * 41, "caf\u00e9"]:
        assert put(client, name).status_code == 422
    names = ["Z", "b" * 40, *(f"entry_{i}-X" for i in range(18))]
    for name in names:
        assert put(client, name).status_code == 200
    full = put(client, "Overflow")
    assert full.status_code == 409 and full.json() == {"error": "library_full"}
    assert [e["name"] for e in put(client, "Z").json()["library"]] == sorted(names)
    assert select(client, "planner", "Z").status_code == 200
    assert client.delete(URL + "?role=planner", headers=ADMIN).json()["selection"]["planner"]["entry"] == "Z"
    cleared = client.delete(URL, headers=ADMIN).json()
    assert cleared["library"] == [] and all(s["entry"] is None for s in cleared["selection"].values())


def test_twenty_full_length_credentials_fit_the_library(client, tmp_path):
    key = "\\" * 4096
    for role in ("shared", *ROLES):
        assert client.put(URL, headers=ADMIN, json={**GROUP, "role": role, "api_key": key}).status_code == 200
    for i in range(20):
        assert put(client, f"Studio{i}", {**GROUP, "api_key": key}).status_code == 200
    assert (tmp_path / "web-home" / "secrets.json").stat().st_size > 128 * 1024
    assert select(client, "planner", "Studio19").status_code == 200
    assert asyncio.run(resolve("planner", {})).api_key == key


@pytest.mark.parametrize("field,value", [("api", "other"), ("vision", 1), ("api_key", "fake bad key"),
                                          ("base_url", "https://host.example/v1?q=x"), ("model", "")])
def test_library_validation(client, field, value):
    response = put(client, body={**SAVED, field: value})
    assert response.status_code == 422 and response.json() == {"error": "invalid_args"}
    assert FILE_KEY not in response.text and "fake bad key" not in response.text


def test_file_gains_then_loses_a_name(make_client, keys, tmp_path):
    client = make_client()
    assert put(client, "small").status_code == 200
    assert select(client, "planner", "small").status_code == 200
    path = write_models(tmp_path, MIXED)
    client = make_client()
    conflict = put(client, "main")
    assert conflict.status_code == 409 and conflict.json() == {"error": "file_entry"}
    details = client.get(URL, headers=ADMIN).json()
    assert [(e["name"], e["source"], e["shadowed"]) for e in details["library"]] == [
        ("main", "file", False), ("small", "file", False), ("small", "saved", True)]
    assert select(client, "analyst", "small").status_code == 200
    def check_file_target():
        for role in ("planner", "analyst"):
            t = asyncio.run(resolve(role, GUEST))
            assert (t.base_url, t.model, t.api_key, t.options) == (
                "https://small.example/v1", "small-model", SMALL_KEY, {"max_tokens": 512, "temperature": 0})
    check_file_target()
    library = client.get("/config.json").json()["features"]["modelLibrary"]
    assert [(e["name"], e["source"]) for e in library] == [("main", "file"), ("small", "file")]
    deleted = client.delete(LIBRARY + "small", headers=ADMIN)
    assert deleted.status_code == 200
    for role in ("planner", "analyst"):
        assert deleted.json()["selection"][role] == {"entry": "small", "selected_missing": False}
    check_file_target()
    assert select(client, "analyst", "main").status_code == 200
    path.unlink()
    client = make_client()
    assert client.get(URL, headers=ADMIN).json()["selection"]["analyst"] == {"entry": "main", "selected_missing": True}
    assert asyncio.run(resolve("analyst", GUEST)).api_key == GUEST["x-llm-key"]
    assert client.get("/config.json").json()["features"]["modelSelection"]["analyst"] == "main"


@pytest.mark.parametrize("role", ALL_ROLES)
def test_file_and_saved_selections_override_headers_and_groups(role, make_client, keys, tmp_path):
    keys.setenv("LLM_MAX_TOKENS", "8192")
    write_models(tmp_path, MIXED.replace("temperature: 0", "temperature: 0, api: responses, vision: true, "
                                          "reasoning_effort: high, timeout_s: 120"))
    client = make_client()
    assert client.put(URL, headers=ADMIN, json=GROUP).status_code == 200
    assert select(client, role, "small").status_code == 200
    t = asyncio.run(resolve(role, GUEST))
    assert (t.base_url, t.model, t.api_key, t.vision) == ("https://small.example/v1", "small-model", SMALL_KEY, True)
    assert t.options == {"api": "responses", "max_tokens": 512, "temperature": 0, "reasoning_effort": "high",
                         "timeout": 120}
    assert put(client).status_code == 200
    assert select(client, role, "Studio").status_code == 200
    t = asyncio.run(resolve(role, GUEST))
    assert (t.base_url, t.model, t.api_key, t.vision) == (GROUP["base_url"], GROUP["model"], FILE_KEY, True)
    assert t.options == {"api": "responses", "max_tokens": 8192}
    f = client.get("/config.json").json()["features"]
    assert f["models"][role] == t.model and f["modelSelection"][role] == "Studio"
    assert f["modelLibrary"][-1] == {"name": "Studio", "model": t.model, "vision": True, "source": "saved"}


def test_optimizer_fallback_and_mapping(make_client, tmp_path, keys):
    client = make_client()
    def target(role, headers):
        return asyncio.run(resolve(role, headers))
    for headers in ({}, GUEST):
        assert target("optimizer", headers) == target("planner", headers)
    assert put(client).status_code == 200
    assert select(client, "planner", "Studio").status_code == 200
    write_models(tmp_path, MIXED)
    make_client()
    assert target("optimizer", GUEST) == target("planner", GUEST)
    write_models(tmp_path, MIXED + "  optimizer: small\n")
    make_client()
    assert target("optimizer", GUEST).api_key == SMALL_KEY
    write_models(tmp_path, MIXED + "  optimizer: missing\n")
    with pytest.raises(ConfigError, match="roles.optimizer"):
        make_client()


def test_selected_keyless_file_entry_never_borrows_another_key(env, make_client, tmp_path):
    write_models(tmp_path, MIXED)
    env.delenv("W5A_FAKE_MAIN_KEY", raising=False)
    env.setenv("LLM_API_KEY", SMALL_KEY)
    client = make_client()
    assert client.put(URL, headers=ADMIN, json={**GROUP, "base_url": "https://main.example/v1"}).status_code == 200
    assert select(client, "planner", "main").status_code == 200
    assert asyncio.run(resolve("planner", GUEST)).api_key == ""
    assert client.get(URL + "?status_only=true").json() == {"configured": False}
    assert client.get("/config.json").json()["features"]["serverModel"] is False
    env.setenv("W5A_FAKE_MAIN_KEY", MAIN_KEY)
    assert asyncio.run(resolve("planner", GUEST)).api_key == MAIN_KEY


def test_long_file_entry_name_stays_private(keys, make_client, tmp_path):
    name = "possibly-pasted-key"
    path = write_models(tmp_path, MIXED.replace("small:", name + ":").replace(": small\n", ": " + name + "\n"))
    client = make_client()
    assert select(client, "planner", name).status_code == 200
    assert asyncio.run(resolve("planner", {})).api_key == SMALL_KEY
    settings = client.get(URL, headers=ADMIN)
    row = settings.json()["library"][1]
    assert row["name"] == "<entry #2>" and row["selectable"] is False
    config = client.get("/config.json")
    assert [e["name"] for e in config.json()["features"]["modelLibrary"]] == ["main"]
    assert name not in config.text + settings.text
    path.unlink()
    assert name not in make_client().get("/config.json").text


def test_writes_check_only_store_fields(keys, make_client, tmp_path):
    write_models(tmp_path, MIXED.replace("main-model", "ollama/llama3"))
    client = make_client()
    assert put(client, body={**GROUP, "api_key": "ollama"}).status_code == 200
    assert put(client, "More").status_code == 200
    response = put(client, "New", {**GROUP, "api_key": "responses", "api": "responses"})
    assert response.status_code == 422 and response.json() == {"error": "invalid_args"}
    assert select(client, "analyst", "main").status_code == 200
    target = asyncio.run(resolve("analyst", GUEST))
    assert target.model == "ollama/llama3" and target.api_key == MAIN_KEY


@pytest.mark.parametrize("own_key,field", [("small-model", "model"), ("small.example", "base_url")])
def test_selected_entry_refuses_its_own_credential(keys, make_client, tmp_path, own_key, field):
    write_models(tmp_path, MIXED)
    client = make_client()
    assert select(client, "planner", "small").status_code == 200
    keys.setenv("W5A_FAKE_SMALL_KEY", own_key)
    with pytest.raises(LLMError) as exc:
        asyncio.run(resolve("planner", GUEST))
    assert exc.value.status == 400
    assert client.get(URL, headers=ADMIN).json()["models"]["planner"][field] == ""


def test_private_fields_and_live_supervisor_switch(env, make_client, recorder, caplog):
    env.delenv("LLM_SINGLE_MODEL")
    env.setenv("LLM_BASE_URL", "https://regular.example/v1")
    env.setenv("LLM_MODEL", "regular")
    env.setenv("LLM_API_KEY", MAIN_KEY)
    client = make_client()
    caplog.set_level(logging.DEBUG)
    responses = [put(client, body=GROUP),
                 put(client, "other", {**GROUP, "model": FILE_KEY}),
                 put(client, "other", {**GROUP, "base_url": GROUP["base_url"] + "/" + FILE_KEY}),
                 put(client, "other", {**GROUP, "api_key": "Studio"})]
    assert [r.status_code for r in responses] == [200, 422, 422, 422]
    recorder.model.turns += [text_turn("hello"), json_turn(DONE), json_turn(DONE), text_turn("ready")]
    first = client.post("/api/chat", json={"message": "hello"})
    sid = first.headers["X-Session-Id"]
    report = {"session_id": sid, "execution": OK}
    responses += [first, client.post("/api/chat", json=report), select(client, "supervisor", "Studio"),
                  client.post("/api/chat", json=report), client.post("/api/chat", json={"session_id": sid, "message": "hello"}),
                  client.get(URL, headers=ADMIN), client.get("/config.json")]
    assert all(r.status_code == 200 and "event: error" not in r.text for r in responses[4:])
    sent = [(r["url"], r["body"]["model"], r["authorization"]) for r in recorder.sent]
    regular = ("https://regular.example/v1/chat/completions", "regular", "Bearer " + MAIN_KEY)
    assert sent == [regular, regular, (GROUP["base_url"] + "/chat/completions", GROUP["model"], "Bearer " + FILE_KEY), regular]
    rows = get_log_store().calls_of_session(sid)
    assert [r["role"] for r in rows] == ["planner", "supervisor", "supervisor", "planner"]
    for surface in [caplog.text, str(rows), *(r.text for r in responses)]:
        assert FILE_KEY not in surface and MAIN_KEY not in surface


@pytest.mark.parametrize("bad", [{"library": []}, {"library": {"x": {"model": "missing-url"}}},
                                 {"library": {"7wrong": GROUP}}, {"selection": None},
                                 {"selection": {"shared": "x"}}, {"selection": {"planner": ["x"]}},
                                 {"selection": {"optimizer": "space name"}}])
def test_malformed_store_is_unavailable(client, tmp_path, bad):
    path = tmp_path / "web-home" / "secrets.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(bad))
    assert client.get(URL, headers=ADMIN).status_code == put(client).status_code == 503
    assert json.loads(path.read_text()) == bad


def test_cloud_and_local_access(make_client, local_env):
    local = make_client(**LOCAL)
    assert local.put(LIBRARY + "Studio", json=SAVED).status_code == 200
    assert local.put(SELECT, json={"role": "planner", "entry": "Studio"}).status_code == 200
    remote = make_client(**{**LOCAL, "client": ("192.0.2.4", 1234)})

    def denied(client):
        for method, route, body in [("GET", URL, None), ("PUT", LIBRARY + "X", SAVED),
                                    ("DELETE", LIBRARY + "Studio", None), ("PUT", SELECT, {"role": "planner", "entry": None})]:
            resp = client.request(method, route, json=body)
            assert resp.status_code == 403 and FILE_KEY not in resp.text
            assert resp.headers["cache-control"] == "no-store"
    denied(remote)
    assert local.delete(LIBRARY + "Studio", headers={"Origin": "https://foreign.example"}).status_code == 403
    local_env.setenv("DEPLOY_MODE", "cloud")
    local_env.setenv("ADMIN_PASSWORD", ADMIN["X-Admin-Token"])
    cloud = make_client()
    denied(cloud)
    assert cloud.get(URL + "?status_only=true").json() == {"configured": True}


def test_interleaved_library_and_selection_updates(client):
    assert put(client).status_code == 200
    barrier = Barrier(2)
    def write(operation):
        barrier.wait(timeout=5)
        return operation().status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(write, [lambda: put(client, "Second"), lambda: select(client, "planner", "Studio")])) == [200, 200]
    details = client.get(URL, headers=ADMIN).json()
    assert details["selection"]["planner"]["entry"] == "Studio"
    assert [e["name"] for e in details["library"]] == ["Second", "Studio"]


# -- unchanged default path ----------------------------------------------------

DEFAULT_HEADERS = ({}, {"x-llm-model": "guest-model", "x-llm-key": GUEST_KEY}, GUEST, {"x-llm-key": GUEST_KEY})
ENV_TARGET = ("https://env.example/v1", "ollama/llama3", ENV_KEY, {})
GROUP_TARGET = (GROUP["base_url"], GROUP["model"], FILE_KEY, {})
GUEST_TARGET = (GUEST["x-llm-base-url"], "guest-model", GUEST_KEY, {})
COMPLETE_ENV = dict(zip(("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"), ENV_TARGET[:3]))


def check_defaults(client, expected):
    features = client.get("/config.json").json()["features"]
    assert features["serverModel"] is all(expected["planner"][0][:3])
    for role in ROLES:
        assert features["models"][role] == expected[role][0][1]
        for headers, want in zip(DEFAULT_HEADERS, expected[role], strict=True):
            t = asyncio.run(resolve(role, headers))
            assert (t.base_url, t.model, t.api_key, t.options) == want


@pytest.mark.parametrize("environment,groups,default,supervisor", [
    pytest.param(COMPLETE_ENV, [], ENV_TARGET, ENV_TARGET, id="llm-only"),
    pytest.param(COMPLETE_ENV, [{**GROUP, "role": "supervisor", "api_key": "ollama"}],
                 ENV_TARGET, ENV_TARGET, id="B-complete-llm-with-supervisor-key"),
    pytest.param({"LLM_MODEL": "env-model", "LLM_API_KEY": ENV_KEY}, [GROUP],
                 (GROUP["base_url"], "env-model", FILE_KEY, {}), (GROUP["base_url"], "env-model", FILE_KEY, {}),
                 id="partial-llm-with-shared-group"),
    pytest.param({}, [GROUP, {**GROUP, "role": "supervisor", "model": "review"}], GROUP_TARGET,
                 (GROUP["base_url"], "review", FILE_KEY, {}), id="groups-only"),
])
def test_defaults_without_models_file(env, make_client, environment, groups, default, supervisor):
    for name, value in environment.items():
        env.setenv(name, value)
    client = make_client()
    for group in groups:
        assert client.put(URL, headers=ADMIN, json=group).status_code == 200
    check_defaults(client, {"planner": [default] * 4, "analyst": [default] * 4, "supervisor": [supervisor] * 4})


@pytest.mark.parametrize("with_groups", [False, True], ids=["file-only", "file-with-groups"])
def test_defaults_with_models_file(keys, make_client, tmp_path, with_groups):
    write_models(tmp_path, MIXED)
    client = make_client()
    main = ("https://main.example/v1", "main-model", MAIN_KEY, {})
    small = ("https://small.example/v1", "small-model", SMALL_KEY, {"max_tokens": 512, "temperature": 0})
    if with_groups:
        assert client.put(URL, headers=ADMIN, json=GROUP).status_code == 200
        aligned = {"role": "analyst", "base_url": small[0], "model": small[1], "api_key": FILE_KEY}
        assert client.put(URL, headers=ADMIN, json=aligned).status_code == 200
        check_defaults(client, {"planner": [GROUP_TARGET] * 4, "analyst": [small] * 4, "supervisor": [GROUP_TARGET] * 4})
    else:
        planner = [main, (main[0], "guest-model", MAIN_KEY, {}), GUEST_TARGET, main]
        check_defaults(client, {"planner": planner, "analyst": [small] * 4, "supervisor": [small] * 4})


@pytest.mark.parametrize("proxy_key", [MAIN_KEY, ""], ids=["A-other-file-key", "unset-file-key"])
def test_defaults_with_another_files_key_in_the_model_and_endpoint(env, make_client, tmp_path, caplog, proxy_key):
    env.setenv("LOCAL_KEY", "ollama")
    if proxy_key:
        env.setenv("PROXY_KEY", proxy_key)
    else:
        env.delenv("PROXY_KEY", raising=False)
    write_models(tmp_path, """\
models:
  local: {base_url: http://localhost/v1, model: llama3, api_key_env: LOCAL_KEY}
  proxy: {base_url: https://ollama.example/v1, model: ollama/llama3, api_key_env: PROXY_KEY, api: responses}
roles: {planner: proxy}
""")
    caplog.set_level(logging.INFO, logger="backend.models")
    client = make_client()
    url, model = "https://ollama.example/v1", "ollama/llama3"
    own = (url, model, proxy_key, {"api": "responses"})
    targets = [own, (url, "guest-model", proxy_key or GUEST_KEY, {}), GUEST_TARGET,
               (url, model, proxy_key or GUEST_KEY, {"api": "responses"})]
    check_defaults(client, dict.fromkeys(ROLES, targets))
    details = client.get(URL, headers=ADMIN).json()["models"]
    assert (details["planner"]["base_url"], details["planner"]["model"]) == (url, model)
    assert "planner ollama/llama3" in caplog.text
