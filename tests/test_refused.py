"""revit-bridge 0.9.1: a pack that refuses its input in Revit, end to end on the real package path.

The add-in answers a snippet that threw with ``success: false`` and an ``errorMessage`` of its
own prefix ("execution failed: " in Chinese), the exception's full type name and its message; a
pack refuses by throwing an ``InvalidOperationException`` whose message starts ``REFUSED: ``
(``revit_bridge.execution.REFUSAL_MARKER``). Here the fake add-in sends exactly that for the
built-in ``create_level`` (a level of that name exists), and the host's routes and chat take what
the package's ``run_pack`` made of it: ``refused: true``, ``error`` the text after the marker, no
hint, a ledger line that says refused, the token spent, the usage counters untouched; the chat
reports it, and with the roles it is never sent back."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import llm as llm_module
from backend import roles
from backend.api import bridge as bridge_module
from backend.api import chat as chat_module
from backend.session import get_session_store
from tests.fake_model import FakeModel, RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit, model_handler
from tests.test_api_v1 import B, confirm, spec_for
from tests.test_host_loop import HEADERS, events, sse, tokens

ADDIN_PREFIX = "\u6267\u884c\u5931\u8d25: "          # the add-in's "execution failed: "
REFUSAL_TEXT = 'A level named "L1" already exists. Choose another name.'
ADDIN_ERROR = f"{ADDIN_PREFIX}System.InvalidOperationException: REFUSED: {REFUSAL_TEXT}"
LEVEL = {"name": "L1", "elevation": 6000}
# What the run route answers for a refused run: the package's ExecutionResult without hint.
REFUSED_KEYS = {"success", "tool", "result", "error", "validation", "evidence_id", "warnings",
                "preconditions_failed", "refused"}


def refusing_addin():
    """The suite's small model; the create_level pack's own code is answered as the add-in
    answers a pack that threw its refusal (nothing kept: the add-in rolled back)."""
    base = model_handler()

    def handler(request: dict) -> dict:
        if request.get("method") == "send_code_to_revit" and "Level.Create(" in request["params"]["code"]:
            return FakeRevit.code_result(request.get("id"), None, success=False, error=ADDIN_ERROR)
        return base(request)
    return handler


@pytest.fixture
def addin(env):
    with FakeRevit(refusing_addin()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        yield fake


def run_refused(client) -> tuple[dict, str]:
    token = confirm(client, spec_for("create_level", **LEVEL))
    resp = client.post(f"{B}/tools/create_level/run", json={"params": LEVEL, "token": token})
    assert resp.status_code == 200, resp.text
    return resp.json(), token


def test_the_run_route_answers_the_package_s_refusal(addin, client):
    out, token = run_refused(client)
    assert set(out) == REFUSED_KEYS and "hint" not in out
    assert out == {"success": False, "tool": "create_level", "result": None, "error": REFUSAL_TEXT,
                   "validation": None, "evidence_id": out["evidence_id"], "warnings": [],
                   "preconditions_failed": [], "refused": True}
    assert out["evidence_id"].startswith("ev_")
    # The pack's code did reach the add-in.
    assert [r for r in addin.requests if "Level.Create(" in str(r.get("params", {}).get("code", ""))]
    # The ledger line says refused; re-validating it has nothing to assert.
    record = bridge_module.get_ledger().get(out["evidence_id"])
    assert record["refused"] is True and record["success"] is False and record["error"] == REFUSAL_TEXT
    again = client.post(f"{B}/evidence/{out['evidence_id']}/validate")
    assert again.status_code == 400 and again.json()["error"] == "not_executed"
    assert [r.get("refused") for r in client.get(f"{B}/evidence").json()] == [True]
    # Neither a success nor a failure of the pack: its counters did not move.
    listed = next(t for t in client.get(f"{B}/tools").json() if t["name"] == "create_level")
    assert listed["used"] == 0
    # The token was spent: the run reached Revit.
    spent = client.post(f"{B}/tools/create_level/run", json={"params": LEVEL, "token": token}).json()
    assert spent["error"] == "confirmation_invalid" and spent["reason"] == "used"


@pytest.fixture
def single_model(monkeypatch):
    fake = FakeModel([])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": fake.transport}))
    return fake


def test_the_one_loop_reports_it_as_the_page_received_it(addin, client, single_model):
    single_model.turns += [tool_turn(("propose_spec", {"spec": spec_for("create_level", **LEVEL)})),
                           text_turn("The card is below.")]
    first = client.post("/api/chat", json={"message": "a level L1 at 6000"}, headers=HEADERS)
    session_id = first.headers["X-Session-Id"]
    out, _ = run_refused(client)
    single_model.turns.append(text_turn("The tool refused: a level L1 is already there."))
    frames = sse(client.post("/api/chat", json={"execution": out, "session_id": session_id}, headers=HEADERS).text)
    assert frames[0] == ("execution", out)
    fed = single_model.last_messages()[-1]
    assert fed["content"].split("\n")[0] == chat_module.EXECUTION_PREFIX
    assert json.loads(fed["content"].split("\n", 1)[1]) == out          # refused: true reaches the model


@pytest.fixture
def role_model(monkeypatch):
    fake = RoleScriptedModel()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": fake.transport}))
    return fake


def test_the_roles_never_send_it_back(roles_env, role_model):
    with FakeRevit(refusing_addin()) as fake:
        roles_env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        from backend.main import create_app
        with TestClient(create_app()) as client:
            role_model.script("analyst", json_turn({"route": "tool", "tool": "create_level", "facts_needed": [],
                                                    "note": ""}))
            role_model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_level", **LEVEL)})),
                              text_turn("The card is below."))
            first = client.post("/api/chat", json={"message": "a level L1 at 6000"}, headers=HEADERS)
            session_id = first.headers["X-Session-Id"]
            session = get_session_store().get(session_id)
            confirmed = client.post(f"{B}/spec/confirm", json={"spec": session.spec, "session_id": session_id}).json()
            out = client.post(f"{B}/tools/create_level/run", json={"params": LEVEL, "token": confirmed["token"]}).json()
            assert out["refused"] is True and out["error"] == REFUSAL_TEXT
            assert roles.refusal_kind(out) == "the tool refused the input in Revit; nothing was kept"
            # The supervisor asks for a retry; the host holds it to the package's verdict.
            role_model.script("supervisor", json_turn({"verdict": "retry", "report": "Try again.", "fix": "rerun"}))
            calls = len(role_model.requests)
            frames = sse(client.post("/api/chat", json={"execution": out, "session_id": session_id},
                                     headers=HEADERS).text)
            assert len(role_model.requests) == calls + 1 and role_model.roles[-1] == "supervisor"   # no planner
            assert tokens(frames) == (f"This run is not sent back: the tool refused the input in Revit; nothing was "
                                      f"kept. {REFUSAL_TEXT}")
            form, = events(frames, "questions")
            assert form["questions"][0]["text"] == roles.DEFAULT_QUESTION
            assert [s["role"] for s in events(frames, "stage")] == ["supervisor", "supervisor"]
            assert get_session_store().get(session_id).role_task.retries == 0
            # The supervisor saw refused: true and the pack's text, no hint.
            sent = json.loads(role_model.requests_of("supervisor")[0]["messages"][1]["content"].split("\n", 1)[1])
            assert sent["execution"]["refused"] is True and sent["execution"]["error"] == REFUSAL_TEXT
            assert "hint" not in sent["execution"]
