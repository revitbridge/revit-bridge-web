"""The chat's modes: single (one loop), roles (planner + supervisor), roles_analyst (analyst +
planner + supervisor) and auto, the host's default. The default is roles; LLM_SINGLE_MODEL=1 and
ROLES_ANALYST=on make it single and roles_analyst. A request may name its mode (``mode`` in the
body): a local host honours it, a cloud host only for an admin-authenticated request. The
resolved mode is on the turn's first ``stage`` event and on every ``llm_calls`` row, and
``/api/usage/runs`` reports it per run."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import llm as llm_module
from backend.config import WebSettings
from backend.log_store import get_log_store
from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.fake_model import RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.test_api_v1 import WALL, spec_for
from tests.test_deploy_mode import LOCAL
from tests.test_host_loop import HEADERS, events, sse, tokens

ROUTING_QUERY = {"route": "query", "facts_needed": [], "note": "Read the levels."}
DONE = {"verdict": "done", "report": "Done."}
ADMIN = {"X-Admin-Token": FIXTURE_ADMIN_PASSWORD}
ALL = ["auto", "single", "roles", "roles_analyst"]


@pytest.fixture
def model(monkeypatch):
    fake = RoleScriptedModel()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


def host(env, local: bool = False, **environment) -> TestClient:
    """A host with the roles' default environment (no LLM_SINGLE_MODEL) plus ``environment``."""
    env.delenv("LLM_SINGLE_MODEL", raising=False)
    if local:
        env.setenv("DEPLOY_MODE", "local")
        env.setenv("ALLOW_BROWSER_KEYS", "on")  # scripted browser model
        env.delenv("ADMIN_PASSWORD", raising=False)
    for name, value in environment.items():
        env.setenv(name, value)
    from tests.conftest import _reset
    _reset()
    from backend.main import create_app
    return TestClient(create_app(), **(LOCAL if local else {}))


def script_for(model: RoleScriptedModel, mode: str) -> None:
    """The model's answers to one message ("levels?") in ``mode``."""
    if mode == "single":
        model.script("single", tool_turn(("query", {"kind": "levels"})), text_turn("L1 and L2."))
        return
    if mode == "roles_analyst":
        model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", tool_turn(("query", {"kind": "levels"})), text_turn("L1 and L2."))


ROLES_OF = {"single": ["single", "single"], "roles": ["planner", "planner"],
            "roles_analyst": ["analyst", "planner", "planner"]}


# -- the default and the switches ---------------------------------------------------------------

@pytest.mark.parametrize("environment, default", [
    ({}, "roles"),
    ({"ROLES_ANALYST": "off"}, "roles"),
    ({"ROLES_ANALYST": "on"}, "roles_analyst"),
    ({"LLM_SINGLE_MODEL": "1"}, "single"),
    ({"LLM_SINGLE_MODEL": "1", "ROLES_ANALYST": "on"}, "single"),
])
def test_the_default_mode_and_the_switches(env, revit, model, environment, default):
    client = host(env, **environment)
    features = client.get("/config.json").json()["features"]
    assert features["chatModeDefault"] == default
    assert features["roles"] is (default != "single")
    script_for(model, default)
    resp = client.post("/api/chat", json={"message": "levels?"}, headers=HEADERS)
    assert tokens(sse(resp.text)) == "L1 and L2." and model.roles == ROLES_OF[default]
    assert events(sse(resp.text), "stage")[0]["mode"] == default
    # auto is the default, said or not
    model.roles.clear()
    script_for(model, default)
    resp = client.post("/api/chat", json={"message": "levels?", "mode": "auto"}, headers=HEADERS)
    assert model.roles == ROLES_OF[default] and events(sse(resp.text), "stage")[0]["mode"] == default


def test_the_settings_resolve_a_requested_mode():
    local = WebSettings.from_env({"DEPLOY_MODE": "local"})
    assert local.chat_mode_default == "roles" and local.roles_analyst is False
    assert [local.resolve_chat_mode(m, admin=False) for m in (None, *ALL)] == [
        "roles", "roles", "single", "roles", "roles_analyst"]
    assert local.chat_modes(admin=False) == ALL
    cloud = WebSettings.from_env({"DEPLOY_MODE": "cloud", "ADMIN_PASSWORD": "x", "LLM_SINGLE_MODEL": "1"})
    assert cloud.chat_mode_default == "single"
    assert [cloud.resolve_chat_mode(m, admin=False) for m in (None, *ALL)] == ["single"] * 5
    assert [cloud.resolve_chat_mode(m, admin=True) for m in (None, *ALL)] == [
        "single", "single", "single", "roles", "roles_analyst"]
    assert cloud.chat_modes(admin=False) == ["auto"] and cloud.chat_modes(admin=True) == ALL


# -- a request's mode -------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["single", "roles", "roles_analyst"])
def test_a_local_host_runs_the_mode_a_request_names(env, revit, model, mode):
    client = host(env, local=True)
    assert client.get("/config.json").json()["features"]["chatModes"] == ALL
    script_for(model, mode)
    resp = client.post("/api/chat", json={"message": "levels?", "mode": mode}, headers=HEADERS)
    frames = sse(resp.text)
    assert tokens(frames) == "L1 and L2." and model.roles == ROLES_OF[mode]
    stages = events(frames, "stage")
    assert stages[0]["mode"] == mode and all("mode" not in s for s in stages[1:])   # the first one only
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert {r["mode"] for r in rows} == {mode}


