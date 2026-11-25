"""Workflow authoring and host-built plans with a scripted provider and add-in."""
import json
from copy import deepcopy

import pytest
from revit_bridge.capabilities import ToolStore
from revit_bridge.workflows import WorkflowStore, validate_workflow

from tests.conftest import FIXTURE_ADMIN_PASSWORD
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit, SNAPSHOT_RESULT
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_auto_run import client as local_client
from tests.test_host_loop import events, tokens
from tests.test_multi_step import execute, request
from tests.test_roles import DONE, ROUTING_TOOL, model


BASE = B + "/workflows"
ADMIN = {"X-Admin-Token": FIXTURE_ADMIN_PASSWORD}


def definition():
    return {"schema_version": 1, "kind": "workflow", "name": "wall_window", "display_name": "Wall and window",
            "summary": "Build and glaze", "description": "Place the window on the newly built wall.",
            "version": "1.0.0", "inputs": [], "steps": [
                {"id": "wall", "tool": "create_wall", "title": "Build wall", "params": dict(WALL)},
                {"id": "window", "tool": "create_window_on_wall", "title": "Place window", "params": {
                    "wall_id": {"from": "wall", "path": "ElementId"}, "type_name": "OST_Windows-TypeA",
                    "offset_along_wall": 1500, "sill_height": 900}}]}


def save(client, data):
    response = client.put(f"{BASE}/{data['name']}", json=data, headers=ADMIN)
    assert response.status_code == 200, response.text


def test_authoring_round_trip_and_permissions(client):
    data = definition()
    url = BASE + "/wall_window"
    assert client.post(BASE + "/validate", json=data).json() == {"errors": []}
    assert client.get(BASE).json() == []
    assert client.put(url, json=data).status_code == 403
    save(client, data)
    assert client.put(url, json=data, headers=ADMIN).status_code == 409
    assert client.put(url + "?overwrite=true", json=data, headers=ADMIN).status_code == 200
    assert client.get(url).json() == data == client.get(BASE).json()[0]
    yaml = client.get(url + "/export").text
    assert client.delete(url).status_code == 403
    assert client.delete(url, headers=ADMIN).status_code == 200
    assert client.get(url).status_code == 404
    headers = {"Content-Type": "text/yaml"}
    assert client.post(BASE + "/import", content=yaml, headers=headers).status_code == 403
    assert client.post(BASE + "/import", content=yaml, headers={**headers, **ADMIN}).status_code == 200
    assert client.get(url + "/export").text == yaml
    assert client.post(BASE + "/import", content="&x [*x]", headers={**headers, **ADMIN}).json()["errors"]


@pytest.mark.parametrize("mutation", [
    {"extra": True, "schema_version": True, "kind": 1, "name": "CON", "display_name": "", "description": "",
     "summary": "x" * 121, "version": "old", "inputs": {}, "steps": {}},
    {"name": "create_wall", "summary": "", "steps": []},
    {"inputs": [1, {"name": "!", "type": {}, "description": "", "ask": 1, "wrong": True},
                {"name": "twice"}, {"name": "twice"}], "steps": [0, {"id": "-", "tool": "wall_window"}]},
    {"steps": [{"id": "s", "tool": "query_levels", "unexpected": 1, "params": {}}] * 9},
    {"steps": [{"id": "dup", "tool": "missing", "title": "", "params": {"x": [], "y": {"input": "absent", "z": 0}}},
               {"id": "dup", "tool": "create_wall", "title": "Wall", "params": {"bogus": {"from": "later", "path": "-1"}}}]},
])
def test_every_package_problem_is_returned(client, mutation):
    data = {**definition(), **mutation}
    expected = validate_workflow(data, ToolStore())
    assert expected and client.post(BASE + "/validate", json=data).json() == {"errors": expected}
    assert not client.get(BASE).json()


