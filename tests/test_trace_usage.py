"""W2: what the host loop did, made visible (stage / tool / usage events), each model call
recorded in ``llm_calls`` with the request's run label, and the prompt prefix kept stable
within a session."""
from __future__ import annotations

import contextlib
import json
import sqlite3

import httpx
import pytest

from backend import llm as llm_module
from backend.api import chat as chat_module
from backend.api.model_tools import TOOL_DEFINITIONS, ToolOutcome
from backend.log_store import InteractionLogStore, get_log_store
from backend.session import get_session_store
from tests.fake_model import USAGE_SHAPES, drop_trace_frames, text_turn, tool_turn
from tests.test_api_v1 import WALL, spec_for
from tests.test_host_loop import HEADERS, events, model, sse, tokens  # noqa: F401 - the model fixture

FAKE_KEY = "fake-key-must-not-leak"
KEYED = {**HEADERS, "X-LLM-Key": FAKE_KEY}


# -- the events -------------------------------------------------------------------------

def test_a_turn_streams_stage_tool_and_usage_events_in_order(revit, client, model):
    """One scripted turn: a snapshot, a spec, a reply. The trace pairs every tool call,
    numbers the rounds, and leaves the old events and their order as they were."""
    model.turns += [
        tool_turn(("get_project_snapshot", {"categories": ["OST_Walls"]}),
                  ("query", {"kind": "levels"}), content="Reading the model. ",
                  usage=USAGE_SHAPES["openai"][0]),
        tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)}), usage=USAGE_SHAPES["deepseek"][0]),
        text_turn("The card is below.", usage=USAGE_SHAPES["gemini"][0]),
    ]
    resp = client.post("/api/chat", json={"message": "a wall on L1"}, headers=KEYED)
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)
    names = [e or "token" for e, _ in frames]
    assert names == [
        "stage",
        "token", "token", "token", "usage",
        "tool", "tool", "tool", "tool",
        "usage", "tool", "tool", "spec",
        "token", "token", "token", "usage",
        "stage", "done",
    ]
    assert tokens(frames) == "Reading the model. The card is below."
    stages = events(frames, "stage")
    # The turn's first stage event names the chat mode it runs in (the suite's: single).
    assert stages == [{"role": "planner", "model": "fake-1", "status": "start", "mode": "single"},
                      {"role": "planner", "model": "fake-1", "status": "end"}]

    usage = events(frames, "usage")
    assert [u["round"] for u in usage] == [1, 2, 3]
    assert {u["role"] for u in usage} == {"planner"} and {u["model"] for u in usage} == {"fake-1"}
    assert [(u["input"], u["output"], u["cache_read"], u["cache_write"]) for u in usage] == [
        USAGE_SHAPES["openai"][1], USAGE_SHAPES["deepseek"][1], USAGE_SHAPES["gemini"][1]]
    assert all(isinstance(u["ms"], int) and u["ms"] >= 0 for u in usage)
    assert list(usage[0]) == ["role", "model", "round", "input", "output", "cache_read", "cache_write", "ms"]

    tools = events(frames, "tool")
    assert [(t["id"], t["status"]) for t in tools] == [
        ("1.1.1", "running"), ("1.1.1", "ok"), ("1.1.2", "running"), ("1.1.2", "ok"),
        ("1.2.1", "running"), ("1.2.1", "ok")]
    assert {t["role"] for t in tools} == {"planner"}
    assert [t["name"] for t in tools[::2]] == ["get_project_snapshot", "query", "propose_spec"]
    assert tools[0] == {"id": "1.1.1", "role": "planner", "name": "get_project_snapshot",
                        "args": {"categories": ["OST_Walls"]}, "status": "running"}
    # The closing event: duration, the result's length in characters and its head.
    sent = [m["content"] for m in model.requests[1]["messages"] if m.get("role") == "tool"]
    for closing, content in zip(tools[1::2][:2], sent):
        assert closing["size"] == len(content) and closing["preview"] == content[:chat_module.TOOL_PREVIEW]
        assert isinstance(closing["ms"], int) and "error" not in closing
    assert len(tools[1]["preview"]) == min(len(sent[0]), 2000)
    assert tools[4]["args"] == {"spec": spec_for("create_wall", **WALL)}

    # One row per model call, the same numbers.
    session_id = resp.headers["X-Session-Id"]
    rows = get_log_store().calls_of_session(session_id)
    assert [(r["turn"], r["round"], r["role"], r["model"]) for r in rows] == [
        (1, 1, "planner", "fake-1"), (1, 2, "planner", "fake-1"), (1, 3, "planner", "fake-1")]
    assert [(r["input"], r["output"], r["cache_read"], r["cache_write"]) for r in rows] == [
        (u["input"], u["output"], u["cache_read"], u["cache_write"]) for u in usage]
    assert [r["ms"] for r in rows] == [u["ms"] for u in usage]
    # Never a key, a header or message text.
    dump = json.dumps(rows) + resp.text
    assert FAKE_KEY not in dump and "model.example" not in dump and "Bearer" not in dump
    assert "a wall on L1" not in json.dumps(rows)

    # The next turn numbers its tool events on: ids stay unique in the session.
    model.turns += [tool_turn(("list_tools", {})), text_turn("ok")]
    resp = client.post("/api/chat", json={"message": "and?", "session_id": session_id}, headers=HEADERS)
    assert [t["id"] for t in events(sse(resp.text), "tool")] == ["2.1.1", "2.1.1"]
    assert [(r["turn"], r["round"]) for r in get_log_store().calls_of_session(session_id)][-2:] == [(2, 1), (2, 2)]