def test_a_report_runs_in_its_requests_mode(env, revit, model):
    """single: the one loop reports; roles: the supervisor."""
    client = host(env, local=True)
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})), text_turn("card"))
    resp = client.post("/api/chat", json={"message": "a wall", "mode": "roles"}, headers=HEADERS)
    sid = resp.headers["X-Session-Id"]
    model.script("single", text_turn("It ran."))
    single = client.post("/api/chat", json={"execution": {"success": True}, "session_id": sid, "mode": "single"},
                         headers=HEADERS)
    assert tokens(sse(single.text)) == "It ran." and model.roles[-1] == "single"
    model.script("supervisor", json_turn(DONE))
    roles = client.post("/api/chat", json={"execution": {"success": True, "evidence_id": "ev_1"}, "session_id": sid,
                                           "mode": "roles"}, headers=HEADERS)
    assert events(sse(roles.text), "stage")[0] == {"role": "supervisor", "model": "fake-1", "status": "start",
                                                   "mode": "roles"}


def test_a_cloud_host_honours_a_mode_for_an_admin_only(env, revit, model):
    client = host(env)                                   # cloud, the fixture's ADMIN_PASSWORD
    assert client.get("/config.json").json()["features"]["chatModes"] == ["auto"]
    assert client.get("/config.json", headers=ADMIN).json()["features"]["chatModes"] == ALL
    assert client.get("/config.json", headers={"X-Admin-Token": "wrong"}).json()["features"]["chatModes"] == ["auto"]
    # A visitor's mode is ignored: the default runs.
    script_for(model, "roles")
    resp = client.post("/api/chat", json={"message": "levels?", "mode": "single"}, headers=HEADERS)
    assert model.roles == ROLES_OF["roles"] and events(sse(resp.text), "stage")[0]["mode"] == "roles"
    model.roles.clear()
    script_for(model, "roles")
    client.post("/api/chat", json={"message": "levels?", "mode": "roles_analyst"},
                headers={**HEADERS, "X-Admin-Token": "wrong"})
    assert model.roles == ROLES_OF["roles"]
    # The admin's is honoured.
    model.roles.clear()
    script_for(model, "single")
    resp = client.post("/api/chat", json={"message": "levels?", "mode": "single"}, headers={**HEADERS, **ADMIN})
    assert model.roles == ROLES_OF["single"] and events(sse(resp.text), "stage")[0]["mode"] == "single"


@pytest.mark.parametrize("mode", ["fast", "", "AUTO", "Single", 1, True, ["roles"]])
def test_a_mode_that_is_not_one_of_the_four_is_422(env, model, mode):
    client = host(env, local=True)
    resp = client.post("/api/chat", json={"message": "hi", "mode": mode}, headers=HEADERS)
    assert resp.status_code == 422
    assert "mode" in str(resp.json()["detail"][0]["loc"])
    assert model.requests == []
    # null is "not given": the default
    model.script("planner", text_turn("hi"))
    resp = client.post("/api/chat", json={"message": "hi", "mode": None}, headers=HEADERS)
    assert resp.status_code == 200 and events(sse(resp.text), "stage")[0]["mode"] == "roles"


def test_without_the_bridge_the_mode_is_single(env, model):
    client = host(env, local=True)
    model.script("single", text_turn("guessing"))
    resp = client.post("/api/chat", json={"message": "a wall", "bridge": False, "mode": "roles_analyst"},
                       headers=HEADERS)
    assert model.roles == ["single"] and events(sse(resp.text), "stage")[0]["mode"] == "single"


# -- the usage record ---------------------------------------------------------------------------------

def test_the_runs_report_their_mode(env, revit, model):
    client = host(env, local=True)
    for label, modes in (("one-1", ["roles", "roles"]), ("two-1", ["single"]), ("mixed-1", ["roles", "single"])):
        for mode in modes:
            script_for(model, mode)
            resp = client.post("/api/chat", json={"message": "levels?", "mode": mode},
                               headers={**HEADERS, "X-Run-Label": label})
            assert resp.status_code == 200
    runs = {r["run_label"]: r for r in client.get("/api/usage/runs").json()["items"]}
    assert (runs["one-1"]["mode"], runs["two-1"]["mode"], runs["mixed-1"]["mode"]) == ("roles", "single", "mixed")
    store = get_log_store()
    store.record_call(session_id="s", turn=1, round=1, role="planner", model="m", usage={}, ms=1, run_label="old-1")
    runs = {r["run_label"]: r for r in client.get("/api/usage/runs").json()["items"]}
    assert runs["old-1"]["mode"] is None                 # a row without a mode (before it was recorded)


def test_an_older_database_gains_the_mode_column(tmp_path):
    import sqlite3
    path = tmp_path / "logs.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                        session_id TEXT NOT NULL, turn INTEGER NOT NULL, round INTEGER NOT NULL, role TEXT NOT NULL,
                        model TEXT, input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
                        ms INTEGER NOT NULL, run_label TEXT)""")
        conn.execute("INSERT INTO llm_calls (created_at, session_id, turn, round, role, ms, run_label) "
                     "VALUES ('2099-01-01 00:00:00', 's', 1, 1, 'planner', 5, 'r-1')")
    conn.close()
    from backend.log_store import InteractionLogStore
    store = InteractionLogStore(path)
    store.record_call(session_id="s", turn=2, round=1, role="planner", model="m", usage={}, ms=1, run_label="r-2",
                      mode="roles")
    assert [r["mode"] for r in store.calls_of_session("s")] == [None, "roles"]
    assert {r["run_label"]: r["mode"] for r in store.runs()} == {"r-1": None, "r-2": "roles"}
