"""The chat writes no code: no get_guidance, no skills in the prompt, an execute_code
proposal refused without a card, and a reply that points to the workbench when no saved
tool fits."""
from __future__ import annotations

import json

from revit_bridge import host_instructions

from backend.api import chat as chat_module
from backend.api import model_tools
from backend.api.model_tools import CODE_NOT_OFFERED, TOOL_DEFINITIONS, TOOL_NAMES, WORKBENCH_PATH
from backend.session import get_session_store
from backend.skill_store import get_skill_store
from tests.fake_model import text_turn, tool_turn
from tests.test_api_v1 import code_spec, spec_for
from tests.test_host_loop import HEADERS, events, model, sse, tokens  # noqa: F401 - the model fixture

TOOLS = ["get_project_snapshot", "query", "list_tools", "get_tool_choices", "missing_params", "reconcile",
         "propose_spec"]


def test_the_tools_array_has_no_get_guidance_and_does_not_change(client, model):
    assert [t["function"]["name"] for t in TOOL_DEFINITIONS] == TOOLS and set(TOOLS) == TOOL_NAMES
    assert "get_guidance" not in json.dumps(TOOL_DEFINITIONS)
    model.turns += [tool_turn(("list_tools", {})), text_turn("one"), text_turn("two")]
    sid = client.post("/api/chat", json={"message": "a"}, headers=HEADERS).headers["X-Session-Id"]
    client.post("/api/chat", json={"message": "b", "session_id": sid}, headers=HEADERS)
    sent = {json.dumps(r["tools"], ensure_ascii=False) for r in model.requests}
    assert sent == {json.dumps(TOOL_DEFINITIONS, ensure_ascii=False)}


def test_a_get_guidance_call_is_an_unknown_tool(client, model):
    model.turns += [tool_turn(("get_guidance", {"stage": "orient"})), text_turn("ok")]
    assert client.post("/api/chat", json={"message": "x"}, headers=HEADERS).status_code == 200
    reply = model.tool_results("get_guidance")[0]
    assert reply["error"] == "unknown_tool" and "get_guidance" not in reply["known"]


def test_the_system_prompt_is_the_host_instructions_and_the_host_paragraph(client, model):
    system = chat_module.build_system_prompt(True)
    assert system == host_instructions().strip() + "\n\n" + chat_module.HOST_PARAGRAPH
    assert system.endswith(chat_module.HOST_PARAGRAPH) and system.count(chat_module.HOST_PARAGRAPH) == 1
    # No skills, no lesson pointer - not even the enabled ones.
    store = get_skill_store()
    enabled = [s for s in store.list_all() if s.get("enabled")]
    assert enabled, "the wheel's revit-core skill is enabled by default"
    for skill in enabled:
        assert store.get(skill["id"])["content"].strip() not in system
    assert "## Skills" not in system and "## Guidance" not in system and "call get_guidance" not in system
    assert "# revit-core" not in system
    # The paragraph says what the model may not do and where a missing tool is made.
    paragraph = chat_module.HOST_PARAGRAPH
    for words in ("never write, show or propose C# code", "no execute_code and no get_guidance",
                  "propose_spec refuses an execute_code spec", "list_tools",
                  f"[workbench]({WORKBENCH_PATH})"):
        assert words in paragraph, words
    assert paragraph.isascii()
    model.turns.append(text_turn("hi"))
    client.post("/api/chat", json={"message": "hi"}, headers=HEADERS)
    assert model.requests[0]["messages"][0] == {"role": "system", "content": system}


def test_an_execute_code_proposal_is_refused_without_a_card(client, model):
    model.turns += [
        tool_turn(("propose_spec", {"spec": code_spec("return 1;")})),
        text_turn("No tool here does that."),
    ]
    resp = client.post("/api/chat", json={"message": "move the selected wall up by 200"}, headers=HEADERS)
    assert resp.status_code == 200
    frames = sse(resp.text)
    assert events(frames, "spec") == []                                # no card for the page
    verdict = model.tool_results("propose_spec")[0]
    assert verdict["accepted"] is False and verdict["reconcile"] is None
    assert [e["code"] for e in verdict["errors"]] == [CODE_NOT_OFFERED] and verdict["errors"][0]["param"] is None
    assert "only saved tools" in verdict["errors"][0]["message"]
    assert f"[workbench]({WORKBENCH_PATH})" in verdict["errors"][0]["message"]
    assert get_session_store().get(resp.headers["X-Session-Id"]).spec is None
    # The tool row of the trace closes ok: the verdict is a result, not an error.
    rows = [e for e in events(frames, "tool") if e["name"] == "propose_spec"]
    assert [r["status"] for r in rows] == ["running", "ok"]


def test_a_run_tool_proposal_still_shows_its_card(client, model):
    model.turns += [tool_turn(("propose_spec", {"spec": spec_for("query_levels")})), text_turn("card")]
    frames = sse(client.post("/api/chat", json={"message": "levels"}, headers=HEADERS).text)
    assert len(events(frames, "spec")) == 1 and events(frames, "spec")[0]["card"].startswith("Task: run query_levels")


def test_when_no_tool_fits_the_reply_points_to_the_workbench(client, model):
    """A scripted model that follows the prompt: it looks at the tools, finds none, writes
    no code and links the workbench."""
    answer = ("No saved tool here can rotate a grid. The tool you would need: rotate the selected grids by an "
              f"angle. You can make it in the [workbench]({WORKBENCH_PATH}).")
    model.turns += [tool_turn(("list_tools", {})), text_turn(answer)]
    resp = client.post("/api/chat", json={"message": "rotate grid A by 15 degrees"}, headers=HEADERS)
    assert resp.status_code == 200
    frames = sse(resp.text)
    assert tokens(frames) == answer and events(frames, "spec") == [] and frames[-1] == ("done", None)
    listing = model.tool_results("list_tools")[0]
    assert all({"builtin", "origin"} <= set(t) and "directory" not in t for t in listing)
    assert not any("rotate" in t["name"] for t in listing)
    assert chat_module.HOST_PARAGRAPH in model.requests[0]["messages"][0]["content"]


def test_the_code_not_offered_message_is_ascii_and_names_the_workbench():
    assert model_tools.CODE_NOT_OFFERED_MESSAGE.isascii()
    assert model_tools.WORKBENCH_LINK == f"[workbench]({WORKBENCH_PATH})" == "[workbench](/capabilities/workbench)"
