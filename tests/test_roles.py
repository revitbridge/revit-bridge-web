"""W6: the chat in three roles - analyst, planner, supervisor - each with its own text, tools
and model, and the hand-offs between them (backend/roles.py, backend/api/chat.py).

A scripted model answers each role from its own script (tests/fake_model.py
``RoleScriptedModel``: the role is read off the system prompt). These tests run on the
host's default chat (``roles_env``); the rest of the suite runs LLM_SINGLE_MODEL=1."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import llm as llm_module
from backend import roles
from backend.api import chat as chat_module
from backend.api.model_tools import TOOL_DEFINITIONS, WORKBENCH_LINK
from backend.log_store import get_log_store
from backend.session import get_session_store
from tests.conftest import builtin_pack_names
from tests.fake_model import USAGE_SHAPES, RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_host_loop import HEADERS, events, sse, tokens

ROUTING_TOOL = {"route": "tool", "tool": "create_wall", "facts_needed": ["end point"], "note": "Ask for the end."}
ROUTING_QUERY = {"route": "query", "facts_needed": [], "note": "Read the levels."}
DONE = {"verdict": "done", "report": "One wall was created on L1; the validator passed."}


@pytest.fixture
def model(monkeypatch):
    fake = RoleScriptedModel()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


@pytest.fixture
def client(roles_env):
    from backend.main import create_app
    with TestClient(create_app()) as tc:
        yield tc


def names(frames) -> list[str]:
    return [e or "token" for e, _ in frames]


def tool_names(body: dict) -> list[str]:
    return [t["function"]["name"] for t in body.get("tools") or []]


def user_text(body: dict) -> str:
    return [m for m in body["messages"] if m["role"] == "user"][-1]["content"]


def wall_turn(client, model, message="a wall on L1 from (0,0) to (5000,0), 3000 high") -> str:
    """Turn 1 of the tool route: the analyst routes, the planner proposes the spec. The session id."""
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})),
                 text_turn("The card is below."))
    resp = client.post("/api/chat", json={"message": message}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.headers["X-Session-Id"]


def report(client, session_id, execution) -> list:
    resp = client.post("/api/chat", json={"execution": execution, "session_id": session_id}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    return sse(resp.text)


FAILED = {"success": False, "tool": "create_wall", "error": "Level 'L9' not found", "evidence_id": "ev_1",
          "warnings": []}
VALIDATION_FAILED = {
    "success": False, "tool": "create_wall", "error": "validation_failed", "evidence_id": "ev_2",
    "result": {"Status": "Success", "ElementId": 4242},
    "validation": {"validator": "element_count", "passed": False,
                   "checks": [{"name": "count", "passed": False, "detail": "walls 1 -> 1, expected 2"},
                              {"name": "level", "passed": True, "detail": "on L1"}],
                   "before": {"walls": 1}, "after": {"walls": 1}},
    "warnings": []}
OK = {"success": True, "tool": "create_wall", "error": None, "evidence_id": "ev_3",
      "result": {"Status": "Success", "ElementId": 4242},
      "validation": {"validator": "element_count", "passed": True,
                     "checks": [{"name": "count", "passed": True, "detail": "walls 1 -> 2"}],
                     "before": {"walls": 1}, "after": {"walls": 2}}, "warnings": []}


# -- the routes ---------------------------------------------------------------------------

def test_route_chat_is_the_analysts_note_and_nothing_else(client, model):
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "Hello - ask me for a wall."},
                                      usage=USAGE_SHAPES["openai"][0]))
    resp = client.post("/api/chat", json={"message": "hi"}, headers=HEADERS)
    frames = sse(resp.text)
    assert names(frames) == ["stage", "usage", "stage", "token", "done"]
    assert tokens(frames) == "Hello - ask me for a wall."
    # The turn's first stage event names the chat mode it runs in.
    assert events(frames, "stage") == [{"role": "analyst", "model": "fake-1", "status": "start", "mode": "roles_analyst"},
                                       {"role": "analyst", "model": "fake-1", "status": "end"}]
    assert events(frames, "usage")[0]["role"] == "analyst" and events(frames, "usage")[0]["round"] == 1
    assert model.roles == ["analyst"]
    body = model.requests[0]
    # The analyst: its own text and host line, no tools, one user message.
    assert "tools" not in body and "tool_choice" not in body
    assert body["messages"][0] == {"role": "system", "content": chat_module.build_role_prompt("analyst")}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    sent = body["messages"][1]["content"]
    # First the tool list: every pack of the wheel, one "- <name>: <description>" line each, by name.
    listed = sent.split("\n\n", 1)[0].split("\n")
    assert listed[0] == "Tools:"
    assert [line.split(": ", 1)[0] for line in listed[1:]] == [f"- {name}" for name in sorted(builtin_pack_names())]
    assert "\n\nDigest of the open model:\nnot available (revit_unreachable)\n\n" in sent   # no add-in here
    assert sent.endswith("Designer's message:\nhi")
    # The run count of a pack is not in the list: the bytes stay the same after a run.
    assert '"used"' not in sent and "Open task" not in sent
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert [(r["role"], r["model"], r["turn"], r["round"]) for r in rows] == [("analyst", "fake-1", 1, 1)]
    # The planner never saw this turn.
    assert get_session_store().get(resp.headers["X-Session-Id"]).history == []


def test_route_none_points_to_the_workbench(client, model):
    model.script("analyst", json_turn({"route": "none", "facts_needed": [], "note": "No tool here pipes sprinklers."}),
                 json_turn({"route": "none", "facts_needed": [],
                            "note": f"No tool for that; make one in the {WORKBENCH_LINK}."}))
    first = client.post("/api/chat", json={"message": "sprinkler pipes"}, headers=HEADERS)
    assert tokens(sse(first.text)) == f"No tool here pipes sprinklers.\n\n{WORKBENCH_LINK}"
    second = client.post("/api/chat", json={"message": "pipes again"}, headers=HEADERS)
    assert tokens(sse(second.text)) == f"No tool for that; make one in the {WORKBENCH_LINK}."
    assert model.roles == ["analyst", "analyst"]
    # The analyst's prompt asks for the link (its one host line).
    assert model.requests[0]["messages"][0]["content"].endswith("\n\n" + roles.ANALYST_WORKBENCH_LINE)


def test_route_query_runs_the_planner_with_the_read_tools_only(revit, client, model):
    model.script("analyst", json_turn(ROUTING_QUERY, usage=USAGE_SHAPES["deepseek"][0]))
    model.script("planner", tool_turn(("query", {"kind": "levels"}), usage=USAGE_SHAPES["openai"][0]),
                 text_turn("Two levels: L1 and L2.", usage=USAGE_SHAPES["gemini"][0]))
    resp = client.post("/api/chat", json={"message": "what levels are there?"}, headers=HEADERS)
    frames = sse(resp.text)
    assert names(frames) == ["stage", "usage", "stage",
                             "stage", "usage", "tool", "tool", "token", "token", "token", "usage", "stage", "done"]
    assert tokens(frames) == "Two levels: L1 and L2."
    assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [
        ("analyst", "start"), ("analyst", "end"), ("planner", "start"), ("planner", "end")]
    assert [(u["role"], u["round"]) for u in events(frames, "usage")] == [("analyst", 1), ("planner", 2), ("planner", 3)]
    assert [(t["id"], t["role"]) for t in events(frames, "tool")] == [("1.2.1", "planner"), ("1.2.1", "planner")]
    assert model.roles == ["analyst", "planner", "planner"]
    planner = model.requests_of("planner")[0]
    assert tool_names(planner) == ["get_project_snapshot", "query", "ask_designer"]
    assert planner["tools"] == roles.ROUTE_DEFINITIONS["query"]
    assert planner["messages"][0] == {"role": "system", "content": chat_module.build_role_prompt("planner")}
    # The planner's message: the designer's words, then the analyst's routing - not the
    # analyst's prompt, tool list or digest.
    assert user_text(planner) == ("what levels are there?\n\n" + roles.ROUTING_PREFIX + " "
                                  + json.dumps(ROUTING_QUERY, ensure_ascii=False))
    dumped = json.dumps(planner)
    assert "Digest of the open model" not in dumped and "You are the analyst" not in dumped
    # The digest the analyst got is the package's, from a snapshot the host took.
    digest = user_text(model.requests[0]).split("Digest of the open model:\n", 1)[1].split("\n\n")[0]
    assert digest.startswith("Document: Project1") and "Levels (2): L1, L2" in digest and len(digest) <= 1200
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert [(r["role"], r["round"]) for r in rows] == [("analyst", 1), ("planner", 2), ("planner", 3)]
    assert [(r["input"], r["output"]) for r in rows] == [
        USAGE_SHAPES[shape][1][:2] for shape in ("deepseek", "openai", "gemini")]


def test_route_tool_proposes_and_the_supervisor_reports(revit, client, model):
    session_id = wall_turn(client, model)
    planner = model.requests_of("planner")[0]
    assert tool_names(planner) == [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert planner["tools"] == roles.ROUTE_DEFINITIONS["tool"]
    session = get_session_store().get(session_id)
    assert session.role_task.route == "tool" and session.role_task.tool == "create_wall"
    assert session.role_task.last == "The card is below."

    # The page confirms (with the session) and runs; the execution goes to the supervisor.
    proposed = session.spec
    confirmed = client.post(f"{B}/spec/confirm", json={"spec": proposed, "session_id": session_id}).json()
    run = client.post(f"{B}/tools/create_wall/run", json={"params": WALL, "token": confirmed["token"]}).json()
    assert run["success"] is True
    model.script("supervisor", json_turn({**DONE, "report": f"One wall; validator passed ({run['evidence_id']})."},
                                         usage=USAGE_SHAPES["openai"][0]))
    confirmed_spec = session.confirmed_spec
    assert confirmed_spec is not None and session.digests          # the analyst's digest of this device
    frames = report(client, session_id, run)
    assert names(frames) == ["execution", "stage", "usage", "stage", "token", "done"]
    assert tokens(frames) == f"One wall; validator passed ({run['evidence_id']})."
    assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [("supervisor", "start"), ("supervisor", "end")]
    body = model.requests_of("supervisor")[0]
    assert body["messages"][0] == {"role": "system", "content": chat_module.build_role_prompt("supervisor")}
    assert tool_names(body) == ["query"]
    # Its one message: the confirmed spec, the result's summary and the validator's checks;
    # not the planner's transcript, the analyst's routing or the validator's samples.
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    head, payload = body["messages"][1]["content"].split("\n", 1)
    assert head == roles.SUPERVISOR_PREFIX
    payload = json.loads(payload)
    assert payload["confirmed_spec"] == confirmed_spec and "proposed_spec" not in payload
    assert payload["execution"]["evidence_id"] == run["evidence_id"] and payload["execution"]["success"] is True
    assert set(payload["execution"]["validation"]) == {"validator", "passed", "checks"}
    assert payload["execution"]["validation"]["checks"] == run["validation"]["checks"]
    assert payload["sent_back_so_far"] == 0 and payload["max_sent_back"] == 2
    dumped = json.dumps(body)
    assert "propose_spec" not in dumped and "Analyst's routing" not in dumped
    assert session.role_task is None and session.pending_verdict["verdict"] == "done"
    # The run is reported: its confirmation is used up, and the analyst reads the model again.
    assert session.confirmed_spec is None and session.digests == {}

    # The next message: the analyst sees no open task, the planner learns the verdict once.
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", text_turn("ok"))
    client.post("/api/chat", json={"message": "and now?", "session_id": session_id}, headers=HEADERS)
    assert "Open task" not in user_text(model.requests_of("analyst")[-1])
    sent = user_text(model.requests_of("planner")[-1])
    assert sent.startswith("and now?\n\n" + roles.VERDICT_PREFIX + " ")
    assert session.pending_verdict is None
    # The planner's conversation grew by appending: its earlier messages went out unchanged.
    first, later = model.requests_of("planner")[0], model.requests_of("planner")[-1]
    assert later["messages"][:len(first["messages"])] == first["messages"]


def test_the_confirmed_spec_reaches_the_supervisor_not_the_last_proposal(revit, client, model):
    session_id = wall_turn(client, model)
    session = get_session_store().get(session_id)
    draft = {**session.spec, "interpretations": [{"param": None, "text": "on L1", "confirmed": True}]}
    assert client.post(f"{B}/spec/confirm", json={"spec": draft, "session_id": session_id}).status_code == 200
    model.script("supervisor", json_turn(DONE))
    report(client, session_id, OK)
    sent = json.loads(user_text(model.requests_of("supervisor")[0]).split("\n", 1)[1])
    assert sent["confirmed_spec"]["interpretations"][0]["text"] == "on L1"
    # A second report without a new confirmation (the bench's fixed result does this): the
    # planner's last proposal, labelled as proposed - never as confirmed.
    model.script("supervisor", json_turn(DONE))
    report(client, session_id, OK)
    sent = json.loads(user_text(model.requests_of("supervisor")[1]).split("\n", 1)[1])
    assert "confirmed_spec" not in sent and sent["proposed_spec"] == session.spec


# -- the supervisor's verdicts --------------------------------------------------------------

def test_retry_goes_back_to_the_planner_twice_then_the_host_asks(revit, client, model):
    session_id = wall_turn(client, model)
    for attempt in (1, 2):
        model.script("supervisor", json_turn({"verdict": "retry", "report": f"Failed: no level L9 ({attempt}).",
                                              "fix": "use level L1"}))
        model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})),
                     text_turn(f"New card {attempt}."))
        frames = report(client, session_id, FAILED)
        assert tokens(frames) == f"Failed: no level L9 ({attempt}).\n\nNew card {attempt}."
        assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [
            ("supervisor", "start"), ("supervisor", "end"), ("planner", "start"), ("planner", "end")]
        assert len(events(frames, "spec")) == 1                 # the new card for the page
        sent = user_text(model.requests_of("planner")[-2])
        assert sent.startswith(roles.RETRY_PREFIX + " ")
        assert json.loads(sent.split(" ", len(roles.RETRY_PREFIX.split(" ")))[-1]) == {
            "verdict": "retry", "report": f"Failed: no level L9 ({attempt}).", "fix": "use level L1",
            "retry": attempt, "retries_left": 2 - attempt}
        sup = json.loads(user_text(model.requests_of("supervisor")[-1]).split("\n", 1)[1])
        assert sup["sent_back_so_far"] == attempt - 1
    # The third time the supervisor says retry, the host asks instead; no planner call.
    calls = len(model.requests)
    model.script("supervisor", json_turn({"verdict": "retry", "report": "Failed again.", "fix": "use level L2"}))
    frames = report(client, session_id, FAILED)
    assert len(model.requests) == calls + 1 and model.roles[-1] == "supervisor"
    reply = tokens(frames)
    assert reply.startswith("Failed again.\n\nThe task was sent back 2 times")
    assert events(frames, "questions")[0]["questions"][0]["text"] == "What should change? (last suggestion: use level L2)"
    assert [s["role"] for s in events(frames, "stage")] == ["supervisor", "supervisor"]
    session = get_session_store().get(session_id)
    assert session.role_task.retries == 2 and session.pending_verdict["verdict"] == "ask"
    # The designer writes again: a new attempt, the send-backs count from 0.
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Which level then?"))
    client.post("/api/chat", json={"message": "try L1 instead", "session_id": session_id}, headers=HEADERS)
    assert session.role_task.retries == 0 and session.role_task.tool == "create_wall"


def test_a_retry_clears_the_verdict_the_planner_has_not_seen(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "ask", "report": "Failed.", "question": "Again?"}))
    report(client, session_id, FAILED)
    session = get_session_store().get(session_id)
    assert session.pending_verdict["verdict"] == "ask"
    model.script("supervisor", json_turn({"verdict": "retry", "report": "No L9.", "fix": "use L1"}))
    model.script("planner", text_turn("New card."))
    report(client, session_id, FAILED)
    assert session.pending_verdict is None                    # the retry message carried the verdict
    retry = user_text(model.requests_of("planner")[-1])
    assert retry.startswith(roles.RETRY_PREFIX) and roles.VERDICT_PREFIX not in retry


def test_a_run_that_succeeded_is_not_sent_back(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "retry", "report": "It worked, but L2 looks better.",
                                          "fix": "use level L2"}))
    calls = len(model.requests)
    frames = report(client, session_id, OK)
    assert len(model.requests) == calls + 1                    # no planner: the designer decides
    assert tokens(frames) == ("It worked, but L2 looks better.\n\nRevit reported success and the validator did not "
                              "fail, so the host does not send this run back; you decide whether anything should "
                              "change.")
    assert events(frames, "questions")[0]["questions"][0]["text"] == "Should anything change? (the supervisor suggested: use level L2)"
    assert get_session_store().get(session_id).role_task.retries == 0
    # The send-back cap on a run that succeeded does not say it failed (validator failed, Revit ran).
    capped = roles.enforce(roles.Verdict("retry", "Checks off.", fix="redo"),
                           {**VALIDATION_FAILED, "success": True}, roles.MAX_RETRIES)
    assert "sent back 2 times; the host" in capped.report and "did not succeed" not in capped.report
    capped = roles.enforce(roles.Verdict("retry", "Failed.", fix="redo"), FAILED, roles.MAX_RETRIES)
    assert "sent back 2 times and still did not succeed" in capped.report


def test_ask_shows_the_report_and_the_question(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "ask", "report": "Revit refused: the view is locked.",
                                          "question": "Unlock the view and run it again?"}))
    frames = report(client, session_id, FAILED)
    assert tokens(frames) == "Revit refused: the view is locked."
    form, = events(frames, "questions")
    question, = form["questions"]
    assert form["ask_id"] and question["allow_other"] and question["text"] == "Unlock the view and run it again?"
    session = get_session_store().get(session_id)
    assert session.role_task.last == tokens(frames) + "\n\n" + question["text"]
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("ok"))
    client.post("/api/chat", json={"message": "yes", "session_id": session_id}, headers=HEADERS)
    sent = user_text(model.requests_of("planner")[-1])
    assert sent.startswith("yes\n") and "Unlock the view and run it again?" in sent
    for role in ("analyst", "planner", "supervisor"):
        prompt = model.requests_of(role)[0]["messages"][0]["content"]
        if role == "supervisor":
            assert chat_module.CARD_GUIDANCE not in prompt
            assert "on the card in this chat" in prompt and "on the card in this chat reply" not in prompt
        else:
            assert "确认并执行" in prompt and "卡片在我上一条提交它的回复里" in prompt
        if role == "analyst":
            assert "When a card is pending confirmation" in prompt
        if role == "planner":
            assert "other: true marks the designer's own words" in prompt
        assert "wait for an explicit yes" not in prompt and "in this host\'s interface" not in prompt
        assert "confirm in revit" not in prompt.lower() and "revit panel" not in prompt.lower()


def test_a_failed_validator_is_never_done(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "done", "report": "All good!"}))
    frames = report(client, session_id, VALIDATION_FAILED)
    reply = tokens(frames)
    assert "All good" not in reply
    assert reply == ("The validator did not pass, so this is not done:\n"
                     "- count (failed): walls 1 -> 1, expected 2")
    assert events(frames, "questions")[0]["questions"][0]["text"] == "What should change?"
    assert get_session_store().get(session_id).pending_verdict["verdict"] == "ask"
    # Nor is a Revit error.
    model.script("supervisor", json_turn({"verdict": "done", "report": "Done."}))
    assert tokens(report(client, session_id, FAILED)).startswith(
        "Revit did not report success, so this is not done: Level 'L9' not found")


# Failures that are refusals by nature, as the package's fields say (revit_bridge.execution):
# 0.9.1's refused (the pack refused the input in Revit; error is the text after the marker), and on
# any version the runs it stopped before they changed anything.
REFUSALS = {
    "refused": ({"success": False, "tool": "create_wall", "error": "the wall would cut the stair", "refused": True,
                 "evidence_id": "ev_r", "warnings": []},
                "the tool refused the input in Revit; nothing was kept. the wall would cut the stair"),
    "preconditions": ({"success": False, "tool": "create_wall", "error": "preconditions_failed",
                       "preconditions_failed": ["the active document is read-only"], "evidence_id": "ev_p",
                       "warnings": []}, "a precondition failed. preconditions_failed"),
    "validator_before": ({"success": False, "tool": "create_wall", "error": "validator_before_failed",
                          "evidence_id": "ev_v", "warnings": ["validator.before: TimeoutError"]},
                         "the validator could not take its first sample. validator_before_failed"),
    "blocked": ({"success": False, "error": "blocked", "warnings": ["File.Delete"]},
                "the code was blocked. blocked"),
    "not_recorded": ({"success": False, "error": "Parameter validation failed: height must be a number"},
                     "nothing was run or recorded. Parameter validation failed: height must be a number"),
}


@pytest.mark.parametrize("name", list(REFUSALS))
def test_a_refusal_by_nature_is_never_sent_back(revit, client, model, name):
    execution, why = REFUSALS[name]
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "retry", "report": "Retrying.", "fix": "try again"}))
    calls = len(model.requests)
    frames = report(client, session_id, execution)
    assert len(model.requests) == calls + 1                    # no planner
    assert tokens(frames) == f"This run is not sent back: {why}"
    assert events(frames, "questions")[0]["questions"][0]["text"] == roles.DEFAULT_QUESTION
    assert get_session_store().get(session_id).role_task.retries == 0
    # Nor called done; the supervisor's own ask stands.
    model.script("supervisor", json_turn({"verdict": "done", "report": "Fine."}),
                 json_turn({"verdict": "ask", "report": "Refused.", "question": "Change the input?"}))
    assert tokens(report(client, session_id, execution)).startswith("This run is not sent back")
    frames = report(client, session_id, execution)
    assert tokens(frames) == "Refused."
    assert events(frames, "questions")[0]["questions"][0]["text"] == "Change the input?"


def test_a_failure_that_ran_may_be_sent_back():
    """The other failures - Revit threw, the validator found the model unchanged - stay retry."""
    for execution in (FAILED, VALIDATION_FAILED):
        assert roles.refusal_kind(execution) is None
        assert roles.enforce(roles.Verdict("retry", "r", fix="f"), execution, 0).verdict == "retry"


# -- output that is not the JSON asked for ---------------------------------------------------

def test_an_unreadable_analyst_gets_one_repair_call(client, model):
    model.script("analyst", text_turn("Sounds like a question about Revit."),
                 json_turn({"route": "chat", "facts_needed": [], "note": "Yes."}))
    frames = sse(client.post("/api/chat", json={"message": "is Revit BIM?"}, headers=HEADERS).text)
    assert tokens(frames) == "Yes." and model.roles == ["analyst", "analyst"]
    repair = model.requests[1]["messages"]
    assert repair[:2] == model.requests[0]["messages"]
    assert repair[2:] == [{"role": "assistant", "content": "Sounds like a question about Revit."},
                          {"role": "user", "content": roles.REPAIR_INSTRUCTION}]
    assert "tools" not in model.requests[1]
    assert [(u["role"], u["round"]) for u in events(frames, "usage")] == [("analyst", 1), ("analyst", 2)]


def test_an_analyst_unreadable_twice_hands_the_message_to_the_whole_planner(client, model):
    model.script("analyst", text_turn("hmm"), json_turn({"route": "maybe", "note": "?"}))
    model.script("planner", text_turn("Which wall?"))
    frames = sse(client.post("/api/chat", json={"message": "a wall"}, headers=HEADERS).text)
    assert tokens(frames) == "Which wall?" and model.roles == ["analyst", "analyst", "planner"]
    planner = model.requests_of("planner")[0]
    assert planner["tools"] == roles.ROUTE_DEFINITIONS["tool"]
    assert user_text(planner).endswith(roles.ROUTING_PREFIX + ' {"route": "tool", "facts_needed": [], "note": ""}')


@pytest.mark.parametrize("reply", [
    "```json\n" + json.dumps({"route": "chat", "facts_needed": [], "note": "Hi."}) + "\n```",
    "Here it is: " + json.dumps({"route": "chat", "note": "Hi."}),
])
def test_a_fenced_or_prefixed_object_needs_no_repair(client, model, reply):
    model.script("analyst", text_turn(reply))
    assert tokens(sse(client.post("/api/chat", json={"message": "hi"}, headers=HEADERS).text)) == "Hi."
    assert model.roles == ["analyst"]


def test_a_tool_the_list_does_not_have_is_repaired(client, model):
    model.script("analyst", json_turn({"route": "tool", "tool": "create_sprinkler", "facts_needed": [], "note": ""}),
                 json_turn({"route": "none", "facts_needed": [], "note": "No such tool."}))
    frames = sse(client.post("/api/chat", json={"message": "sprinklers"}, headers=HEADERS).text)
    assert model.roles == ["analyst", "analyst"] and tokens(frames).startswith("No such tool.")


def test_an_unreadable_supervisor_leaves_the_report_to_the_host(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", text_turn("It worked I think"), text_turn("Really, it worked."))
    calls = len(model.requests)
    frames = report(client, session_id, VALIDATION_FAILED)
    assert len(model.requests) == calls + 2                       # the call and one repair, no more
    assert tokens(frames) == (
        "The execution result, as the host received it (the supervisor's reply could not be read):\n"
        'success: false\nerror: validation_failed\nvalidator element_count: did not pass\n'
        "- count (failed): walls 1 -> 1, expected 2\n- level (passed): on L1\nevidence: ev_2")
    assert events(frames, "questions")[0]["questions"][0]["text"] == roles.DEFAULT_QUESTION
    assert get_session_store().get(session_id).pending_verdict["verdict"] == "ask"


# -- each role's tools ------------------------------------------------------------------------

def test_a_role_cannot_run_a_tool_outside_its_subset(revit, client, model, monkeypatch):
    ran: list[str] = []
    real = chat_module.call_tool

    async def spy(name, arguments, session):
        ran.append(name)
        return await real(name, arguments, session)

    monkeypatch.setattr(chat_module, "call_tool", spy)
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)}),
                                      ("query", {"kind": "levels"})),
                 text_turn("Two levels."))
    resp = client.post("/api/chat", json={"message": "levels?"}, headers=HEADERS)
    frames = sse(resp.text)
    assert ran == ["query"] and events(frames, "spec") == []
    closing = events(frames, "tool")[1::2]
    assert [(t["name"], t["status"]) for t in closing] == [("propose_spec", "error"), ("query", "ok")]
    assert closing[0]["error"] == "tool_not_offered: the planner has only: get_project_snapshot, query, ask_designer"
    assert get_session_store().get(resp.headers["X-Session-Id"]).spec is None

    # The supervisor has query alone.
    session_id = wall_turn(client, model)
    ran.clear()
    model.script("supervisor", tool_turn(("reconcile", {"spec": spec_for("create_wall", **WALL)}),
                                         ("query", {"kind": "levels"})),
                 json_turn(DONE))
    frames = report(client, session_id, OK)
    assert ran == ["query"]
    closing = events(frames, "tool")[1::2]
    assert [(t["role"], t["name"], t["status"]) for t in closing] == [
        ("supervisor", "reconcile", "error"), ("supervisor", "query", "ok")]
    assert tokens(frames) == DONE["report"]


def test_the_supervisor_answers_after_its_query_rounds(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", *[tool_turn(("query", {"kind": "levels"}))] * roles.SUPERVISOR_TOOL_ROUNDS,
                 json_turn(DONE))
    report(client, session_id, OK)
    sups = model.requests_of("supervisor")
    assert len(sups) == roles.SUPERVISOR_TOOL_ROUNDS + 1
    assert all(tool_names(b) == ["query"] for b in sups[:-1]) and "tools" not in sups[-1]


def test_role_definitions_are_constant_and_follow_the_table():
    assert roles.ROUTE_TOOLS == {"query": ("get_project_snapshot", "query", "ask_designer"),
                                 "tool": ("get_project_snapshot", "query", "list_tools", "get_tool_choices",
                                          "missing_params", "reconcile", "propose_spec", "ask_designer")}
    assert roles.SUPERVISOR_TOOLS == ("query",)
    assert roles.ROUTE_DEFINITIONS["tool"] == TOOL_DEFINITIONS
    assert all(d in TOOL_DEFINITIONS for d in roles.ROUTE_DEFINITIONS["query"] + roles.SUPERVISOR_DEFINITIONS)


def test_analyst_routed_questions_send_structured_reply_to_planner(client, revit, model):
    from tests.test_questions import FORM
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", tool_turn(("ask_designer", FORM)), text_turn("L2"))
    res = client.post("/api/chat", json={"message": "Which level?"}, headers=HEADERS)
    form, = events(sse(res.text), "questions")
    answers = {"ask_id": form["ask_id"], "items": [{"id": "level", "label": "L2", "value": 2}]}
    res = client.post("/api/chat", json={"session_id": res.headers["X-Session-Id"], "message": "L2",
                                         "answers": answers}, headers=HEADERS)
    assert tokens(sse(res.text)) == "L2"
    assert tool_names(model.requests_of("planner")[-1]) == list(roles.QUERY_TOOLS)
    assert roles.ANSWERS_PREFIX in user_text(model.requests_of("planner")[-1])


def test_typed_change_after_query_form_is_routed_again(client, revit, model):
    from tests.test_questions import FORM
    model.script("analyst", json_turn(ROUTING_QUERY), json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("ask_designer", FORM)), text_turn("A new wall."))
    res = client.post("/api/chat", json={"message": "Which level?"}, headers=HEADERS)
    assert events(sse(res.text), "questions")
    res = client.post("/api/chat", json={"session_id": res.headers["X-Session-Id"],
                                         "message": "Create a wall instead"}, headers=HEADERS)
    assert tokens(sse(res.text)) == "A new wall."
    assert model.roles[-2:] == ["analyst", "planner"]
    assert "propose_spec" in tool_names(model.requests_of("planner")[-1])


@pytest.mark.parametrize("language,label,reply", [("zh-TW,en;q=0.5", "结束此任务", "此任务已结束。"),
                                                  ("zh;q=0.2,en-US", "End this task", "This task has ended.")])
def test_supervisor_stop_closes_task_without_model_call(revit, client, model, language, label, reply):
    sid = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "ask", "report": "Locked.", "question": "What next?"}))
    response = client.post("/api/chat", json={"session_id": sid, "execution": FAILED},
                           headers={**HEADERS, "Accept-Language": language})
    form, = events(sse(response.text), "questions")
    option = form["questions"][0]["options"][1]
    assert option == {"value": "stop", "label": label}
    body = {"message": label, "session_id": sid,
            "answers": {"ask_id": form["ask_id"], "items": [{"id": "next_step", **option}]}}
    calls = len(model.requests)
    assert tokens(sse(client.post("/api/chat", json=body).text)) == reply
    session = get_session_store().get(sid)
    assert session.role_task is session.pending_verdict is session.pending_form is session.spec is None
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 422
    assert len(model.requests) == calls


@pytest.mark.parametrize("language, expected", [("zh-TW,en;q=0.5", "需要调整什么？"),
                                                ("en-US,zh;q=0.5", "What should change?")])
def test_host_corrected_question_uses_browser_language(revit, client, model, language, expected):
    sid = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "done", "report": "All good."}))
    response = client.post("/api/chat", json={"session_id": sid, "execution": VALIDATION_FAILED},
                           headers={**HEADERS, "Accept-Language": language})
    frames = sse(response.text)
    form, = events(frames, "questions")
    assert form["questions"][0]["text"] == expected
    assert expected not in tokens(frames)


def test_other_host_generated_chinese_questions():
    assert roles.fallback_verdict(FAILED, chinese=True).question == "接下来要怎么做？"
    retry = roles.Verdict("retry", "r", fix="L2")
    assert roles.enforce(retry, OK, 0, chinese=True).question == "需要调整吗？监督员建议：L2"
    assert roles.enforce(retry, FAILED, roles.MAX_RETRIES, chinese=True).question == "需要调整什么？上次建议：L2"
    assert roles.enforce(roles.Verdict("done", "r"), FAILED, 0, chinese=True).question == "接下来要怎么做？"


def test_planner_option_named_stop_is_an_ordinary_answer(revit, client, model):
    sid = wall_turn(client, model)
    option = {"label": "Stop at L2", "value": "stop"}
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("ask_designer", {"questions": [{"id": "next_step", "text": "Where?",
                 "options": [option, {"label": "Continue to L3", "value": "next"}]}]})), text_turn("L2."))
    response = client.post("/api/chat", json={"session_id": sid, "message": "Where should the wall stop?"},
                           headers=HEADERS)
    form, = events(sse(response.text), "questions")
    body = {"session_id": sid, "message": option["label"],
            "answers": {"ask_id": form["ask_id"], "items": [{"id": "next_step", **option}]}}
    assert tokens(sse(client.post("/api/chat", json=body, headers=HEADERS).text)) == "L2."
    assert get_session_store().get(sid).role_task is not None


# -- prompts, prefixes, the digest ------------------------------------------------------------

def test_each_role_prompt_is_the_packages_text_and_stays_the_same(revit, client, model, monkeypatch):
    built: list[str] = []
    real = chat_module.build_role_prompt

    def counting(role):
        built.append(role)
        return real(role)

    monkeypatch.setattr(chat_module, "build_role_prompt", counting)
    session_id = wall_turn(client, model)
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Which level?"), text_turn("ok"))
    client.post("/api/chat", json={"message": "a second wall", "session_id": session_id}, headers=HEADERS)
    client.post("/api/chat", json={"message": "L2", "session_id": session_id}, headers=HEADERS)
    assert built == ["analyst", "planner"]
    analysts = model.requests_of("analyst")
    assert len({json.dumps(b["messages"][0]) for b in analysts}) == 1
    assert analysts[0]["messages"][0]["content"] == (chat_module.inline_card_instructions(role="analyst", code=False).rstrip("\n")
                                                     + "\n\n" + roles.ANALYST_WORKBENCH_LINE)
    # The analyst's message opens with the same tools and digest every turn.
    heads = {user_text(b).split("\n\nDesigner's message:")[0].split("\n\nOpen task")[0] for b in analysts}
    assert len(heads) == 1
    planners = model.requests_of("planner")
    assert len({json.dumps(b["messages"][0]) for b in planners}) == 1
    assert planners[0]["messages"][0]["content"] == (chat_module.inline_card_instructions(role="planner", code=False).rstrip("\n")
                                                     + "\n" + chat_module.WORKBENCH_LINE)
    # Append, never rewrite: every planner request starts with the whole previous one.
    for earlier, later in zip(planners, planners[1:]):
        assert later["messages"][:len(earlier["messages"])] == earlier["messages"]
    assert len(planners) == 4


def test_the_digest_is_read_once_and_again_after_an_execution(revit, client, model, monkeypatch):
    taken: list[int] = []
    real = chat_module.snapshot_now

    async def counting(client_, categories=None):
        taken.append(1)
        return await real(client_, categories)

    monkeypatch.setattr(chat_module, "snapshot_now", counting)
    session_id = wall_turn(client, model)
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "ok"}))
    client.post("/api/chat", json={"message": "thanks", "session_id": session_id}, headers=HEADERS)
    assert len(taken) == 1
    model.script("supervisor", json_turn(DONE))
    report(client, session_id, OK)
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "ok"}))
    client.post("/api/chat", json={"message": "thanks again", "session_id": session_id}, headers=HEADERS)
    assert len(taken) == 2
    # A new session (the page's new task) reads its own.
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "ok"}))
    client.post("/api/chat", json={"message": "hello"}, headers=HEADERS)
    assert len(taken) == 3


# -- models, the switch, the limit --------------------------------------------------------------

THREE = """\
models:
  a: { base_url: https://analyst.example/v1, model: analyst-model, api_key_env: W6_FAKE_A_KEY }
  p: { base_url: https://planner.example/v1, model: planner-model, api_key_env: W6_FAKE_P_KEY }
  s: { base_url: https://supervisor.example/v1, model: supervisor-model, api_key_env: W6_FAKE_S_KEY }
roles:
  analyst: a
  planner: p
  supervisor: s
"""


@pytest.fixture
def three_models(roles_env, tmp_path, monkeypatch):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(THREE, encoding="utf-8")
    for role in "APS":
        roles_env.setenv(f"W6_FAKE_{role}_KEY", f"made-up-{role.lower()}-key")
    sent: list[tuple[str, str]] = []
    fake = RoleScriptedModel()

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.headers.get("authorization", "")))
        return fake._handle(request)

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda *a, **k: real_client(*a, **{**k, "transport": transport}))
    from backend.main import create_app
    with TestClient(create_app()) as tc:
        yield tc, fake, sent


def test_each_role_runs_on_its_own_entry_and_the_headers_replace_the_planners_only(revit, three_models):
    client, model, sent = three_models
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})), text_turn("card"))
    resp = client.post("/api/chat", json={"message": "a wall"})
    session_id = resp.headers["X-Session-Id"]
    model.script("supervisor", json_turn(DONE))
    frames = report_free(client, session_id, OK)
    assert sent == [("https://analyst.example/v1/chat/completions", "Bearer made-up-a-key"),
                    ("https://planner.example/v1/chat/completions", "Bearer made-up-p-key"),
                    ("https://planner.example/v1/chat/completions", "Bearer made-up-p-key"),
                    ("https://supervisor.example/v1/chat/completions", "Bearer made-up-s-key")]
    assert [b["model"] for b in model.requests] == ["analyst-model", "planner-model", "planner-model",
                                                    "supervisor-model"]
    stages = events(sse(resp.text), "stage") + events(frames, "stage")
    assert [(s["role"], s["model"]) for s in stages] == [
        ("analyst", "analyst-model"), ("analyst", "analyst-model"), ("planner", "planner-model"),
        ("planner", "planner-model"), ("supervisor", "supervisor-model"), ("supervisor", "supervisor-model")]
    rows = get_log_store().calls_of_session(session_id)
    assert [(r["role"], r["model"]) for r in rows] == [("analyst", "analyst-model"), ("planner", "planner-model"),
                                                        ("planner", "planner-model"), ("supervisor", "supervisor-model")]
    # The browser's model settings replace the planner's entry, and only that one.
    sent.clear()
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", text_turn("L1, L2"))
    resp = client.post("/api/chat", json={"message": "levels", "session_id": session_id},
                       headers={"X-LLM-Base-Url": "https://mine.example/v1", "X-LLM-Model": "my-model",
                                "X-LLM-Key": "made-up-visitor-key"})
    assert sent == [("https://analyst.example/v1/chat/completions", "Bearer made-up-a-key"),
                    ("https://mine.example/v1/chat/completions", "Bearer made-up-visitor-key")]
    assert [(u["role"], u["model"]) for u in events(sse(resp.text), "usage")] == [
        ("analyst", "analyst-model"), ("planner", "my-model")]
    assert "made-up" not in resp.text


def report_free(client, session_id, execution) -> list:
    resp = client.post("/api/chat", json={"execution": execution, "session_id": session_id})
    assert resp.status_code == 200, resp.text
    return sse(resp.text)


def test_without_a_models_file_every_role_uses_the_one_model(revit, client, model):
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn(DONE))
    report(client, session_id, OK)
    assert {b["model"] for b in model.requests} == {"fake-1"}
    assert set(model.roles) == {"analyst", "planner", "supervisor"}


def test_a_role_without_a_model_refuses_the_turn_before_any_call(roles_env, tmp_path, monkeypatch, model):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(THREE, encoding="utf-8")
    roles_env.setenv("W6_FAKE_P_KEY", "made-up-p-key")           # the analyst's variable is not set
    from backend.main import create_app
    with TestClient(create_app()) as client:
        resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 400 and model.requests == []


def test_the_switch_and_the_default(make_client, env, model):
    env.delenv("LLM_SINGLE_MODEL")
    client = make_client()
    assert client.get("/config.json").json()["features"]["roles"] is True
    # The default roles are planner + supervisor: the analyst is ROLES_ANALYST=on (tests/test_chat_modes.py).
    assert client.get("/config.json").json()["features"]["chatModeDefault"] == "roles"
    env.setenv("LLM_SINGLE_MODEL", "1")
    client = make_client()
    assert client.get("/config.json").json()["features"]["roles"] is False
    model.script("single", text_turn("one loop"))
    frames = sse(client.post("/api/chat", json={"message": "hi"}, headers=HEADERS).text)
    assert tokens(frames) == "one loop" and model.roles == ["single"]
    assert {s["role"] for s in events(frames, "stage")} == {"planner"}


def test_without_the_bridge_there_are_no_roles(client, model):
    model.script("single", text_turn("guessing"))
    frames = sse(client.post("/api/chat", json={"message": "a wall", "bridge": False}, headers=HEADERS).text)
    assert tokens(frames) == "guessing" and model.roles == ["single"]
    assert model.requests[0]["messages"][0]["content"] == chat_module.BASE_PROMPT
    assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [("planner", "start"), ("planner", "end")]


def test_the_rate_limit_counts_turns_not_model_calls(make_client, env, model):
    env.delenv("LLM_SINGLE_MODEL")
    env.setenv("ROLES_ANALYST", "on")
    env.setenv("CHAT_RATE_LIMIT", "2")
    client = make_client()
    for _ in range(2):
        model.script("analyst", json_turn(ROUTING_QUERY))
        model.script("planner", tool_turn(("query", {"kind": "levels"})), text_turn("none"))
        assert client.post("/api/chat", json={"message": "levels"}, headers=HEADERS).status_code == 200
    assert len(model.requests) == 6                             # three model calls per turn
    assert client.post("/api/chat", json={"message": "levels"}, headers=HEADERS).status_code == 429


def test_a_model_error_closes_the_open_stage(client, model):
    model.script("analyst", json_turn(ROUTING_QUERY))           # the planner's script is empty: HTTP 500
    frames = sse(client.post("/api/chat", json={"message": "levels"}, headers=HEADERS).text)
    assert names(frames)[-3:] == ["stage", "stage", "error"]
    assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [
        ("analyst", "start"), ("analyst", "end"), ("planner", "start"), ("planner", "end")]
    assert "script of planner exhausted" in events(frames, "error")[0]["detail"]


# -- the designer's picks ------------------------------------------------------------------

def test_picks_are_checked_before_the_analyst_and_reach_both_roles(env, picker, model):
    from revit_bridge.mentions import mention_block

    from tests.test_mentions import L2
    env.delenv("LLM_SINGLE_MODEL")
    env.setenv("ROLES_ANALYST", "on")
    from backend.main import create_app
    with TestClient(create_app()) as client:
        bound = spec_for("create_wall", **{**WALL, "level_name": "L2"})
        for binding in bound["parameters"]:
            if binding["name"] == "level_name":
                binding.update(source="tool", evidence="mention:level#2", unit=None)
        forged = spec_for("create_wall", **{**WALL, "level_name": "L1"})
        forged["parameters"][0].update(source="tool", evidence="mention:level#1", unit=None)
        model.script("analyst", json_turn(ROUTING_TOOL))
        model.script("planner", tool_turn(("propose_spec", {"spec": forged})), tool_turn(("propose_spec", {"spec": bound})),
                     text_turn("card"))
        resp = client.post("/api/chat", json={"message": "a wall on the picked level",
                                              "mentions": [{"kind": "level", "id": 2}]}, headers=HEADERS)
        frames = sse(resp.text)
    assert names(frames)[0] == "mentions" and events(frames, "mentions")[0]["resolved"][0]["id"] == 2
    block = mention_block([L2], "en")
    assert user_text(model.requests_of("analyst")[0]).endswith("a wall on the picked level\n\n" + block)
    assert user_text(model.requests_of("planner")[0]).startswith("a wall on the picked level\n\n" + block + "\n\n")
    verdicts = [json.loads(m["content"]) for m in model.requests_of("planner")[-1]["messages"] if m["role"] == "tool"]
    assert [v["accepted"] for v in verdicts] == [False, True]
    assert verdicts[0]["errors"][0]["code"] == "unknown_mention"


@pytest.fixture
def picker(env):
    from tests.fake_revit import FakeRevit
    from tests.test_mentions import LEVEL_ROWS, picker_handler
    state: dict = {"levels": [dict(row) for row in LEVEL_ROWS], "calls": []}
    with FakeRevit(picker_handler(state)) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        yield fake


# -- the parsers ------------------------------------------------------------------------------

@pytest.mark.parametrize("text, problem", [
    ("", "empty reply"), ("no json", "no JSON object"), ("[1, 2]", "not a JSON object"),
    ('{"route": "maybe"}', "route"), ('{"route": "chat"}', "note is missing"),
    ('{"route": "chat", "note": "  "}', "note is empty"),
    ('{"route": "query", "facts_needed": "level", "note": ""}', "facts_needed"),
    ('{"route": "tool", "tool": "nope", "note": ""}', "tool is not a name"),
    ('{"route": "tool", "tool": 5, "note": ""}', "tool is not text"),
])
def test_routing_parse_refusals(text, problem):
    with pytest.raises(roles.RoleOutputError, match=problem):
        roles.parse_routing(text, ["create_wall"])


def test_routing_parse_accepts_the_shape_and_drops_what_does_not_apply():
    r = roles.parse_routing('{"route": "query", "tool": "create_wall", "note": "read", "extra": 1}', ["create_wall"])
    assert (r.route, r.tool, r.facts_needed, r.note) == ("query", None, [], "read")
    r = roles.parse_routing('{"route": "tool", "tool": "create_wall", "facts_needed": [" level ", ""], "note": null}',
                            ["create_wall"])
    assert r.handoff() == {"route": "tool", "tool": "create_wall", "facts_needed": ["level"], "note": ""}


@pytest.mark.parametrize("text, problem", [
    ('{"verdict": "maybe", "report": "x"}', "verdict"), ('{"verdict": "done"}', "report is missing"),
    ('{"verdict": "retry", "report": "x"}', "fix is missing"), ('{"verdict": "done", "report": 3}', "report is not text"),
])
def test_verdict_parse_refusals(text, problem):
    with pytest.raises(roles.RoleOutputError, match=problem):
        roles.parse_verdict(text)


def test_verdict_parse_keeps_fix_and_question_where_they_belong():
    v = roles.parse_verdict('{"verdict": "done", "report": "ok", "fix": "x", "question": "y"}')
    assert (v.fix, v.question, v.reply()) == (None, None, "ok")
    v = roles.parse_verdict('{"verdict": "ask", "report": "r", "question": "q?"}')
    assert v.reply() == "r\n\nq?"
    v = roles.parse_verdict('{"verdict": "ask", "report": "r"}')
    assert v.reply() == "r"


def test_a_failed_validator_keeps_its_own_words():
    execution = {"success": False, "error": "validation_failed",
                 "validation": {"validator": "v", "passed": False,
                                "checks": [{"name": "ratio", "passed": False, "detail": "expected 1: "},
                                           {"name": "bare", "passed": False, "detail": ""}]}}
    forced = roles.enforce(roles.Verdict("done", "ok"), execution, 0)
    assert forced.report == ("The validator did not pass, so this is not done:\n"
                             "- ratio (failed): expected 1: \n- bare (failed)")


def test_the_execution_summary_cuts_the_result_and_drops_the_samples():
    big = {**OK, "result": {"ids": list(range(2000))}, "unknown_field": "x" * 50}
    summary = roles.execution_summary(big)
    assert isinstance(summary["result"], str) and summary["result"].endswith(" characters)")
    assert len(summary["result"]) < roles.RESULT_KEEP + 50
    assert "unknown_field" not in summary and "before" not in summary["validation"]
    assert roles.execution_summary({"success": True}) == {"success": True}


# -- round 2: caps, the task's life, the switches, the log, the digest -------------------------

def supervisor_request_bytes(model) -> int:
    return len(json.dumps(model.requests_of("supervisor")[-1], ensure_ascii=False))


HUGE = {
    "error": {"success": False, "tool": "create_wall", "error": "e" * 4_000_000, "evidence_id": "ev_h",
              "warnings": ["w" * 5000] * 20},
    "checks": {**VALIDATION_FAILED, "validation": {
        "validator": "element_count", "passed": False,
        "checks": [{"name": f"check-{i}", "passed": i % 3 != 0, "detail": f"detail {i}"} for i in range(10_000)]}},
    "detail": {**VALIDATION_FAILED, "validation": {
        "validator": "element_count", "passed": False,
        "checks": [{"name": "count", "passed": False, "detail": "d" * 1_000_000}]},
        "result": {"ids": list(range(200_000))}},
}


@pytest.mark.parametrize("name", list(HUGE))
def test_a_huge_result_reaches_no_model_report_or_session_whole(revit, client, model, name):
    execution = HUGE[name]
    assert len(json.dumps(execution)) <= chat_module.MAX_EXECUTION_CHARS          # the route takes it
    session_id = wall_turn(client, model)
    model.script("supervisor", json_turn({"verdict": "done", "report": "All good."}))
    frames = report(client, session_id, execution)
    message = user_text(model.requests_of("supervisor")[-1])
    assert len(message) <= roles.MESSAGE_KEEP == chat_module.EXECUTION_KEEP
    assert supervisor_request_bytes(model) < 30_000
    reply = tokens(frames)
    assert len(reply) < 12_000 and "All good" not in reply                       # the rules still fire
    assert reply.startswith("The validator did not pass" if name != "error" else "Revit did not report success")
    session = get_session_store().get(session_id)
    assert len(json.dumps(session.pending_verdict)) < 3 * roles.TEXT_MAX
    assert len(json.dumps(session.history)) < 40_000                             # nothing of it went in
    if name == "checks":
        sent = json.loads(message.split("\n", 1)[1])["execution"]["validation"]
        assert len(sent["checks"]) == roles.CHECKS_MAX and sent["checks_more"] == 10_000 - roles.CHECKS_MAX
        assert all(c["passed"] is False for c in sent["checks"])                   # the failed ones first
        assert reply.count("\n- ") == roles.CHECKS_SHOWN and "more checks)" in reply
    # A huge report of the supervisor's own is cut before it goes back to the planner.
    model.script("supervisor", json_turn({"verdict": "retry", "report": "r" * 100_000, "fix": "f" * 100_000}))
    model.script("planner", text_turn("again"))
    report(client, session_id, FAILED)
    retry = user_text(model.requests_of("planner")[-1])
    assert len(retry) < 3 * roles.TEXT_MAX


def test_a_spec_too_large_for_the_message_goes_as_its_head():
    spec = {"task": "t" * 50_000, "parameters": []}
    message = roles.supervisor_message(spec, OK, 0)
    assert len(message) <= roles.MESSAGE_KEEP
    payload = json.loads(message.split("\n", 1)[1])
    assert isinstance(payload["confirmed_spec"], str) and payload["confirmed_spec"].endswith(" characters)")
    assert payload["execution"]["evidence_id"] == "ev_3"


def test_a_query_opens_no_task_and_another_tool_opens_a_new_one(revit, client, model):
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", text_turn("L1 and L2."))
    resp = client.post("/api/chat", json={"message": "levels?"}, headers=HEADERS)
    session_id = resp.headers["X-Session-Id"]
    session = get_session_store().get(session_id)
    assert session.role_task is None

    def say(routing, reply, message="next"):
        model.script("analyst", json_turn(routing))
        model.script("planner", text_turn(reply))
        client.post("/api/chat", json={"message": message, "session_id": session_id}, headers=HEADERS)

    say(ROUTING_TOOL, "Which level?")
    task = session.role_task
    assert (task.tool, task.last, task.retries) == ("create_wall", "Which level?", 0)
    task.retries = 1
    say(ROUTING_QUERY, "L1 and L2.")                       # a read in between: the task stays as it was
    assert session.role_task is task and task.last == "Which level?" and task.retries == 1
    say({**ROUTING_TOOL, "tool": "create_floor"}, "Which floor type?")
    assert session.role_task is not task
    assert (session.role_task.tool, session.role_task.retries) == ("create_floor", 0)


def test_the_planner_tools_switch_gives_it_all_seven(make_client, env, revit, model):
    env.delenv("LLM_SINGLE_MODEL")
    env.setenv("ROLES_PLANNER_TOOLS", "all")
    client = make_client()
    model.script("analyst", json_turn(ROUTING_QUERY))
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})), text_turn("ok"))
    frames = sse(client.post("/api/chat", json={"message": "levels?"}, headers=HEADERS).text)
    planner = model.requests_of("planner")[0]
    assert planner["tools"] == TOOL_DEFINITIONS                # every route
    assert len(events(frames, "spec")) == 1                    # and every one runs
    assert events(frames, "tool")[-1]["status"] == "ok"


def test_the_analyst_switch_sends_every_message_to_the_planner(make_client, env, revit, model):
    env.delenv("LLM_SINGLE_MODEL")
    env.setenv("ROLES_ANALYST", "off")
    client = make_client()
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})), text_turn("card"))
    resp = client.post("/api/chat", json={"message": "a wall"}, headers=HEADERS)
    frames = sse(resp.text)
    assert model.roles == ["planner", "planner"]
    assert [(s["role"], s["status"]) for s in events(frames, "stage")] == [("planner", "start"), ("planner", "end")]
    assert model.requests[0]["tools"] == TOOL_DEFINITIONS
    assert user_text(model.requests[0]).endswith(
        roles.ROUTING_PREFIX + ' {"route": "tool", "facts_needed": [], "note": ""}')
    model.script("supervisor", json_turn(DONE))
    report(client, resp.headers["X-Session-Id"], OK)
    assert model.roles[-1] == "supervisor"                     # the supervisor still reports


@pytest.mark.parametrize("variable, value", [("ROLES_PLANNER_TOOLS", "some"), ("ROLES_ANALYST", "1")])
def test_a_switch_with_another_value_refuses_the_start(make_client, env, variable, value):
    from backend.config import ConfigError
    env.setenv(variable, value)
    with pytest.raises(ConfigError, match=variable):
        make_client()


def test_a_missing_model_names_the_role(roles_env, tmp_path, monkeypatch, model):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(THREE, encoding="utf-8")
    roles_env.setenv("W6_FAKE_P_KEY", "made-up-p-key")           # the analyst's and supervisor's are not set
    from backend.main import create_app
    with TestClient(create_app()) as client:
        detail = client.post("/api/chat", json={"message": "hi"}).json()["detail"]
        assert "for the analyst" in detail and "api_key_env" in detail and "Connect page" not in detail
        roles_env.delenv("W6_FAKE_P_KEY")
        detail = client.post("/api/chat", json={"message": "hi"}).json()["detail"]
        assert detail == chat_module.NO_MODEL_FROM_FILE             # the planner's: the browser can fill it in
    assert model.requests == []


def test_the_interaction_log_names_each_role_that_ran(revit, client, model):
    wall_turn(client, model)
    rows = get_log_store().query()["items"]
    assert rows[0]["model"] == "analyst fake-1, planner fake-1"
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "hi"}))
    client.post("/api/chat", json={"message": "hello"}, headers=HEADERS)
    assert get_log_store().query()["items"][0]["model"] == "analyst fake-1"


def test_the_digest_is_kept_per_device_and_a_failed_read_waits(monkeypatch):
    import asyncio

    from backend.session import Session

    titles = iter(["Model A", "Model B"])
    reads: list[int] = []

    async def snapshot(client_, categories=None):
        reads.append(1)
        return {"document": {"title": next(titles)}, "levels": [], "fingerprint": "f"}

    async def no_client():
        return None

    monkeypatch.setattr(chat_module, "get_revit_client", no_client)
    monkeypatch.setattr(chat_module, "snapshot_now", snapshot)
    session = Session(session_id="s")
    a = asyncio.run(chat_module.model_digest(session, "device-a"))
    b = asyncio.run(chat_module.model_digest(session, "device-b"))
    assert a.startswith("Document: Model A") and b.startswith("Document: Model B") and len(reads) == 2
    assert asyncio.run(chat_module.model_digest(session, "device-a")) == a and len(reads) == 2

    async def unreachable(client_, categories=None):
        reads.append(1)
        raise OSError("refused")

    monkeypatch.setattr(chat_module, "snapshot_now", unreachable)
    assert asyncio.run(chat_module.model_digest(session, "device-c")) == "not available (revit_unreachable)"
    assert asyncio.run(chat_module.model_digest(session, "device-c")) == "not available (revit_unreachable)"
    assert len(reads) == 3                                      # not tried again within the minute
    monkeypatch.setattr(roles, "DIGEST_RETRY_SECONDS", 0.0)
    asyncio.run(chat_module.model_digest(session, "device-c"))
    assert len(reads) == 4


# -- the old suite's key cases, in every mode ---------------------------------------------------

@pytest.fixture(params=["single", "roles_analyst", "roles"])
def mode(request, env):
    """The chat's modes by their switches: the one loop, the roles with the analyst
    (ROLES_ANALYST=on) and the default roles, planner + supervisor (ROLES_ANALYST unset)."""
    if request.param != "single":
        env.delenv("LLM_SINGLE_MODEL")
    if request.param == "roles_analyst":
        env.setenv("ROLES_ANALYST", "on")
    return request.param


@pytest.fixture
def mode_client(mode, make_client):
    return make_client(raise_server_exceptions=False)


def planner_script(model, mode, *turns) -> str:
    """The turns of the one loop, or of the planner (after an analyst that routes to tool in mode
    roles_analyst). The role whose requests to read."""
    if mode == "single":
        model.script("single", *turns)
        return "single"
    if mode == "roles_analyst":
        model.script("analyst", json_turn({"route": "tool", "facts_needed": [], "note": ""}))
    model.script("planner", *turns)
    return "planner"


def test_both_modes_a_tool_that_raises_closes_its_row_and_the_stage(mode, mode_client, model, monkeypatch):
    import contextlib

    from tests.test_trace_usage import _recording

    async def broken(name, arguments, session):
        raise RuntimeError("boom at C:\\secret")

    monkeypatch.setattr(chat_module, "call_tool", broken)
    chunks = _recording(monkeypatch)
    role = planner_script(model, mode, tool_turn(("list_tools", {})), text_turn("never"))
    with contextlib.suppress(Exception):
        mode_client.post("/api/chat", json={"message": "x"}, headers=HEADERS)
    frames = sse("".join(chunks))
    tools = events(frames, "tool")
    first = "1.2.1" if mode == "roles_analyst" else "1.1.1"      # the analyst made call 1
    assert [(t["id"], t["role"], t["status"]) for t in tools] == [(first, "planner", "running"),
                                                                    (first, "planner", "error")]
    assert tools[1]["error"] == "RuntimeError" and "secret" not in "".join(chunks)
    assert events(frames, "stage")[-1] == {"role": "planner", "model": "fake-1", "status": "end"}
    assert len(model.requests_of(role)) == 1


def test_both_modes_keep_the_head_of_a_large_tool_result(mode, mode_client, model, monkeypatch):
    from backend.api.model_tools import ToolOutcome
    big = {"items": [{"id": i, "name": f"element-{i:05d}"} for i in range(1500)]}

    async def fake_call_tool(name, arguments, session):
        return ToolOutcome(big)

    monkeypatch.setattr(chat_module, "call_tool", fake_call_tool)
    role = planner_script(model, mode, tool_turn(("query", {"kind": "levels"})), text_turn("many"))
    resp = mode_client.post("/api/chat", json={"message": "list"}, headers=HEADERS)
    full = json.dumps(big, ensure_ascii=False)
    sent = [m for m in model.requests_of(role)[1]["messages"] if m.get("role") == "tool"]
    assert sent[0]["content"] == full
    stored = [m for m in get_session_store().get(resp.headers["X-Session-Id"]).history if m.get("role") == "tool"][0]
    assert stored["content"].startswith(full[:chat_module.TOOL_RESULT_KEEP]) and len(stored["content"]) < len(full)


def test_both_modes_send_a_window_of_40_messages(mode, mode_client, model):
    planner_script(model, mode, text_turn("first"))
    resp = mode_client.post("/api/chat", json={"message": "start"}, headers=HEADERS)
    session = get_session_store().get(resp.headers["X-Session-Id"])
    for i in range(30):
        session.add({"role": "user", "content": f"u{i}"})
        session.add({"role": "assistant", "content": f"a{i}"})
    role = planner_script(model, mode, text_turn("ok"))
    mode_client.post("/api/chat", json={"message": "last", "session_id": session.session_id}, headers=HEADERS)
    messages = model.requests_of(role)[-1]["messages"]
    assert messages[0]["role"] == "system" and len(messages) - 1 <= chat_module.HISTORY_WINDOW
    assert messages[1]["role"] == "user" and messages[-1]["content"].startswith("last")


def test_both_modes_refuse_a_pick_that_was_not_made(mode, make_client, picker, model):
    client = make_client()
    forged = spec_for("create_wall", **{**WALL, "level_name": "L1"})
    forged["parameters"][0].update(source="tool", evidence="mention:level#1", unit=None)
    role = planner_script(model, mode, tool_turn(("propose_spec", {"spec": forged})), text_turn("card"))
    frames = sse(client.post("/api/chat", json={"message": "a wall"}, headers=HEADERS).text)
    verdict = [json.loads(m["content"]) for m in model.requests_of(role)[-1]["messages"] if m["role"] == "tool"][0]
    assert verdict["accepted"] is False and verdict["errors"][0]["code"] == "unknown_mention"
    assert events(frames, "spec")[0]["errors"][0]["code"] == "unknown_mention"
