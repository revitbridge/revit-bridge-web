"""Multi-step plans through chat, the confirmation gate and a scripted Revit add-in."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from backend.log_store import get_log_store
from backend.session import get_session_store
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit, SNAPSHOT_RESULT
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_host_loop import HEADERS, events, sse, tokens
from tests.test_roles import ROUTING_TOOL, client, model  # noqa: F401 - role fixtures

BRIEF = "Build a wall then place a window at its end. The sill is 900 mm."
STEPS = [{"id": "wall", "title": "Build the wall", "tool": "create_wall"},
         {"id": "window", "title": "Place the window", "tool": "create_window_on_wall"}]


def request(client, *, session_id=None, **body):
    response = client.post("/api/chat", headers={**HEADERS, "X-Run-Label": "plan-test"},
                           json={"session_id": session_id, **body})
    assert response.status_code == 200, response.text
    frames = sse(response.text)
    assert not events(frames, "error"), frames
    return response.headers["X-Session-Id"], frames


def propose_wall(step="wall"):
    return tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL), "step_id": step}))


def start(client, model):
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})), propose_wall(), text_turn("Wall card."))
    sid, frames = request(client, message=BRIEF)
    assert [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["proposed", "pending"]
    return sid, events(frames, "spec")[0]["spec"]


def confirm(client, sid, spec):
    response = client.post(f"{B}/spec/confirm", json={"spec": spec, "session_id": sid})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def execute(client, sid, spec):
    token = confirm(client, sid, spec)
    response = client.post(f"{B}/tools/{spec['action']['tool']}/run", json={
        "token": token, "params": {p["name"]: p["value"] for p in spec["parameters"]}})
    assert response.status_code == 200, response.text
    return response.json()


def test_wall_result_reaches_window_card_and_final_report(client, model, revit, monkeypatch):
    monkeypatch.setitem(SNAPSHOT_RESULT["CategoryNames"], "OST_Windows", "Windows")
    original = revit.handler
    operations = []

    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
            return FakeRevit.code_result(req["id"], 1 + len(operations))
        if "Wall.Create(document" in code or "document.Create.NewFamilyInstance(" in code:
            operations.append(code)
            return FakeRevit.code_result(req["id"], {"Length": 3000.0, "ElementId": 23091 + len(operations)})
        return original(req)

    revit.handler = handler
    sid, first = start(client, model)
    first_result = execute(client, sid, first)
    assert first_result["success"], first_result
    wall_id = first_result["result"]["ElementId"]
    # Evict every original message, as a long conversation can. The plan and the
    # actual model result must still reach the next planner request independently.
    session = get_session_store().get(sid)
    for i in range(80):
        session.add_message("user", f"Earlier detail {i}")
    assert all(BRIEF not in str(m) for m in session.history)
    window = spec_for("create_window_on_wall", wall_id=str(wall_id), type_name="OST_Windows-TypeA",
                      offset_along_wall=1500, sill_height=900)
    model.script("supervisor", json_turn({"verdict": "done", "report": "The wall was built."}))
    model.script("planner", tool_turn(("propose_spec", {"spec": window, "step_id": "window"})), text_turn("Window card."))
    before = len(model.requests_of("planner"))
    # JSON.stringify in the browser writes an integral float without its decimal part.
    browser_result = {**first_result, "result": {**first_result["result"], "Length": 3000}}
    _, frames = request(client, session_id=sid, execution=browser_result)
    steps = events(frames, "plan")[-1]["steps"]
    assert [s["status"] for s in steps] == ["done", "proposed"]
    assert steps[0]["evidence_id"] == first_result["evidence_id"]
    sent = json.dumps(model.requests_of("planner")[before]["messages"], ensure_ascii=False)
    assert BRIEF in sent and str(wall_id) in sent and first_result["evidence_id"] in sent
    next_card, = events(frames, "spec")
    assert next_card["errors"] == [] and next_card["reconcile"]["ready"]
    assert {p["name"]: p["value"] for p in next_card["spec"]["parameters"]}["wall_id"] == str(wall_id)
    assert len(operations) == 1  # the second card still awaits its own confirmation
    stale_card = client.post(f"{B}/spec/confirm", json={"spec": first, "session_id": sid})
    assert stale_card.status_code in (409, 422)
    replay = client.post("/api/chat", headers=HEADERS, json={"session_id": sid, "execution": first_result})
    assert replay.status_code in (409, 422)
    second_result = execute(client, sid, next_card["spec"])
    assert second_result["success"], second_result
    model.script("supervisor", json_turn({"verdict": "done", "report": "Built the wall and placed its window."}))
    _, frames = request(client, session_id=sid, execution=second_result)
    steps = events(frames, "plan")[-1]["steps"]
    assert [s["status"] for s in steps] == ["done", "done"]
    assert steps[1]["evidence_id"] == second_result["evidence_id"] and len(operations) == 2
    assert "wall" in tokens(frames) and "window" in tokens(frames)
    supervisor = json.dumps(model.requests_of("supervisor")[-1]["messages"])
    assert "Build the wall" in supervisor and "Place the window" in supervisor
    stored = get_log_store().calls_of_session(sid)
    assert {r["step_id"] for r in stored} >= {"wall", "window"}
    assert len({r["plan_id"] for r in stored if r["plan_id"]}) == 1


@pytest.mark.parametrize("refused,decision,expected", [
    (False, "stop", ["failed", "skipped"]),
    (True, "stop", ["failed", "skipped"]),
    (True, "continue", ["skipped", "pending"]),
    (True, "revise", ["pending", "pending"]),
])
def test_failed_first_step_pauses_for_a_designer_decision(client, model, revit, refused, decision, expected):
    sid, spec = start(client, model)
    original = revit.handler
    def fail_wall(req):
        if "Wall.Create(document" in req.get("params", {}).get("code", ""):
            reason = "System.InvalidOperationException: REFUSED: wall unavailable" if refused else "wall unavailable"
            return FakeRevit.code_result(req["id"], None, success=False, error=reason)
        return original(req)
    revit.handler = fail_wall
    result = execute(client, sid, spec)
    assert not result["success"] and bool(result.get("refused")) == refused and result["evidence_id"]
    model.script("supervisor", json_turn({"verdict": "ask", "report": "Wall unavailable.", "question": "What next?"}))
    _, frames = request(client, session_id=sid, execution=result)
    assert [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["failed", "pending"]
    form, = events(frames, "questions")
    options = form["questions"][0]["options"]
    assert len(options) >= 3 and any(option["value"] == "stop" for option in options)
    assert not events(frames, "spec")
    question, = form["questions"]
    choice = next(option for option in options if option["value"] == decision)
    if decision != "stop":
        model.script("planner", text_turn("What change would you like?"))
    _, next_reply = request(client, session_id=sid, message=choice["label"], answers={
        "ask_id": form["ask_id"], "items": [{"id": question["id"], **choice}]})
    assert [s["status"] for s in events(next_reply, "plan")[-1]["steps"]] == expected
    assert not events(next_reply, "spec")


@pytest.mark.parametrize("completed", [True, False], ids=["skip-second", "cancel-both"])
def test_revision_can_close_a_two_step_plan(client, model, revit, completed):
    sid, card = start(client, model)
    if completed:
        result = execute(client, sid, card)
        model.script("supervisor", json_turn({"verdict": "done", "report": "Wall built."}))
        model.script("planner", text_turn("Preparing the window."))
        request(client, session_id=sid, execution=result)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS[:1] if completed else []})),
                 text_turn("Task ended."))
    _, frames = request(client, session_id=sid, message="Skip the remaining work.")
    final, = events(frames, "plan")
    assert [s["status"] for s in final["steps"]] == ["done" if completed else "skipped", "skipped"]
    assert get_session_store().get(sid).plan.closed


def test_interruption_can_revise_pending_steps_and_skip_an_omitted_step(client, model, revit):
    third = {"id": "door", "title": "Add a door", "tool": "create_door_on_wall"}
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": [*STEPS, third]})), text_turn("Plan ready."))
    sid, frames = request(client, message=BRIEF + " Then add a door.")
    created, = events(frames, "plan")
    revision = [STEPS[0], {**STEPS[1], "title": "Place the larger window"}]
    model.script("planner", tool_turn(("set_plan", {"steps": revision})), text_turn("Plan updated."))
    _, frames = request(client, session_id=sid, message="Use a larger window. Skip the door.")
    changed = events(frames, "plan")[-1]
    assert changed["plan_id"] == created["plan_id"]
    by_id = {step["id"]: step for step in changed["steps"]}
    assert by_id["door"]["status"] == "skipped" and by_id["window"]["title"] == revision[1]["title"]
    assert "Build the wall" in json.dumps(model.requests_of("analyst")[-1]["messages"])
    assert "Skip the door" in json.dumps(model.requests_of("planner")[-1]["messages"])


@pytest.mark.parametrize("wrong", ["window", None])
def test_out_of_order_or_missing_step_cannot_create_card(client, model, revit, wrong):
    args = {"spec": spec_for("create_wall", **WALL)}
    if wrong is not None:
        args["step_id"] = wrong
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})),
                 tool_turn(("propose_spec", args)), text_turn("Waiting."))
    _, frames = request(client, message=BRIEF)
    assert not events(frames, "spec")
    assert [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["pending", "pending"]
    assert "step" in str(model.tool_results("propose_spec")[0]).lower()


@pytest.mark.parametrize("steps", [STEPS[:1], [STEPS[0], STEPS[0]],
                                   [{"id": str(i), "title": f"Step {i}"} for i in range(9)]])
def test_invalid_plan_is_refused_without_a_plan_event(client, model, revit, steps):
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": steps})), text_turn("No plan."))
    _, frames = request(client, message=BRIEF)
    assert not events(frames, "plan")
    assert model.tool_results("set_plan")[0].get("error")


def test_form_answers_from_before_the_plan_survive_history_trimming(client, model, revit):
    form = {"questions": [{"id": "sill", "text": "Choose a sill height.", "options": [
        {"value": 975, "label": "975 mm"}, {"value": 1100, "label": "1100 mm"}]}]}
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("ask_designer", form)))
    brief = "Build a wall then add a window."
    sid, frames = request(client, message=brief)
    asked, = events(frames, "questions")
    answer = {"ask_id": asked["ask_id"], "items": [{"id": "sill", "value": 975, "label": "975 mm"}]}
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})), propose_wall(), text_turn("Wall card."))
    _, proposed = request(client, session_id=sid, message="975 mm", answers=answer)
    result = execute(client, sid, events(proposed, "spec")[0]["spec"])
    session = get_session_store().get(sid)
    for i in range(80):
        session.add_message("user", f"Other detail {i}")
    assert "975" not in str(session.history)
    model.script("supervisor", json_turn({"verdict": "done", "report": "Wall created."}))
    model.script("planner", text_turn("Using the requested sill height."))
    _, frames = request(client, session_id=sid, execution=result)
    sent = json.dumps(model.requests_of("planner")[-1]["messages"], ensure_ascii=False)
    assert brief in sent and "975" in sent and "sill" in sent and answer["ask_id"] in sent
    assert not events(frames, "questions")


def test_plan_card_allows_the_designer_to_confirm_its_original_interpretation(client, model, revit):
    spec = spec_for("create_wall", **WALL)
    interpretation = {"param": "height", "text": "Measure height from the base level.", "confirmed": False}
    spec["interpretations"] = [interpretation]
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})),
                 tool_turn(("propose_spec", {"spec": spec, "step_id": "wall"})), text_turn("Review the assumption."))
    sid, frames = request(client, message=BRIEF)
    card, = events(frames, "spec")
    assert not card["reconcile"]["ready"]
    shown = card["spec"]
    def mark(**changes):
        return {**shown, "interpretations": [{**shown["interpretations"][0], "confirmed": True, **changes}]}
    for modified in [mark(text="Something else."), mark(param="end_x"), {**shown, "interpretations": []}]:
        response = client.post(f"{B}/spec/confirm", json={"session_id": sid, "spec": modified})
        assert response.status_code in (409, 422)
    reviewed = mark()
    check = client.post(f"{B}/spec/reconcile", json={"session_id": sid, "spec": reviewed})
    assert check.json()["ready"]
    confirm(client, sid, reviewed)


@pytest.mark.parametrize("revision", [[], STEPS[:1], [{**STEPS[0], "status": "done"}, STEPS[1]]])
def test_invalid_revision_keeps_the_existing_plan_and_card(client, model, revit, revision):
    sid, shown = start(client, model)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": revision})), text_turn("Keep the plan."))
    _, frames = request(client, session_id=sid, message="Reconsider the plan.")
    assert not events(frames, "plan") and model.tool_results("set_plan")[-1].get("error")
    confirm(client, sid, shown)


def test_single_model_reviews_and_continues_on_its_configured_target(make_client, model, revit):
    model.script("single", tool_turn(("set_plan", {"steps": STEPS})), propose_wall(), text_turn("Wall card."))
    with make_client() as tc:
        sid, frames = request(tc, message=BRIEF)
        assert events(frames, "stage")[0]["mode"] == "single"
        result = execute(tc, sid, events(frames, "spec")[0]["spec"])
        # The first attempt has no scripted supervisor response. The same result can be
        # resubmitted after that transport error, without confirming or executing again.
        interrupted = tc.post("/api/chat", headers=HEADERS, json={"session_id": sid, "execution": result})
        assert events(sse(interrupted.text), "error")
        model.script("supervisor", json_turn({"verdict": "done", "report": "Wall created."}))
        model.script("planner", text_turn("I have the wall id to place the window."))
        _, resumed = request(tc, session_id=sid, execution=result)
        assert [s["status"] for s in events(resumed, "plan")[-1]["steps"]] == ["done", "pending"]
        assert {event["role"] for event in events(resumed, "stage")} == {"supervisor", "planner"}
        assert len({event["model"] for event in events(resumed, "stage")}) == 1


def test_an_open_task_keeps_the_request_before_a_plain_text_clarification(client, model, revit):
    brief = "Create a wall and a window along it."
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("What sill height should I use?"))
    sid, _ = request(client, message=brief)
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})), propose_wall(), text_turn("Wall card."))
    _, frames = request(client, session_id=sid, message="Use 925 mm.")
    result = execute(client, sid, events(frames, "spec")[0]["spec"])
    session = get_session_store().get(sid)
    for i in range(80):
        session.add_message("user", f"Other detail {i}")
    model.script("supervisor", json_turn({"verdict": "done", "report": "Wall done."}))
    model.script("planner", text_turn("Continuing with the window."))
    request(client, session_id=sid, execution=result)
    sent = json.dumps(model.requests_of("planner")[-1]["messages"])
    assert brief in sent and "925" in sent


def test_revision_cannot_rewrite_or_remove_a_completed_step(client, model, revit):
    third = {"id": "door", "title": "Add a door"}
    sid, wall = start(client, model)
    result = execute(client, sid, wall)
    model.script("supervisor", json_turn({"verdict": "done", "report": "Wall done."}))
    model.script("planner", tool_turn(("set_plan", {"steps": [STEPS[1], third]})),
                 tool_turn(("set_plan", {"steps": [{**STEPS[0], "title": "Changed"}, STEPS[1]]})),
                 text_turn("Keep the original wall."))
    _, frames = request(client, session_id=sid, execution=result)
    assert [step["status"] for step in events(frames, "plan")[-1]["steps"]] == ["done", "pending"]
    assert all(r.get("error") for r in model.tool_results("set_plan")[-2:])


def test_plan_confirm_covers_one_dispatch_even_with_concurrent_clicks(client, model, revit, monkeypatch):
    from backend.api import bridge
    sid, shown = start(client, model)
    token = confirm(client, sid, shown)
    url = f"{B}/tools/create_wall/run"
    body = {"token": token, "params": WALL}
    wrong = client.post(url, json={**body, "params": {**WALL, "height": 2422}})
    assert wrong.json()["error"] == "confirmation_invalid"
    entered, release = threading.Event(), threading.Event()
    original = bridge.run_pack
    calls = []
    async def blocked(**kwargs):
        calls.append(kwargs["name"])
        entered.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await original(**kwargs)
    monkeypatch.setattr(bridge, "run_pack", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(client.post, url, json=body)
        try:
            assert entered.wait(10)
            repeat = client.post(url, json=body)
            assert repeat.json()["error"] == "confirmation_invalid"
        finally:
            release.set()
        assert running.result(timeout=10).json()["success"]
    assert calls == ["create_wall"]


def test_plan_step_reports_a_disconnected_device_and_retires_its_token(client, model, revit, monkeypatch):
    from backend.api import bridge
    from backend.api.errors import ApiError
    sid, shown = start(client, model)
    token = confirm(client, sid, shown)
    url, body = f"{B}/tools/create_wall/run", {"token": token, "params": WALL}
    async def offline():
        raise ApiError(503, "revit_unreachable", "Device went offline.")
    with monkeypatch.context() as patch:
        patch.setattr(bridge, "get_revit_client", offline)
        attempt = client.post(url, json=body)
    assert attempt.status_code == 200 and attempt.json()["error"] == "revit_unreachable"
    model.script("supervisor", json_turn({"verdict": "ask", "report": "Device offline.", "question": "Continue?"}))
    _, frames = request(client, session_id=sid, execution=attempt.json())
    assert events(frames, "plan")[-1]["steps"][0]["status"] == "failed"
    assert events(frames, "questions")
    assert client.post(url, json=body).json()["error"] == "confirmation_invalid"


def test_closing_the_report_after_its_first_frame_releases_the_session(client, model, revit):
    from starlette.requests import Request
    from backend.api import chat
    sid, wall = start(client, model)
    result = execute(client, sid, wall)
    session = get_session_store().get(sid)

    async def disconnect():
        incoming = Request({"type": "http", "path": "/api/chat", "method": "POST",
                            "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()]})
        response = await chat.chat(chat.ChatRequest(session_id=sid, execution=result), incoming, "plan-test")
        stream = response.body_iterator
        try:
            assert "event: execution" in await anext(stream)
        finally:
            await stream.aclose()
        assert not session.plan.reporting and not session.bridge_turn.locked()

    asyncio.run(disconnect())
    model.script("supervisor", json_turn({"verdict": "done", "report": "Wall built."}))
    model.script("planner", text_turn("Preparing the window."))
    _, frames = request(client, session_id=sid, execution=result)
    assert events(frames, "plan")[-1]["steps"][0]["status"] == "done"


def test_two_retries_on_the_first_step_leave_the_next_step_its_own_allowance(client, model, revit):
    second = {"id": "another", "title": "Build the other wall", "tool": "create_wall"}
    outcomes = iter([False, False, True, False])
    original = revit.handler
    def model_wall(req):
        if "Wall.Create(document" in req.get("params", {}).get("code", "") and not next(outcomes):
            return FakeRevit.code_result(req["id"], None, False, "Wall creation failed.")
        return original(req)
    revit.handler = model_wall
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": [STEPS[0], second]})), propose_wall(), text_turn("Review."))
    sid, frames = request(client, message="Build two different walls.")
    # On the first retry the planner chooses a different wall pack. Each pack's
    # independent health gate still applies to the actual confirmation and run.
    by_points = tool_turn(("propose_spec", {"step_id": "wall", "spec": spec_for(
        "create_walls_by_points", level_name="L1", points="0,0;5000,0", closed=False)}))
    for success, next_id, proposal in [(False, "wall", by_points), (False, "wall", propose_wall()),
                                       (True, "another", propose_wall("another")), (False, "another", propose_wall("another"))]:
        result = execute(client, sid, events(frames, "spec")[0]["spec"])
        assert result["success"] is success, result
        verdict = {"verdict": "done", "report": "Wall built."} if success else {
            "verdict": "retry", "report": "Wall unavailable.", "fix": "Review and retry."}
        model.script("supervisor", json_turn(verdict))
        model.script("planner", proposal, text_turn("Review this card."))
        _, frames = request(client, session_id=sid, execution=result)
        assert not events(frames, "questions")
        steps = events(frames, "plan")[-1]["steps"]
        assert next(s["status"] for s in steps if s["id"] == next_id) == "proposed"
