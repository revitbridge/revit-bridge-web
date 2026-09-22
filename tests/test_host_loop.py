"""The host loop: the model's tools, propose_spec, the execution feedback and the no-bridge mode."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from revit_bridge import host_instructions

from backend import llm as llm_module
from backend.api import chat as chat_module
from backend.api.model_tools import TOOL_NAMES
from backend.session import get_session_store
from tests.fake_model import FakeModel, text_turn, tool_turn
from tests.test_api_v1 import B, WALL, spec_for

HEADERS = {"X-LLM-Base-Url": "https://model.example/v1", "X-LLM-Model": "fake-1", "X-LLM-Key": "k"}


@pytest.fixture
def model(monkeypatch):
    """Route the host's httpx client to a scripted model; tests append turns to ``fake.turns``."""
    fake = FakeModel([])
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


def sse(text: str) -> list[tuple[str, object]]:
    """SSE frames as (event, payload): ("", "token") for reply text, ("spec", {...}) and so on."""
    frames = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event, data = "", None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data == "[DONE]":
            frames.append(("done", None))
        else:
            frames.append((event, json.loads(data)))
    return frames


def tokens(frames) -> str:
    return "".join(p for e, p in frames if e == "" and isinstance(p, str))


def events(frames, name):
    return [p for e, p in frames if e == name]


# -- the whole sequence of spec 10.8 --------------------------------------------------