def test_failed_tools_close_their_row_as_errors(client, model):
    """No Revit here: the results carry the MCP error codes, so the rows are errors; bad
    arguments show what the model sent."""
    model.turns += [
        tool_turn(("query", {"kind": "levels"}), ("no_such_tool", {})),
        {"content": "", "tool_calls": [{"name": "list_tools", "raw": "not json"}]},
        text_turn("Revit is not reachable."),
    ]
    resp = client.post("/api/chat", json={"message": "levels?"}, headers=HEADERS)
    tools = events(sse(resp.text), "tool")
    closing = tools[1::2]
    assert [(t["name"], t["status"]) for t in closing] == [
        ("query", "error"), ("no_such_tool", "error"), ("list_tools", "error")]
    assert closing[0]["error"].startswith("revit_unreachable")
    assert closing[1]["error"] == "unknown_tool"
    assert closing[2]["error"] == "invalid_args: arguments must be a JSON object"
    assert closing[2]["args"] == {"_raw": "not json"} and tools[4]["args"] == {"_raw": "not json"}
    assert all("size" in t and "preview" in t and "ms" in t for t in closing)


def _recording(monkeypatch) -> list[str]:
    """The test client drops the body of a stream that raised: record the frames on the way out."""
    chunks: list[str] = []
    real_log_and_stream = chat_module.log_and_stream

    def recording(generator, **kwargs):
        async def frames():
            async for chunk in real_log_and_stream(generator, **kwargs):
                chunks.append(chunk)
                yield chunk
        return frames()

    monkeypatch.setattr(chat_module, "log_and_stream", recording)
    return chunks


def test_a_handler_that_raises_still_closes_its_row(make_client, env, model, monkeypatch, caplog):
    secret_path = "C:\\secret\\path"

    async def broken(name, arguments, session):
        raise RuntimeError(f"boom at {secret_path}")

    monkeypatch.setattr(chat_module, "call_tool", broken)
    chunks = _recording(monkeypatch)
    model.turns += [tool_turn(("list_tools", {})), text_turn("never")]
    client = make_client(raise_server_exceptions=False)
    with contextlib.suppress(Exception):
        client.post("/api/chat", json={"message": "x"}, headers=HEADERS)
    sent = "".join(chunks)
    frames = sse(sent)
    tools = events(frames, "tool")
    assert [(t["id"], t["status"]) for t in tools] == [("1.1.1", "running"), ("1.1.1", "error")]
    # Only the type reaches the browser; the text, with its path, is in the server log.
    assert tools[1]["error"] == "RuntimeError" and "ms" in tools[1]
    assert "secret" not in sent and "boom" not in sent
    assert "tool list_tools raised" in caplog.text and "boom at" in caplog.text
    # The stage is closed before the turn breaks off.
    assert [e for e, _ in frames if e in ("stage", "tool")] == ["stage", "tool", "tool", "stage"]
    assert events(frames, "stage")[-1]["status"] == "end"
    assert len(model.requests) == 1               # the turn ended there, as before
    assert get_log_store().query()["items"][0]["status"].startswith("error: boom at")


