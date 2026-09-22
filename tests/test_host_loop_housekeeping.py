"""What the host loop keeps between turns: capped tool results, only parsable specs."""
from __future__ import annotations

import json

from backend.api import chat as chat_module
from backend.api.model_tools import ToolOutcome
from backend.session import get_session_store
from tests.fake_model import text_turn, tool_turn
from tests.test_host_loop import HEADERS, model, sse, tokens  # noqa: F401 - the model fixture


def test_large_tool_results_are_capped_in_the_session_after_the_turn(client, model, monkeypatch):
    big = {"items": [{"id": i, "name": f"element-{i:05d}"} for i in range(1500)]}   # well over 8 KB

    async def fake_call_tool(name, arguments, session):
        return ToolOutcome(big)

    monkeypatch.setattr(chat_module, "call_tool", fake_call_tool)
    model.turns += [tool_turn(("query", {"kind": "elements", "args": {"category": "OST_Walls"}})), text_turn("many")]
    resp = client.post("/api/chat", json={"message": "list the walls"}, headers=HEADERS)
    assert resp.status_code == 200 and tokens(sse(resp.text)) == "many"

    # The turn that produced the result sent it to the model in full...
    full = json.dumps(big, ensure_ascii=False)
    sent = [m for m in model.requests[1]["messages"] if m.get("role") == "tool"]
    assert len(sent) == 1 and sent[0]["content"] == full and len(full) > chat_module.TOOL_RESULT_KEEP

    # ...the session keeps the head and a one-line note.
    session = get_session_store().get(resp.headers["X-Session-Id"])
    stored = [m for m in session.history if m.get("role") == "tool"][0]
    head, note = stored["content"].rsplit("\n", 1)
    assert head == full[:chat_module.TOOL_RESULT_KEEP]
    assert note == chat_module.ELISION_NOTE.format(kept=chat_module.TOOL_RESULT_KEEP, total=len(full))
    assert stored["tool_call_id"] == sent[0]["tool_call_id"]

    # The next turn carries the capped version, not the full one.
    model.turns.append(text_turn("still here"))
    client.post("/api/chat", json={"message": "and?", "session_id": session.session_id}, headers=HEADERS)
    later = [m for m in model.requests[-1]["messages"] if m.get("role") == "tool"][0]
    assert later["content"] == stored["content"]


def test_small_tool_results_are_stored_as_they_are():
    message = {"role": "tool", "tool_call_id": "c1", "content": "x" * 100}
    assert chat_module.kept_tool_message(message) is message
    capped = chat_module.kept_tool_message({"role": "tool", "tool_call_id": "c1", "content": "y" * 20}, keep=8)
    assert capped["content"] == "y" * 8 + "\n" + chat_module.ELISION_NOTE.format(kept=8, total=20)


def test_only_a_parsable_spec_lands_in_the_session(client, model):
    from tests.test_api_v1 import spec_for

    model.turns += [
        tool_turn(("propose_spec", {"spec": {"task": "half a spec"}})),
        text_turn("that was not a spec"),
    ]
    resp = client.post("/api/chat", json={"message": "list levels"}, headers=HEADERS)
    assert resp.status_code == 200
    session = get_session_store().get(resp.headers["X-Session-Id"])
    assert session.spec is None                      # the unparsable one was not kept

    good = spec_for("query_levels")
    model.turns += [tool_turn(("propose_spec", {"spec": good})), text_turn("card shown")]
    client.post("/api/chat", json={"message": "again", "session_id": session.session_id}, headers=HEADERS)
    assert session.spec["action"] == {"kind": "run_tool", "tool": "query_levels", "code": None, "code_parameters": None}
    assert session.spec["task"] == "run query_levels"

    model.turns += [tool_turn(("propose_spec", {"spec": "not even an object"})), text_turn("nope")]
    client.post("/api/chat", json={"message": "once more", "session_id": session.session_id}, headers=HEADERS)
    assert session.spec["task"] == "run query_levels"   # still the last good one
    assert model.tool_results("propose_spec")[-1]["errors"][0]["code"] == "invalid_spec"