def test_validate_non_mapping_and_nested_fixture_result_paths(client):
    from tests.test_pack_admin import IMPORT_YAML
    assert client.post(BASE + "/validate", json=[]).json()["errors"] == validate_workflow([], ToolStore())
    headers = {**ADMIN, "Content-Type": "text/yaml"}
    pack = IMPORT_YAML + "fixtures: [{name: normal, params: {}, expect: {Items: [{Id: 12}], Done: true}}]"
    assert client.post(B + "/tools/import", headers=headers, content=pack).is_success
    assert client.get(BASE + "/tools/count_doors/result-paths").json() == ["Done", "Items.0.Id"]
    assert "ElementId" in client.get(BASE + "/tools/create_wall/result-paths").json()


def route(model, inputs=None):
    model.script("analyst", json_turn({"route": "tool", "tool": "wall_window", "note": "", "facts_needed": []}))
    model.script("planner", tool_turn(("use_workflow", {"name": "wall_window", "inputs": inputs or {}})))


@pytest.fixture
def writes(revit, monkeypatch):
    monkeypatch.setitem(SNAPSHOT_RESULT["CategoryNames"], "OST_Windows", "Windows")
    original = revit.handler
    operations = []
    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
            return FakeRevit.code_result(req["id"], 1 + len(operations))
        if "Wall.Create(document" in code or "document.Create.NewFamilyInstance(" in code:
            operations.append(code)
            return FakeRevit.code_result(req["id"], {"Length": 3000., "ElementId": 23090 + len(operations)})
        return original(req)
    revit.handler = handler
    return operations


@pytest.mark.parametrize("mode", ["auto", "confirm"])
def test_saved_steps_use_the_recorded_id_in_order(local_client, writes, model, mode):
    client = local_client
    save(client, definition())
    route(model)
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    brief = "Run wall_window." if mode == "auto" else "Build a wall and place a window on it."
    sid, frames = request(client, message=brief, run=mode)
    if mode == "confirm":
        assert len(writes) == 0
        for count in (1, 2):
            spec = events(frames, "spec")[-1]["spec"]
            receipt = execute(client, sid, spec)
            assert receipt["success"] and len(writes) == count
            _, more = request(client, session_id=sid, execution=receipt)
            frames += more
    first, second = [c["spec"] for c in events(frames, "spec")]
    assert first["parameters"][0]["evidence"].startswith("workflow:wall_window@1.0.0#wall.")
    result = events(frames, "execution")[0]
    param = next(p for p in second["parameters"] if p["name"] == "wall_id")
    assert param["value"] == result["result"]["ElementId"] == 23091
    assert param["evidence"] == "result:" + result["evidence_id"] + "#ElementId"
    assert len(writes) == 2 and len(model.requests_of("planner")) == 1
    assert events(frames, "plan")[-1]["workflow"] == "wall_window"
    assert "Build and glaze" in str(model.requests_of("analyst")) and "newly built wall" in str(model.requests_of("analyst"))
    assert "Build and glaze" in str(model.requests_of("planner")) and "newly built wall" in str(model.requests_of("planner"))
    if mode == "auto":
        assert [c["run"] for c in events(frames, "spec")] == ["auto", "auto"]
        assert client.get(B + "/evidence").text.count("designer:auto-run") == 2


def test_missing_input_uses_verified_form_answer_even_when_model_guesses(local_client, writes, model):
    data = definition()
    data["inputs"] = [{"name": "wall_height", "type": "double", "description": "Height in mm", "ask": True}]
    data["steps"][0]["params"]["height"] = {"input": "wall_height"}
    save(local_client, data)
    route(model, {"wall_height": 123456})
    sid, frames = request(local_client, message="Run wall_window", run="auto")
    form, = events(frames, "questions")
    assert not writes and not events(frames, "plan") and not events(frames, "spec")
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    answer = {"ask_id": form["ask_id"], "items": [{"id": form["questions"][0]["id"], "value": "3150", "label": "3150"}]}
    _, frames = request(local_client, session_id=sid, message="3150", answers=answer, run="auto")
    assert len(writes) == 2 and len(model.requests_of("planner")) == 1
    height = next(p for p in events(frames, "spec")[0]["spec"]["parameters"] if p["name"] == "height")
    assert (height["source"], height["value"], height["evidence"]) == ("answer", 3150, form["ask_id"] + "#wall_height")