def test_any_other_failure_of_the_turn_closes_the_stage(make_client, env, model, monkeypatch):
    """A failure outside the tools (here: storing the session's reply) still sends stage end."""
    def broken_kept(message, keep=chat_module.TOOL_RESULT_KEEP):
        raise ValueError("store failed")

    monkeypatch.setattr(chat_module, "kept_tool_message", broken_kept)
    chunks = _recording(monkeypatch)
    model.turns += [tool_turn(("list_tools", {})), text_turn("never")]
    client = make_client(raise_server_exceptions=False)
    with contextlib.suppress(Exception):
        client.post("/api/chat", json={"message": "x"}, headers=HEADERS)
    frames = sse("".join(chunks))
    assert [s["status"] for s in events(frames, "stage")] == ["start", "end"]
    assert [t["status"] for t in events(frames, "tool")] == ["running", "ok"]
    assert ("done", None) not in frames


def test_large_arguments_go_out_truncated_in_both_events(client, model):
    big = {"spec": {"task": "x" * 9000}}
    text = json.dumps(big, ensure_ascii=False)
    small = {"spec": {"task": "y" * 100}}
    model.turns += [tool_turn(("reconcile", big), ("reconcile", small)), text_turn("ok")]
    resp = client.post("/api/chat", json={"message": "check"}, headers=HEADERS)
    tools = events(sse(resp.text), "tool")
    marker = {"_truncated": True, "_chars": len(text), "_head": text[:chat_module.TOOL_ARGS_HEAD]}
    assert [t["args"] for t in tools[:2]] == [marker, marker]
    assert [t["args"] for t in tools[2:]] == [small, small]           # short ones as given
    assert len(resp.text) < 2 * len(text)                             # the 9 KB went out not even once whole
    # The model still got its own arguments back in the history, whole.
    sent = model.requests[1]["messages"][2]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(sent) == big


def test_shown_arguments_measures_the_json_text():
    limit = chat_module.TOOL_ARGS_LIMIT
    exactly = {"k": "a" * (limit - len('{"k": ""}'))}
    assert len(json.dumps(exactly)) == limit and chat_module.shown_arguments("", exactly) == exactly
    over = {"k": "a" * (limit - len('{"k": ""}') + 1)}
    assert chat_module.shown_arguments("", over)["_chars"] == limit + 1
    assert chat_module.shown_arguments("not json", None) == {"_raw": "not json"}
    raw = "x" * 9000
    shown = chat_module.shown_arguments(raw, None)
    assert shown["_truncated"] is True and shown["_head"].startswith('{"_raw": "xxx')


def test_a_turn_without_the_bridge_reports_usage_but_no_tools(client, model):
    model.turns.append(tool_turn(("list_tools", {}), content="I would guess.", usage=USAGE_SHAPES["deepseek"][0]))
    resp = client.post("/api/chat", json={"message": "a wall", "bridge": False}, headers=HEADERS)
    frames = sse(resp.text)
    assert events(frames, "tool") == []
    assert [u["round"] for u in events(frames, "usage")] == [1]
    assert events(frames, "usage")[0]["role"] == "planner"
    assert [s["status"] for s in events(frames, "stage")] == ["start", "end"]


def test_an_execution_report_keeps_its_echo_first(client, model):
    model.turns.append(text_turn("Reported."))
    first = client.post("/api/chat", json={"message": "hi"}, headers=HEADERS)
    model.turns.append(text_turn("Done."))
    run = {"success": True, "error": None, "evidence_id": "ev_1"}
    resp = client.post("/api/chat", json={"execution": run, "session_id": first.headers["X-Session-Id"]},
                       headers=HEADERS)
    names = [e or "token" for e, _ in sse(resp.text)]
    assert names[:2] == ["execution", "stage"] and names[-2:] == ["stage", "done"]


