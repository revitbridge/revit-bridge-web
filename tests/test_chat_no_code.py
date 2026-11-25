"""The chat writes no code: no get_guidance, no skills in the prompt, an execute_code
proposal refused without a card, and a reply that points to the workbench when no saved
tool fits."""
from __future__ import annotations

import json

from backend.api import chat as chat_module
from backend.api import model_tools
from backend.api.model_tools import CODE_NOT_OFFERED, TOOL_DEFINITIONS, TOOL_NAMES, WORKBENCH_PATH
from backend.session import get_session_store
from backend.skill_store import get_skill_store
from tests.fake_model import text_turn, tool_turn
from tests.test_api_v1 import code_spec, spec_for
from tests.test_host_loop import HEADERS, events, model, sse, tokens  # noqa: F401 - the model fixture

TOOLS = ["get_project_snapshot", "query", "list_tools", "get_tool_choices", "missing_params", "reconcile",
         "propose_spec", "ask_designer", "set_plan"]


def test_the_tools_array_has_no_get_guidance_and_does_not_change(client, model):
    assert [t["function"]["name"] for t in TOOL_DEFINITIONS] == TOOLS and set(TOOLS) == TOOL_NAMES
    assert "get_guidance" not in json.dumps(TOOL_DEFINITIONS)
    model.turns += [tool_turn(("list_tools", {})), text_turn("one"), text_turn("two")]
    sid = client.post("/api/chat", json={"message": "a"}, headers=HEADERS).headers["X-Session-Id"]
    client.post("/api/chat", json={"message": "b", "session_id": sid}, headers=HEADERS)
    sent = {json.dumps(r["tools"], ensure_ascii=False) for r in model.requests}
    assert sent == {json.dumps(TOOL_DEFINITIONS, ensure_ascii=False)}


def test_the_query_tool_is_described_as_the_package_describes_it():
    """The kinds 0.9 added (links, type_categories) and args.link_id, in the MCP tool's own words."""
    import revit_bridge.mcp_server as mcp
    described = next(t for t in TOOL_DEFINITIONS if t["function"]["name"] == "query")["function"]["description"]
    assert " ".join(mcp.query.__doc__.split()).startswith(described)
    for words in ("links (every linked model instance", "type_categories (", "args.link_id",
                  "must never be passed to a tool that acts on the open model."):
        assert words in described, words


def test_a_get_guidance_call_is_an_unknown_tool(client, model):
    model.turns += [tool_turn(("get_guidance", {"stage": "orient"})), text_turn("ok")]
    assert client.post("/api/chat", json={"message": "x"}, headers=HEADERS).status_code == 200
    reply = model.tool_results("get_guidance")[0]
    assert reply["error"] == "unknown_tool" and "get_guidance" not in reply["known"]


def test_the_system_prompt_is_the_package_text_without_code_and_one_host_line(client, model):
    system = chat_module.build_system_prompt(True)
    package = chat_module.host_instructions(code=False, mentions=True)
    assert system == package.rstrip("\n") + "\n" + chat_module.WORKBENCH_LINE
    # The package's text is the no-code one, with the selections rule; nothing of the code variant.
    assert "## Designer's selections" in package and "- Write code:" in package
    for words in ("execute_code", "snippet contract", "Snippet contract", "blocked_code", "get_guidance",
                  "check_code"):
        assert words not in system, words
    # The host adds exactly one line, the last one, and it is the only place the workbench is named.
    host_lines = [line for line in system.split("\n") if line not in package.split("\n")]
    assert host_lines == [chat_module.WORKBENCH_LINE] and system.split("\n")[-1] == chat_module.WORKBENCH_LINE
    assert system.count(f"[workbench]({WORKBENCH_PATH})") == 1 and "workbench" not in package
    assert "\n" not in chat_module.WORKBENCH_LINE and chat_module.WORKBENCH_LINE.startswith("- ")
    # No skills, no lesson pointer - not even the enabled ones.
    store = get_skill_store()
    enabled = [s for s in store.list_all() if s.get("enabled")]
    assert enabled, "the wheel's revit-core skill is enabled by default"
    for skill in enabled:
        assert store.get(skill["id"])["content"].strip() not in system
    assert "## Skills" not in system and "## Guidance" not in system and "call get_guidance" not in system
    assert "# revit-core" not in system
    # The package says the model writes no code; the host's line says where a missing tool is made.
    assert "Write code: if no tool fits, say that no existing tool can do it." in system
    assert "{kind: run_tool, tool}" in system and "propose_spec" in system
    for words in ("When no tool fits", "list_tools", f"[workbench]({WORKBENCH_PATH})"):
        assert words in chat_module.WORKBENCH_LINE, words
    assert "ask_designer" in system and "确认并执行" in system
    assert "wait for an explicit yes" not in system and "in this host\'s interface" not in system
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
    answer = ("No saved tool here can dimension a grid. The tool you would need: dimension the selected grids at an "
              f"angle. You can make it in the [workbench]({WORKBENCH_PATH}).")
    model.turns += [tool_turn(("list_tools", {})), text_turn(answer)]
    resp = client.post("/api/chat", json={"message": "dimension grid A at an angle"}, headers=HEADERS)
    assert resp.status_code == 200
    frames = sse(resp.text)
    assert tokens(frames) == answer and events(frames, "spec") == [] and frames[-1] == ("done", None)
    listing = model.tool_results("list_tools")[0]
    assert all({"builtin", "origin"} <= set(t) and "directory" not in t for t in listing)
    assert not any("dimension" in t["name"] for t in listing)
    assert model.requests[0]["messages"][0]["content"].endswith("\n" + chat_module.WORKBENCH_LINE)


def test_the_code_not_offered_message_is_ascii_and_names_the_workbench():
    assert model_tools.CODE_NOT_OFFERED_MESSAGE.isascii()
    assert model_tools.WORKBENCH_LINK == f"[workbench]({WORKBENCH_PATH})" == "[workbench](/capabilities/authoring)"