def test_routing_alone_cannot_authorize_workflow_values(local_client, writes, model):
    save(local_client, definition())
    route(model)
    sid, frames = request(local_client, message="What does wall_window do?", run="auto")
    form, = events(frames, "questions")
    assert "wall_window" in form["questions"][0]["text"] and not writes
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    run = form["questions"][0]["options"][0]
    _, frames = request(local_client, session_id=sid, message="Run", answers={"ask_id": form["ask_id"],
                         "items": [{"id": "invoke", **run}]}, run="auto")
    assert len(writes) == 2 and all(c["run"] == "auto" for c in events(frames, "spec"))


def test_tampered_fixed_value_and_out_of_request_reference_refused(local_client, writes, model):
    from backend.api.auto_run import AutoRun
    from backend.api.model_tools import call_tool
    from backend.config import get_settings
    from backend.session import get_session_store, Session
    import asyncio

    save(local_client, definition())
    route(model)
    sid, frames = request(local_client, message="Run wall_window")
    session = get_session_store().get(sid)
    event, = events(frames, "spec")
    policy = AutoRun(session, None, get_settings(), False, "", None)
    assert policy.inspect(event) == (None, None)
    copied = deepcopy(event)
    copied["spec"]["parameters"][0]["value"] = "L2"
    proposal = asyncio.run(call_tool("propose_spec", {"step_id": "wall", "spec": copied["spec"]}, session))
    assert not proposal.spec_event and not proposal.result["accepted"]
    assert policy.inspect(copied)[0] == "unverified_evidence"
    # Even a forged proposal in host memory would still need the saved literal.
    session.plan.proposed = copied["spec"]
    assert policy.inspect(copied)[0] == "unverified_evidence"
    session.plan.proposed = event["spec"]
    assert AutoRun(Session("other"), None, get_settings(), False, "", None).inspect(event)[0] == "unverified_evidence"
    response = local_client.post(B + "/spec/confirm", json={"session_id": sid, "spec": copied["spec"]})
    assert response.status_code == 422 and not writes
    data = definition()
    data["steps"][0]["params"]["height"] = 2300
    assert local_client.put(BASE + "/wall_window?overwrite=true", json=data).is_success
    assert policy.inspect(event)[0] == "unverified_evidence"


@pytest.mark.parametrize("drift", ["deleted", "changed"])
def test_stale_later_tool_fails_before_first_card(local_client, model, writes, drift):
    from tests.test_pack_admin import IMPORT_YAML
    from tests.fake_model import text_turn
    client = local_client
    imported = client.post(B + "/tools/import", content=IMPORT_YAML, headers={"Content-Type": "text/yaml"})
    assert imported.is_success, imported.text
    data = definition()
    data["steps"][1].update(tool="count_doors", params={})
    save(client, data)
    if drift == "deleted":
        assert client.delete(B + "/tools/count_doors").is_success
    else:
        changed = IMPORT_YAML.replace("parameters: []", "parameters: [{name: height, type: double, source: designer, "
                                     "description: Height, required: true}]")
        assert client.post(B + "/tools/import?overwrite=true", content=changed, headers={"Content-Type": "text/yaml"}).is_success
    # The package omits the now invalid definition from the catalogue. Simulate a
    # planner that remembers its name, including a second tool call in the same reply.
    model.script("analyst", json_turn({"route": "tool", "note": "", "facts_needed": []}))
    model.script("planner", tool_turn(("use_workflow", {"name": "wall_window", "inputs": {}}),
                                     ("propose_spec", {"spec": {}})), text_turn("End"))
    _, frames = request(client, message="Run wall_window", run="auto")
    assert "steps[1]" in tokens(frames) and ("unknown tool" if drift == "deleted" else "required parameter") in tokens(frames)
    assert not writes and not events(frames, "spec") and not events(frames, "plan")


def test_delete_step_requires_click(local_client, model, writes):
    data = definition()
    data["steps"][1].update(tool="delete_selected", title="Delete selection", params={})
    save(local_client, data)
    route(model)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(local_client, message="Run wall_window", run="auto")
    first, second = events(frames, "spec")
    assert first["run"] == "auto" and second["run_reason"] == "excluded_tool"
    assert second["run"] == "confirm" and len(writes) == len(events(frames, "execution")) == 1