def test_a_stream_that_breaks_after_the_endpoint_accepted_still_counts_the_call(client, monkeypatch):
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
            raise httpx.ReadError("connection dropped")

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Broken()))
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": transport}))
    resp = client.post("/api/chat", json={"message": "x"}, headers=HEADERS)
    frames = sse(resp.text)
    assert [e or "token" for e, _ in frames] == ["stage", "token", "usage", "stage", "error"]
    assert events(frames, "usage")[0] == {**events(frames, "usage")[0], "input": None, "output": None,
                                          "cache_read": None, "cache_write": None, "round": 1}
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert len(rows) == 1 and rows[0]["input"] is None


# -- the known-good stream ------------------------------------------------------------------

def test_without_usage_the_stream_is_the_old_one_plus_the_trace(revit, client, model):
    """The scripted turn of test_host_loop with a model that reports no usage: drop the
    three new events and the stream is what the host sent before; the request bodies
    differ only by the usage option."""
    model.turns += [
        tool_turn(("get_project_snapshot", {"categories": ["OST_Walls"]})),
        tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})),
        text_turn("The spec card is in the panel; confirm it there."),
    ]
    resp = client.post("/api/chat", json={"message": "a wall on L1"}, headers=HEADERS)
    old = drop_trace_frames(resp.text)
    assert "event: stage" not in old and "event: usage" not in old and "event: tool" not in old
    assert [e or "token" for e, _ in sse(old)] == ["spec", "token", "token", "token", "done"]
    assert all(u["input"] is None and u["cache_read"] is None for u in events(sse(resp.text), "usage"))
    for body in model.requests:
        assert body["stream_options"] == {"include_usage": True}
        assert set(body) == {"model", "messages", "temperature", "max_tokens", "stream", "tools", "tool_choice",
                             "stream_options"}


# -- the run label ----------------------------------------------------------------------

@pytest.mark.parametrize("label", ["", ".", "..", "a b", "x" * 65, "run/1", "r\u00e9sum\u00e9", "a;b", "run\t1"])
def test_a_bad_run_label_is_refused_before_the_model_is_called(client, model, label):
    # Sent as UTF-8 bytes: the server reads header bytes as Latin-1, so no non-ASCII label passes.
    resp = client.post("/api/chat", json={"message": "x"}, headers={**HEADERS, "X-Run-Label": label.encode("utf-8")})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_args"
    assert model.requests == []


@pytest.mark.parametrize("label", ["a", "base-2026.11.23_r1", "x" * 64, "...", "-"])
def test_a_good_run_label_is_stored_with_every_call(client, model, label):
    model.turns += [tool_turn(("list_tools", {})), text_turn("ok")]
    resp = client.post("/api/chat", json={"message": "x"}, headers={**HEADERS, "X-Run-Label": label})
    assert resp.status_code == 200
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert [r["run_label"] for r in rows] == [label, label]


# -- the table on an old database -----------------------------------------------------------

# The schema origin/main (0f83334) creates, word for word: a data volume written by it.
OLD_SCHEMA = """
    CREATE TABLE IF NOT EXISTS interaction_logs (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp        TEXT NOT NULL,
        module           TEXT NOT NULL,
        session_id       TEXT,
        client_ip        TEXT,
        user_agent       TEXT,
        user_input       TEXT,
        assistant_output TEXT,
        model            TEXT,
        duration_ms      INTEGER,
        status           TEXT DEFAULT 'ok'
    );
    CREATE INDEX IF NOT EXISTS idx_logs_ts ON interaction_logs(timestamp);
    CREATE INDEX IF NOT EXISTS idx_logs_module ON interaction_logs(module);
    CREATE INDEX IF NOT EXISTS idx_logs_ip ON interaction_logs(client_ip);
"""


def test_the_calls_table_is_added_to_an_existing_database(tmp_path):
    path = tmp_path / "logs.db"
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute("INSERT INTO interaction_logs (timestamp, module, user_input, assistant_output, model) "
                     "VALUES ('2099-01-01 00:00:00', 'chat', 'old question', 'old answer', 'm')")
    store = InteractionLogStore(path)
    assert store.query()["items"][0]["user_input"] == "old question"           # untouched
    store.record_call(session_id="s", turn=1, round=1, role="planner", model="m",
                      usage={"input": 5, "output": None, "cache_read": None, "cache_write": None}, ms=3)
    assert store.calls_of_session("s")[0]["input"] == 5 and store.calls_of_session("s")[0]["output"] is None
    InteractionLogStore(path)                                                   # a second start changes nothing
    assert len(store.calls_of_session("s")) == 1