def test_host_loop_walks_the_bridge_flow(revit, client, model):
    # Turn 1: the designer asks; the model snapshots, asks the pack's questions, then asks the designer.
    model.turns += [
        tool_turn(("get_project_snapshot", {"categories": ["OST_Walls"]}),
                  ("missing_params", {"tool": "create_wall", "known": {"start_x": 0, "start_y": 0}, "language": "en"})),
        text_turn("Which level, L1 or L2? And where does the wall end?"),
    ]
    resp = client.post("/api/chat", json={"message": "draw a wall from the origin"}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    session_id = resp.headers["X-Session-Id"]
    frames = sse(resp.text)
    assert tokens(frames) == "Which level, L1 or L2? And where does the wall end?"
    assert events(frames, "spec") == [] and frames[-1] == ("done", None)

    # What the model was shown: the bridge prompt, the skill, the seven tools.
    first = model.requests[0]
    assert first["messages"][0]["role"] == "system"
    assert first["messages"][0]["content"].startswith(host_instructions().strip())
    assert "## Skills" in first["messages"][0]["content"] and "# revit-bridge" in first["messages"][0]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == set(TOOL_NAMES)
    assert first["messages"][-1] == {"role": "user", "content": "draw a wall from the origin"}
    assert "confirm_spec" not in {t["function"]["name"] for t in first["tools"]}

    # The tool results are the package's: a snapshot with a fingerprint, questions with real options.
    snapshot = model.tool_results("get_project_snapshot")[0]
    assert snapshot["fingerprint"] and [lv["name"] for lv in snapshot["levels"]] == ["L1", "L2"]
    questions = model.tool_results("missing_params")[0]
    assert [q["param"] for q in questions] == ["level_name", "end_x", "end_y"]
    assert [o["value"] for o in questions[0]["options"]] == ["L1", "L2"]
    session = get_session_store().get(session_id)
    assert session.snapshot_fingerprint == snapshot["fingerprint"]
    roles = [m["role"] for m in session.history]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"]
    assert [c["function"]["name"] for c in session.history[1]["tool_calls"]] == ["get_project_snapshot", "missing_params"]

    # Turn 2: the designer answers; the model proposes a spec that is not ready, then one that is.
    wrong = spec_for("create_wall", **{**WALL, "level_name": "L9"})
    right = {**spec_for("create_wall", **WALL), "snapshot_fingerprint": snapshot["fingerprint"]}
    model.turns += [
        tool_turn(("propose_spec", {"spec": wrong})),
        tool_turn(("propose_spec", {"spec": right})),
        text_turn("The spec card is in the panel; confirm it there."),
    ]
    resp = client.post("/api/chat", json={"message": "L1, to x=5000", "session_id": session_id}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)
    specs = events(frames, "spec")
    assert len(specs) == 2
    assert specs[0]["reconcile"]["ready"] is False and specs[0]["errors"] == []
    assert [(c["param"], c["kind"]) for c in specs[0]["reconcile"]["conflicts"]] == [("level_name", "not_found")]
    assert specs[1]["reconcile"]["ready"] is True and specs[1]["errors"] == []
    assert specs[1]["card"].startswith("Task: run create_wall\nTool: create_wall (run_tool)")
    assert specs[1]["spec"]["action"] == {"kind": "run_tool", "tool": "create_wall", "code": None, "code_parameters": None}
    verdicts = model.tool_results("propose_spec")
    assert [v["accepted"] for v in verdicts] == [False, True]
    assert verdicts[0]["reconcile"]["ready"] is False and verdicts[1]["errors"] == []
    assert tokens(frames) == "The spec card is in the panel; confirm it there."
    assert get_session_store().get(session_id).spec["parameters"][0]["value"] == "L1"

    # The page confirms and runs with the token; the model never saw one.
    confirmed = client.post(f"{B}/spec/confirm", json={"spec": specs[1]["spec"]}).json()
    assert "token" in confirmed
    run = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": confirmed["token"]}).json()
    assert run["success"] is True and run["validation"]["passed"] is True and run["evidence_id"].startswith("ev_")
    assert not any(confirmed["token"] in json.dumps(r) for r in model.requests)

    # Turn 3: the page reports the execution; the model gets it as a user message and reports.
    model.turns.append(text_turn(f"Created wall 4242 on L1; validator passed; evidence {run['evidence_id']}."))
    resp = client.post("/api/chat", json={"execution": run, "session_id": session_id}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)
    assert frames[0] == ("execution", run)
    assert run["evidence_id"] in tokens(frames)
    fed = model.last_messages()[-1]
    assert fed["role"] == "user"
    assert fed["content"].split("\n")[0] == chat_module.EXECUTION_PREFIX
    assert json.loads(fed["content"].split("\n", 1)[1]) == run
    assert get_session_store().get(session_id).evidence_id == run["evidence_id"]


# -- the pieces --------------------------------------------------------------------------

def test_stream_completion_assembles_fragmented_tool_calls(model):
    model.turns.append(tool_turn(("query", {"kind": "elements", "args": {"category": "OST_Walls", "limit": 5}}),
                                 ("list_tools", {}), content="Let me look. "))
    llm = llm_module.LLMSettings(base_url="https://model.example/v1", model="m", api_key="k")

    async def collect():
        return [item async for item in llm_module.stream_completion(llm, [{"role": "user", "content": "x"}],
                                                                    tools=[{"type": "function", "function": {"name": "query"}}])]

    items = asyncio.run(collect())
    assert "".join(i for i in items if isinstance(i, str)) == "Let me look. "
    calls = items[-1]
    assert [(c.id, c.name) for c in calls] == [("call_0", "query"), ("call_1", "list_tools")]
    assert json.loads(calls[0].arguments) == {"kind": "elements", "args": {"category": "OST_Walls", "limit": 5}}
    assert json.loads(calls[1].arguments) == {}
    assert model.requests[0]["tool_choice"] == "auto"


def test_tool_failures_are_results_the_model_can_read(client, model):
    """No Revit here: the tools answer the MCP error codes; a bad call is a result too."""
    model.turns += [
        tool_turn(("get_project_snapshot", {}), ("no_such_tool", {}), ("query", {"kind": "levels"})),
        text_turn("Revit is not reachable."),
    ]
    resp = client.post("/api/chat", json={"message": "what levels are there?"}, headers=HEADERS)
    assert resp.status_code == 200
    assert model.tool_results("get_project_snapshot")[0]["error"] == "revit_unreachable"
    assert model.tool_results("no_such_tool")[0]["error"] == "unknown_tool"
    query = model.tool_results("query")[0]
    assert query["error"] == "revit_unreachable" and query["kind"] == "levels"

    # Arguments that are not a JSON object.
    model.turns += [{"content": "", "tool_calls": [{"name": "list_tools", "raw": "not json"}]}, text_turn("ok")]
    resp = client.post("/api/chat", json={"message": "list"}, headers=HEADERS)
    assert resp.status_code == 200
    assert model.tool_results("list_tools")[0]["error"] == "invalid_args"


def test_propose_spec_edge_cases(client, model):
    """An unparsable spec is a verdict for the model only; no Revit still shows the card, not ready."""
    model.turns += [
        tool_turn(("propose_spec", {"spec": {"task": "x"}})),
        tool_turn(("propose_spec", {"spec": spec_for("query_levels")})),
        text_turn("noted"),
    ]
    resp = client.post("/api/chat", json={"message": "list levels"}, headers=HEADERS)
    assert resp.status_code == 200
    specs = events(sse(resp.text), "spec")
    verdicts = model.tool_results("propose_spec")
    assert verdicts[0]["accepted"] is False and verdicts[0]["errors"][0]["code"] == "invalid_spec"
    assert verdicts[0]["reconcile"] is None
    assert len(specs) == 1                                        # the unparsable one made no event
    assert specs[0]["card"].startswith("Task: run query_levels") and specs[0]["errors"] == []
    assert specs[0]["reconcile"] is None                          # no add-in in this test: the page sees why
    assert specs[0]["reconcile_error"]["error"] == "revit_unreachable"
    assert verdicts[1]["accepted"] is False and verdicts[1]["reconcile"]["error"] == "revit_unreachable"


def test_a_model_that_never_stops_calling_tools_is_cut_off(client, model):
    model.turns += [tool_turn(("list_tools", {}))] * chat_module.MAX_TOOL_ROUNDS + [text_turn("done")]
    resp = client.post("/api/chat", json={"message": "loop"}, headers=HEADERS)
    assert resp.status_code == 200 and tokens(sse(resp.text)) == "done"
    assert len(model.requests) == chat_module.MAX_TOOL_ROUNDS + 1
    assert "tools" in model.requests[-2] and "tools" not in model.requests[-1]   # the last round has no tools


def test_message_and_execution_are_exclusive(client, model):
    both = client.post("/api/chat", json={"message": "x", "execution": {"success": True}}, headers=HEADERS)
    assert both.status_code == 422 and both.json()["error"] == "invalid_args"
    neither = client.post("/api/chat", json={"session_id": None}, headers=HEADERS)
    assert neither.status_code == 422 and neither.json()["error"] == "invalid_args"
    blank = client.post("/api/chat", json={"message": "   "}, headers=HEADERS)
    assert blank.status_code == 422
    no_bridge = client.post("/api/chat", json={"execution": {"success": True}, "bridge": False}, headers=HEADERS)
    assert no_bridge.status_code == 422 and no_bridge.json()["error"] == "invalid_args"
    assert model.requests == []


def test_no_bridge_mode_sends_no_tools_and_no_skills(client, model):
    model.turns.append(text_turn("I would put it on the ground floor."))
    resp = client.post("/api/chat", json={"message": "draw a wall from the origin", "bridge": False}, headers=HEADERS)
    assert resp.status_code == 200
    frames = sse(resp.text)
    assert tokens(frames) == "I would put it on the ground floor." and frames[-1] == ("done", None)
    request = model.requests[0]
    assert "tools" not in request and "tool_choice" not in request
    system = request["messages"][0]["content"]
    assert system == chat_module.BASE_PROMPT
    assert "revit-bridge" not in system and "get_project_snapshot" not in system and "## Skills" not in system
    # The same message with the bridge gets both.
    model.turns.append(text_turn("Which level?"))
    client.post("/api/chat", json={"message": "draw a wall from the origin"}, headers=HEADERS)
    assert "tools" in model.requests[1] and "## Skills" in model.requests[1]["messages"][0]["content"]


def test_chat_with_the_bridge_needs_a_slot_when_tokens_are_required(make_client, env, model):
    env.setenv("MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "1")
    env.setenv("MCP_BRIDGE_SLOT_TOKEN_1", "top-secret")
    client = make_client()
    refused = client.post("/api/chat", json={"message": "hi"}, headers=HEADERS)
    assert refused.status_code == 403 and refused.json()["error"] == "missing_slot"
    model.turns.append(text_turn("plain"))
    assert client.post("/api/chat", json={"message": "hi", "bridge": False}, headers=HEADERS).status_code == 200


def test_history_window_starts_at_a_user_turn():
    from backend.session import Session, trim_at_user_turn

    session = Session(session_id="s")
    session.add({"role": "user", "content": "one"})
    session.add({"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]})
    session.add({"role": "tool", "tool_call_id": "c1", "content": "{}"})
    session.add({"role": "assistant", "content": "answer"})
    session.add({"role": "user", "content": "two"})
    session.add({"role": "assistant", "content": "reply"})
    assert [m["role"] for m in session.window(3)] == ["user", "assistant"]        # not "tool, assistant, user, ..."
    assert [m["content"] for m in session.window(2)] == ["two", "reply"]
    assert session.window(100) == session.history
    assert trim_at_user_turn([{"role": "tool"}, {"role": "assistant"}]) == []