def test_late_incompatible_sources_and_types_are_reported_before_execution(local_client, model, writes):
    data = definition()
    data["steps"][1]["params"].update(sill_height={"from": "wall", "path": "ElementId"}, offset_along_wall="bad")
    # A package-valid workflow saved by an older host remains readable.
    WorkflowStore(ToolStore()).save(data)
    route(model)
    _, frames = request(local_client, message="Run wall_window", run="auto")
    assert "sill_height" in tokens(frames) and "offset_along_wall" in tokens(frames)
    assert not writes and not events(frames, "spec") and not events(frames, "plan")


def test_workflow_obeys_step_budget(local_client, model, writes, monkeypatch):
    from backend.config import get_settings
    monkeypatch.setenv("AUTO_RUN_MAX_STEPS", "1")
    get_settings.cache_clear()
    save(local_client, definition())
    route(model)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(local_client, message="Run wall_window", run="auto")
    assert len(writes) == 1 and events(frames, "spec")[-1]["run_reason"] == "step_limit"


def test_workflow_selection_writer_makes_implicit_selection_wait(local_client, model, revit):
    data = definition()
    data["steps"][0].update(tool="select_elements", params={"element_ids": "93,94"})
    data["steps"][1].update(tool="rotate_elements", params={"angle": 90})
    save(local_client, data)
    route(model)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(local_client, message="Run wall_window", run="auto")
    first, second = events(frames, "spec")
    assert first["run"] == "auto" and second["assistant_selection"]["tool"] == "select_elements"
    assert second["run_reason"] == "unverified_evidence" and len(events(frames, "execution")) == 1


def test_missing_result_pauses_and_a_skipped_failure_preserves_clicks(local_client, model, writes):
    data = definition()
    data["steps"][1]["params"]["wall_id"]["path"] = "unknown.0"
    data["steps"].append({**data["steps"][0], "id": "another_wall"})
    save(local_client, data)
    route(model)
    model.script("supervisor", json_turn(DONE))
    sid, frames = request(local_client, message="Run wall_window", run="auto")
    form, = events(frames, "questions")
    assert [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["done", "failed", "pending"]
    assert "unknown.0" in tokens(frames) and len(writes) == 1
    q = form["questions"][0]
    skip = next(o for o in q["options"] if o["value"] == "continue")
    _, continued = request(local_client, session_id=sid, message=skip["label"], run="auto",
                           answers={"ask_id": form["ask_id"], "items": [{"id": q["id"], **skip}]})
    assert events(continued, "spec")[0]["run_reason"] == "previous_failure" and len(writes) == 1


def choose(client, sid, form, value):
    question, = form["questions"]
    option = next(o for o in question["options"] if o["value"] == value)
    return request(client, session_id=sid, message=option["label"], run="auto",
                   answers={"ask_id": form["ask_id"], "items": [{"id": question["id"], **option}]})[1]


ASK = {"verdict": "ask", "report": "The wall was built.", "question": "What should change?"}


@pytest.mark.parametrize("final_step", [False, True])
def test_revise_success_hands_completed_evidence_to_planner(local_client, model, writes, final_step):
    save(local_client, definition())
    route(model)
    if final_step:
        model.script("supervisor", json_turn(DONE))
    model.script("supervisor", json_turn(ASK), json_turn(DONE), json_turn(DONE))
    sid, frames = request(local_client, message="Run wall_window", run="auto")
    evidence = events(frames, "execution")[-1]["evidence_id"]
    count = len(writes)
    assert count == (2 if final_step else 1)
    model.script("planner", tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)})))
    revised = choose(local_client, sid, events(frames, "questions")[0], "revise")
    assert len(writes) == count and not events(revised, "execution")
    steps = events(revised, "plan")[-1]["steps"]
    assert [s["status"] for s in steps] == ["done", "done" if final_step else "skipped"]
    assert steps[count - 1]["evidence_id"] == evidence
    message = model.requests_of("planner")[-1]["messages"][-1]["content"]
    assert "Revise the task" in message and evidence in message and "wall_window" in message
    card, = events(revised, "spec")
    assert card["run"] == "confirm" and card["run_reason"] == "workflow_changed"