def test_old_calls_age_out_with_the_logs(tmp_path):
    store = InteractionLogStore(tmp_path / "logs.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "logs.db")) as conn, conn:
        conn.execute("INSERT INTO llm_calls (created_at, session_id, turn, round, role, ms) "
                     "VALUES ('2000-01-01 00:00:00', 'old', 1, 1, 'planner', 1)")
    store.record_call(session_id="new", turn=1, round=1, role="planner", model="m", usage={}, ms=1)
    InteractionLogStore(tmp_path / "logs.db")                                   # startup purge
    assert store.calls_of_session("old") == [] and len(store.calls_of_session("new")) == 1


# -- the prompt prefix -------------------------------------------------------------------

def test_two_turns_of_a_session_send_the_same_system_message_and_tools(make_client, env, model, tmp_path):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    model.turns += [tool_turn(("list_tools", {})), text_turn("one"), text_turn("two"), text_turn("three")]
    first = client.post("/api/chat", json={"message": "first"}, headers=HEADERS)
    session_id = first.headers["X-Session-Id"]
    # A skill created between the turns: skills are no part of the prompt, in this session or a new one.
    created = client.post("/api/skills", json={"name": "late rule", "content": "# Late rule\n\nAlways say hi."},
                          headers={"X-Admin-Token": "hunter2"})
    assert created.status_code in (200, 201), created.text
    client.post("/api/chat", json={"message": "second", "session_id": session_id}, headers=HEADERS)
    bodies = model.requests[:3]
    assert len(bodies) == 3
    systems = [json.dumps(b["messages"][0], ensure_ascii=False) for b in bodies]
    tools = [json.dumps(b["tools"], ensure_ascii=False) for b in bodies]
    assert len(set(systems)) == 1 and len(set(tools)) == 1
    assert bodies[0]["messages"][0]["role"] == "system" and "Always say hi." not in systems[0]
    assert tools[0] == json.dumps(TOOL_DEFINITIONS, ensure_ascii=False)
    # The volatile part comes after them: the user turns.
    assert [m["role"] for m in bodies[2]["messages"]][:2] == ["system", "user"]
    client.post("/api/chat", json={"message": "fresh"}, headers=HEADERS)
    assert model.requests[3]["messages"][0] == bodies[0]["messages"][0]
    assert get_session_store().get(session_id).system_prompts.keys() == {True}


def test_system_prompt_is_built_once_per_session_and_mode(client, model, monkeypatch):
    built: list[bool] = []
    real = chat_module.build_system_prompt

    def counting(bridge: bool = True) -> str:
        built.append(bridge)
        return real(bridge)

    monkeypatch.setattr(chat_module, "build_system_prompt", counting)
    model.turns += [text_turn("a"), text_turn("b"), text_turn("c")]
    sid = client.post("/api/chat", json={"message": "1"}, headers=HEADERS).headers["X-Session-Id"]
    client.post("/api/chat", json={"message": "2", "session_id": sid}, headers=HEADERS)
    client.post("/api/chat", json={"message": "3", "session_id": sid, "bridge": False}, headers=HEADERS)
    assert built == [True, False]
    assert model.requests[2]["messages"][0]["content"] == chat_module.BASE_PROMPT


def test_tool_outcome_error_text():
    assert chat_module.tool_error_text({"error": "revit_unreachable", "message": "refused"}) == "revit_unreachable: refused"
    assert chat_module.tool_error_text({"error": None, "items": []}) is None
    assert chat_module.tool_error_text({"accepted": False, "errors": [{"code": "x"}]}) is None
    assert chat_module.tool_error_text([1, 2]) is None
    assert chat_module.tool_error_text(ToolOutcome({}).result) is None
    assert chat_module.tool_error_text({"success": False, "error": "boom" * 500}) == ("boom" * 500)[:500]