def test_current_message_must_invoke_workflow(local_client, model, writes):
    from tests.test_questions import FORM
    save(local_client, definition())
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("ask_designer", FORM)))
    sid, first = request(local_client, message="Run wall_window", run="auto")
    assert events(first, "questions")
    route(model)
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(local_client, session_id=sid, message="No. Just tell me how many walls.", run="auto")
    assert not writes and events(frames, "questions")[0]["questions"][0]["id"] == "invoke"


def test_continue_successful_ask_uses_recorded_wall_once(local_client, model, writes):
    save(local_client, definition())
    route(model)
    model.script("supervisor", json_turn(ASK), json_turn(DONE))
    sid, first = request(local_client, message="请使用工作流 Wall and window", run="auto")
    wall, = events(first, "execution")
    assert events(first, "plan")[-1]["steps"][0]["status"] == "done"
    frames = choose(local_client, sid, events(first, "questions")[0], "next")
    card, = events(frames, "spec")
    binding = next(p for p in card["spec"]["parameters"] if p["name"] == "wall_id")
    assert binding["evidence"] == "result:" + wall["evidence_id"] + "#ElementId"
    assert len(writes) == 2 and [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["done", "done"]


@pytest.mark.parametrize("message,collision", [("go", None), ("wall_window", None), ("use wall_window", "wall_window"),
                                               ("execute go", "go"), ("run go workflow", "go workflow")])
def test_bare_or_ambiguous_invocation_shows_ordered_preview(local_client, model, writes, message, collision):
    data = {**definition(), "display_name": "go"}
    save(local_client, data)
    if collision:
        save(local_client, {**data, "name": "another", "display_name": collision})
    route(model)
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(local_client, message=message, run="auto")
    assert not writes
    form, = events(frames, "questions")
    assert form["workflow_preview"] == {"summary": data["summary"], "steps": [
        {"title": s["title"], "tool": s["tool"]} for s in data["steps"]]}


def test_no_literals_still_requires_invocation(local_client, model, writes):
    data = definition()
    for step in data["steps"]:
        step.update(tool="query_levels", params={})
    save(local_client, data)
    route(model)
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(local_client, message="What is wall_window?", run="auto")
    assert not events(frames, "execution") and events(frames, "questions")[0]["questions"][0]["id"] == "invoke"


def test_model_dropping_a_step_pauses_remaining_cards(local_client, model, writes):
    data = definition()
    data["steps"].append({**data["steps"][0], "id": "last"})
    save(local_client, data)
    route(model)
    sid, _ = request(local_client, message="Run wall_window", run="confirm")
    model.script("analyst", json_turn(ROUTING_TOOL))
    retained = [{k: s[k] for k in ("id", "title", "tool")} for s in data["steps"][::2]]
    model.script("planner", tool_turn(("set_plan", {"steps": retained})))
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(local_client, session_id=sid, message="Drop the window", run="auto")
    assert not writes
    first, = events(frames, "spec")
    assert (first["run"], first["run_reason"]) == ("confirm", "workflow_changed")
    receipt = execute(local_client, sid, first["spec"])
    _, frames = request(local_client, session_id=sid, execution=receipt, run="auto")
    second, = events(frames, "spec")
    assert second["run_reason"] == "workflow_changed" and len(writes) == 1


def test_authoring_reports_same_binding_errors_as_use(local_client, model, writes):
    data = definition()
    data["inputs"] = [{"name": "distance", "type": "string", "description": "distance", "ask": True}]
    data["steps"][1]["params"].update(sill_height={"from": "wall", "path": "ElementId"},
                                     offset_along_wall={"input": "distance"})
    data["steps"][0]["params"]["height"] = "bad"
    errors = local_client.post(BASE + "/validate", json=data).json()["errors"]
    for name in ("sill_height", "offset_along_wall", "height"):
        assert any(name in e for e in errors)
    for response in (local_client.put(BASE + "/wall_window", json=data),
                     local_client.post(BASE + "/import", content=json.dumps(data), headers={"Content-Type": "text/yaml"})):
        assert response.status_code == 422 and response.json()["errors"] == errors
    assert not local_client.get(BASE).json()
    WorkflowStore(ToolStore()).save(data)
    route(model)
    _, frames = request(local_client, message="Run wall_window", run="auto")
    assert all(e in tokens(frames) for e in errors) and not writes and not events(frames, "questions")


def test_ask_input_ignores_earlier_verified_answers(local_client, model, writes):
    data = definition()
    data["inputs"] = [{"name": "height", "type": "double", "description": "height in mm", "ask": True}]
    data["steps"][0]["params"]["height"] = {"input": "height"}
    save(local_client, data)
    model.script("analyst", json_turn(ROUTING_TOOL))
    form = {"questions": [{"id": "height", "text": "Height?", "options": [
        {"label": "3100 mm", "value": 3100}, {"label": "3200 mm", "value": 3200}]}]}
    model.script("planner", tool_turn(("ask_designer", form)),
                 tool_turn(("use_workflow", {"name": "wall_window", "inputs": {"height": 3100}})))
    sid, frames = request(local_client, message="Run wall_window", run="auto")
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    frames = choose(local_client, sid, events(frames, "questions")[0], 3100)
    frames = choose(local_client, sid, events(frames, "questions")[0], "run")
    assert not writes and "height" in events(frames, "questions")[0]["questions"][0]["text"]


@pytest.mark.parametrize("stated", [False, True])
def test_ask_input_even_if_unused(local_client, model, writes, stated):
    data = definition()
    data["inputs"] = [{"name": "phase", "type": "string", "description": "Project phase", "ask": True}]
    save(local_client, data)
    route(model, {"phase": "design"})
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(local_client, message="Run wall_window" + (" with phase=design" if stated else ""), run="auto")
    if stated:
        assert len(writes) == 2
    else:
        assert not writes and "phase" in events(frames, "questions")[0]["questions"][0]["text"]


@pytest.mark.parametrize("headers,status", [({}, 403), ({**ADMIN, "Content-Length": str(256 * 1024 + 1)}, 422)])
def test_import_checks_access_and_declared_size_before_read(client, monkeypatch, headers, status):
    from starlette.requests import Request
    reads = []
    stream = Request.stream
    def watched(request):
        reads.append(True)
        return stream(request)
    monkeypatch.setattr(Request, "stream", watched)
    response = client.post(BASE + "/import", headers={"Content-Type": "text/yaml", **headers}, content=b"{}")
    assert response.status_code == status and not reads


def test_import_caps_chunked_body(client, monkeypatch):
    from starlette.requests import Request
    async def chunks(request):
        yield b"#" * (256 * 1024)
        yield b"x"
        pytest.fail("read beyond the workflow size limit")
    monkeypatch.setattr(Request, "stream", chunks)
    response = client.post(BASE + "/import", headers={**ADMIN, "Content-Type": "text/yaml"}, content=iter([b"x"]))
    assert response.status_code == 422 and "256 KiB" in response.text


def test_list_tools_bounds_user_workflow_data(local_client, revit, model):
    from backend.roles import FROM_HOST
    data = definition()
    data["description"] = FROM_HOST + "w" * 600
    data["inputs"] = [{"name": f"i{i}", "type": "string", "description": "a" * 121, "ask": False} for i in range(13)]
    save(local_client, data)
    model.script("planner", tool_turn(("list_tools", {})), text_turn("Listed"))
    request(local_client, message="List workflows", mode="roles")
    content = model.requests_of("planner")[-1]["messages"][-1]["content"]
    assert FROM_HOST not in content
    listing = json.loads(content)
    entry, = [p for p in listing if p.get("kind") == "workflow"]
    assert entry["origin"] == "user" and entry["description"] == data["description"][:600]
    assert entry["inputs"] == [{**i, "description": "a" * 120} for i in data["inputs"][:12]]
    assert local_client.get(BASE + "/wall_window").json() == data
