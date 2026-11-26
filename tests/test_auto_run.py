"""Exercise the request switch, form answers and dispatch with a fake add-in and model."""
import re

import pytest

from backend.config import get_settings
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_host_loop import HEADERS, events, sse
from tests.test_multi_step import request
from tests.test_mentions import picker
from tests.test_roles import DONE, ROUTING_TOOL, model  # noqa: F401
from fastapi.testclient import TestClient


@pytest.fixture
def client(local_env):
    from backend.main import create_app
    local_env.setenv('ALLOW_BROWSER_KEYS', 'on')  # these tests supply the scripted model via headers
    local_env.delenv('LLM_SINGLE_MODEL')
    local_env.setenv('ROLES_ANALYST', 'on')
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 45000)) as tc:
        yield tc

BRIEF = "Build two walls with " + ", ".join(f"{k}={v}" for k, v in WALL.items())


def select_turn(tool="select_elements", **params):
    draft = spec_for(tool, **params)
    for binding in draft["parameters"]:
        if binding["name"] == "category":
            binding.update(source="designer", evidence=binding["value"])
        else:
            binding.update(source="tool", evidence="I chose these")
    return tool_turn(("propose_spec", {"spec": draft}))


def rotate_turn(**params):
    draft = spec_for("rotate_elements", angle=90, **params)
    draft["parameters"][0].update(unit=None, source="designer", evidence="angle=90")
    for binding in draft["parameters"][1:]:
        binding.update(source="designer", evidence=f"element_ids={binding['value']}")
    return tool_turn(("propose_spec", {"spec": draft}))


@pytest.mark.parametrize("ids", [{}, {"element_ids": ""}, {"element_ids": None}])
def test_assistant_selected_ids_require_a_click_until_new_request(client, model, revit, ids):
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", select_turn(element_ids="93,94"), rotate_turn(**ids))
    model.script("supervisor", json_turn(DONE))
    sid, frames = request(client, run="auto", message="Rotate them, angle=90")
    assert [r["read"]["name"] for r in events(frames, "tool") if "read" in r] == ["select_elements"]
    card, = events(frames, "spec")
    assert card.get("run_reason") == "unverified_evidence" and card["run"] == "confirm"
    assert card["assistant_selection"] == {"tool": "select_elements", "count": 2, "ids": ["93", "94"]}
    assert not events(frames, "execution")
    assert not any("RotateElements" in r.get("params", {}).get("code", "") for r in revit.requests)
    model.script("planner", rotate_turn())
    _, frames = request(client, session_id=sid, run="auto", message="Rotate the current selection, angle=90")
    assert events(frames, "execution")[0]["success"] and "assistant_selection" not in events(frames, "spec")[0]


@pytest.mark.parametrize("verified", [False, True])
def test_explicit_rotation_targets_use_their_own_evidence(client, model, revit, verified):
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", select_turn(element_ids="93,94"), rotate_turn(element_ids="93,94"))
    model.script("supervisor", json_turn(DONE))
    words = "Rotate them, angle=90" + (", element_ids=93,94" if verified else "")
    _, frames = request(client, run="auto", message=words)
    card, = events(frames, "spec")
    assert "assistant_selection" not in card
    assert card.get("run_reason") == (None if verified else "unverified_evidence")
    assert bool(events(frames, "execution")) == verified


@pytest.mark.parametrize("mode,tool,params,reply,display_ids", [
    ("confirm", "select_by_filter", {"category": "OST_Walls"}, {"Count": 31, "ElementIds": list(range(1, 32))},
     [str(i) for i in range(1, 21)]),
    ("auto", "select_in_box", {"element_ids": "71,72"}, {"Count": 5}, []),
    ("auto", "select_elements", {"element_ids": "71,72", "mode": "remove"}, {"Count": 5}, []),
    ("confirm", "clear_selection", {}, {"Count": 0}, []),
])
def test_delete_card_discloses_assistant_selection(client, model, revit, mode, tool, params, reply, display_ids):
    original = revit.handler
    def handler(req):
        if "SetElementIds" in req.get("params", {}).get("code", ""):
            return FakeRevit.code_result(req["id"], reply)
        return original(req)
    revit.handler = handler
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", select_turn(tool, **params), select_turn("delete_selected"), text_turn("Review the card."))
    _, frames = request(client, run=mode, message="Remove selected elements")
    assert [r["read"]["name"] for r in events(frames, "tool") if "read" in r] == [tool], model.tool_results("propose_spec")[0]
    card, = events(frames, "spec")
    assert card["assistant_selection"] == {"tool": tool, "count": reply["Count"], "ids": display_ids}
    if mode == "auto":
        assert card["run_reason"] == "excluded_tool"
    assert not events(frames, "execution")


@pytest.mark.parametrize("designer_pick", ["watch", "mention", "plain answer"])
def test_selection_provenance_during_clarification(client, model, picker, designer_pick):
    from backend.session import get_session_store
    from tests.test_selection_capture import WATCH, WATCH_ARGS, select_one
    select_one(picker)
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", select_turn(element_ids="93,94"), text_turn("What angle?"), rotate_turn())
    sid, _ = request(client, run="auto", message="Rotate them")
    session = get_session_store().get(sid)
    assert session.assistant_selections["local"]["tool"] == "select_elements"
    extra = {}
    if designer_pick == "watch":
        picked = client.post(WATCH, json={**WATCH_ARGS, "session_id": sid}).json()
        assert picked["items"][0]["id"] == 93
        assert "local" not in session.assistant_selections
    elif designer_pick == "mention":
        extra["mentions"] = [{"kind": "element", "id": 93, "document_id": "fake-document"}]
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, session_id=sid, run="auto", message="angle=90", **extra)
    card, = events(frames, "spec")
    assert ("assistant_selection" in card) == (designer_pick == "plain answer")
    assert bool(events(frames, "execution")) == (designer_pick != "plain answer")


@pytest.mark.parametrize("words,quote,value", [("start_x=125", "25", 25), ("start_x=-25", "25", 25),
                                               ("start_x=1.5", "1", 1), ("height=100 mm", "100 mm", 100),
                                               ("wall height is 100 mm", "100 mm", 100), ("height: 100", "100", 100)])
def test_original_speech_must_prove_the_parameter(words, quote, value):
    from types import SimpleNamespace
    from backend.api.auto_run import _supported
    binding = SimpleNamespace(name="start_x", evidence=quote, value=value)
    assert not _supported(binding, None, words, [])
    assert not _supported(SimpleNamespace(name="start_x", evidence=quote, value=str(value)), None, words, [], "double")
    assert not _supported(binding, None, "", [{"answers": [{"id": "height", "value": value}]}])


def wall(step=None, **changes):
    draft = spec_for("create_wall", **WALL)
    for p in draft["parameters"]:
        p["evidence"] = f"{p['name']}={p['value']}"
    draft.update(changes)
    return tool_turn(("propose_spec", {"spec": draft, **({"step_id": step} if step else {})}))


def two(model, *, failure=False):
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": [{"id": k, "title": k, "tool": "create_wall"}
                                                               for k in ("first", "next")]})),
                 wall("first"), wall("first" if failure else "next"))
    model.script("supervisor", json_turn({"verdict": "retry", "report": "Retry this step", "fix": "Check it"}
                                         if failure else DONE), json_turn(DONE))


def simulated_wall_counter(revit, fail=False):
    original = revit.handler
    writes = []
    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
            return FakeRevit.code_result(req["id"], 2 + len(writes))
        if "Wall.Create" in code:
            writes.append(1)
            if fail:
                return FakeRevit.code_result(req["id"], None, success=False, error="REFUSED: fixture")
        return original(req)
    revit.handler = handler
    return writes


def test_two_steps_are_run_and_recorded_without_clicking(client, model, revit):
    writes = simulated_wall_counter(revit)
    two(model)
    _, frames = request(client, run="auto", message=BRIEF)
    assert [c["run"] for c in events(frames, "spec")] == ["auto", "auto"], frames
    assert [r["success"] for r in events(frames, "execution")] == [True, True]
    assert len(writes) == 2 and all(s["status"] == "done" for s in events(frames, "plan")[-1]["steps"])
    assert client.get(f"{B}/evidence").text.count("designer:auto-run") == 2
    assert client.get("/api/usage/runs").json()["items"][0]["run"] == "auto"


def test_capture_context_keeps_its_prefix_across_auto_steps(client, model, revit, env, tmp_path):
    from backend.session import get_session_store
    from tests.test_selection_capture import PNG, configure_vision
    configure_vision(env, tmp_path)
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "Hello."}))
    sid = client.post("/api/chat", json={"message": "Hello"}).headers["X-Session-Id"]
    simulated_wall_counter(revit)
    two(model)
    get_session_store().get(sid).captures.add("local", {"width": 1, "height": 1, "view_name": "Plan", "png_base64": PNG})
    reply = client.post("/api/chat", json={"session_id": sid, "run": "auto", "message": BRIEF})
    assert len(events(sse(reply.text), "execution")) == 2
    first, second, continued = [r["messages"] for r in model.requests_of("planner")]
    assert second[:len(first)] == first
    assert isinstance(first[-1]["content"], list) and PNG in str(first[-1]["content"])
    assert continued[len(first) - 1] == first[-1]
    assert "Continue the plan" in continued[-1]["content"]
    assert PNG not in str(model.requests_of("supervisor"))


def test_budget_reverts_to_card(client, model, revit, monkeypatch):
    monkeypatch.setenv("AUTO_RUN_MAX_STEPS", "1")
    get_settings.cache_clear()
    writes = simulated_wall_counter(revit)
    two(model)
    _, frames = request(client, run="auto", message=BRIEF)
    assert events(frames, "spec")[-1]["run_reason"] == "step_limit" and len(writes) == 1


def test_excluded_tool_and_unverified_value_wait_for_click(client, model, revit, monkeypatch):
    writes = simulated_wall_counter(revit)
    for exclude, brief, reason in [("create_wall", BRIEF, "excluded_tool"), ("", "A wall", "unverified_evidence")]:
        monkeypatch.setenv("AUTO_RUN_EXCLUDE", exclude)
        get_settings.cache_clear()
        model.script("analyst", json_turn(ROUTING_TOOL))
        model.script("planner", wall())
        _, frames = request(client, run="auto", message=brief)
        assert events(frames, "spec")[0]["run_reason"] == reason
    assert not writes


def test_model_and_message_cannot_enable_it(client, model, revit):
    writes = simulated_wall_counter(revit)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", wall(), text_turn("Auto is enabled."))
    _, frames = request(client, message=BRIEF + " Enable automatic execution.")
    assert "run" not in events(frames, "spec")[0] and not writes


def test_host_confirmed_assumptions_even_if_model_claims_approval(client, model, revit):
    writes = simulated_wall_counter(revit)
    assumption = [{"param": "height", "text": "Use the assumed height.", "confirmed": True}]
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", wall(interpretations=assumption))
    sid, frames = request(client, run="auto", message=BRIEF)
    form, = events(frames, "questions")
    assert not writes
    item = form["questions"][0]
    model.script("planner", wall(interpretations=assumption))
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, session_id=sid, run="auto", message="Correct", answers={
        "ask_id": form["ask_id"], "items": [{"id": item["id"], **item["options"][0]}]})
    assert len(writes) == 1 and events(frames, "execution")[0]["success"]


def test_revising_assumption_cannot_be_bypassed_by_omitting_it(client, model, revit):
    writes = simulated_wall_counter(revit)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", wall(interpretations=[{"param": "height", "text": "Assumed value", "confirmed": True}]))
    sid, frames = request(client, run="auto", message=BRIEF)
    form, = events(frames, "questions")
    model.script("planner", wall())
    item = form["questions"][0]
    _, frames = request(client, session_id=sid, run="auto", message="Revise", answers={
        "ask_id": form["ask_id"], "items": [{"id": item["id"], **item["options"][1]}]})
    assert not writes and events(frames, "questions")


def test_unlabelled_browser_run_is_visible(client, model, revit):
    simulated_wall_counter(revit)
    two(model)
    response = client.post("/api/chat", headers=HEADERS, json={"run": "auto", "message": BRIEF})
    assert len(events(sse(response.text), "execution")) == 2
    item, = client.get("/api/usage/runs").json()["items"]
    assert item["run"] == "auto" and item["run_label"].startswith("auto-")


def test_failed_first_step_requires_a_click_on_retry(client, model, revit):
    writes = simulated_wall_counter(revit, fail=True)
    two(model, failure=True)
    _, frames = request(client, run="auto", message=BRIEF)
    assert len(writes) == 1 and events(frames, "execution")[0]["success"] is False
    assert events(frames, "spec")[-1]["run_reason"] == "previous_failure"


def test_cloud_run_switch_requires_admin(client, model, revit, monkeypatch):
    from tests.conftest import FIXTURE_ADMIN_PASSWORD
    monkeypatch.setenv("DEPLOY_MODE", "cloud")
    monkeypatch.setenv("ADMIN_PASSWORD", FIXTURE_ADMIN_PASSWORD)
    monkeypatch.setenv("AUTO_RUN", "on")
    get_settings.cache_clear()
    writes = simulated_wall_counter(revit)
    for admin in (False, True):
        headers = {**HEADERS, **({"X-Admin-Token": FIXTURE_ADMIN_PASSWORD} if admin else {})}
        modes = client.get("/config.json", headers=headers).json()["features"]["runModes"]
        assert ("auto" in modes) == admin
        model.script("analyst", json_turn(ROUTING_TOOL))
        model.script("planner", wall(), text_turn("Wall card.") if not admin else text_turn("Done."))
        model.script("supervisor", json_turn(DONE))
        frames = sse(client.post("/api/chat", headers=headers, json={"message": BRIEF, "run": "auto"}).text)
        assert bool(events(frames, "execution")) == admin
    assert len(writes) == 1


@pytest.mark.parametrize("planned", [False, True], ids=["single-step", "plan"])
def test_disconnect_finishes_only_the_started_step(client, model, revit, monkeypatch, planned):
    import asyncio
    from starlette.requests import Request
    from backend.api import bridge, chat
    writes = simulated_wall_counter(revit)
    if planned:
        two(model)
    else:
        model.script("analyst", json_turn(ROUTING_TOOL))
        model.script("planner", wall())
        model.script("supervisor", json_turn(DONE))
    execute = bridge.execute_pack

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            started.set()
            await release.wait()
            return await execute(*args)
        monkeypatch.setattr(bridge, "execute_pack", delayed)
        async def receive():
            await asyncio.Event().wait()
        incoming = Request({"type": "http", "method": "POST", "path": "/api/chat", "client": ("127.0.0.1", 45000),
                            "headers": [(k.lower().encode(), v.encode()) for k, v in HEADERS.items()]}, receive)
        response = await chat.chat(chat.ChatRequest(message=BRIEF, run="auto"), incoming, None)
        stream = response.body_iterator
        async def consume():
            async for _ in stream:
                pass
        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 10)
        session = chat.get_session_store().get(response.headers["X-Session-Id"])
        worker = session.auto_worker
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        release.set()
        assert (await asyncio.wait_for(worker, 10))["success"]
        await stream.aclose()
        return session.session_id
    sid = asyncio.run(scenario())
    model.script("planner", text_turn("Review the next card."))
    _, resumed = request(client, session_id=sid, message=BRIEF, run="auto")
    receipt, = events(resumed, "execution")
    assert receipt["success"] and len(model.requests_of("supervisor")) == 1
    if planned:
        assert "run" not in events(resumed, "spec")[-1]
    else:
        assert not events(resumed, "spec")
        from backend.session import get_session_store
        assert get_session_store().get(sid).pending_auto_receipt is None
        replay = client.post("/api/chat", headers=HEADERS, json={"session_id": sid, "execution": receipt})
        assert replay.status_code == 422 and replay.json()["error"] == "invalid_step"
    assert len(writes) == 1 and client.get(f"{B}/evidence").text.count("designer:auto-run") == 1


@pytest.mark.parametrize("source", ["tool", "preference", "answer"])
@pytest.mark.parametrize("height,proven", [(9000, False), (3000, True)])
def test_height_requires_provenance_regardless_of_source(client, model, revit, source, height, proven):
    writes = simulated_wall_counter(revit)
    proposal = wall()
    binding = proposal["tool_calls"][0]["arguments"]["spec"]["parameters"][-1]
    binding.update(value=height, source=source, evidence="preference:estimate" if source == "preference" else "planner estimate")
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", proposal)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, message=BRIEF.split(", height=")[0], run="auto")
    card, = events(frames, "spec")
    assert bool(writes) == proven
    assert card.get("run_reason") == (None if proven else "unverified_evidence")


@pytest.mark.parametrize("words,height,ok", [("墙高3100", 3100, True), ("3100mm", 3100, True),
                                             ("高度 3 米", 3000, False), ("高度 3 米", 3, False),
                                             ("宽度3100mm", 3100, False), ("窗台高度3100", 3100, False),
                                             ("高度 3 千米", 3, False)])
def test_spoken_dimensions_match_the_declared_unit(words, height, ok):
    from types import SimpleNamespace
    from backend.api.auto_run import _supported
    binding = SimpleNamespace(name="height", value=height, evidence=words)
    assert _supported(binding, None, words, [], "double", "mm") is ok


@pytest.mark.parametrize("label,ok", [("3100 mm", True), ("310 mm", False), ("3100 m", False)])
def test_form_value_must_match_the_visible_option(client, model, revit, label, ok):
    writes = simulated_wall_counter(revit)
    form = {"questions": [{"id": "height", "text": "Wall height?", "options": [
        {"label": label, "value": 3100}, {"label": "4000 mm", "value": 4000}]}]}
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("ask_designer", form)))
    sid, first = request(client, message=BRIEF.split(", height=")[0], run="auto")
    question, = events(first, "questions")
    proposal = wall()
    proposal["tool_calls"][0]["arguments"]["spec"]["parameters"][-1].update(value=3100, evidence="form answer")
    model.script("planner", proposal)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, session_id=sid, run="auto", message=label, answers={
        "ask_id": question["ask_id"], "items": [{"id": "height", "label": label, "value": 3100}]})
    assert bool(writes) == ok
    assert events(frames, "spec")[0].get("run_reason") == (None if ok else "unverified_evidence")


@pytest.mark.parametrize("configured", ["", "copy_elements,change_type"])
def test_exclude_adds_to_permanent_deletion_tools(local_env, configured):
    local_env.setenv("AUTO_RUN_EXCLUDE", configured)
    settings = get_settings()
    assert set(settings.auto_run_exclude) == {"delete_elements", "delete_selected", "delete_elements_by_category",
                                              *filter(None, configured.split(","))}


@pytest.mark.parametrize("name,value", [("AUTO_RUN_EXCLUDE", "missing_review_tool"),
                                         ("AUTO_RUN_MAX_STEPS", "0"), ("AUTO_RUN_MAX_STEPS", "21")])
def test_invalid_auto_run_settings_fail_at_startup(local_env, name, value):
    from backend.config import ConfigError
    from backend.main import create_app
    local_env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        create_app()


def cite_result(proposal, binding, path, *, evidence_id=None):
    """A scripted planner uses the evidence ID from the host's actual reply."""
    def reply(body):
        ident = evidence_id
        if ident is None:
            context = "\n".join(m.get("content") or "" for m in body["messages"])
            ident = re.findall(r'"evidence_id":\s*"([^"]+)"', context)[-1]
        binding["evidence"] = f"result:{ident}#{path}"
        return proposal
    return reply


@pytest.mark.parametrize("wall_id,ok", [("4242", True), ("4243", False)])
def test_window_uses_only_the_created_wall_id(client, model, revit, monkeypatch, wall_id, ok):
    from tests.fake_revit import SNAPSHOT_RESULT
    from tests.test_multi_step import STEPS
    monkeypatch.setitem(SNAPSHOT_RESULT["CategoryNames"], "OST_Windows", "Windows")
    original = revit.handler
    writes = []
    def handle(req):
        code = req.get("params", {}).get("code", "")
        if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
            return FakeRevit.code_result(req["id"], len(writes) + 1)
        if "Wall.Create(document" in code or "document.Create.NewFamilyInstance(" in code:
            writes.append(code)
        return original(req)
    revit.handler = handle
    window = spec_for("create_window_on_wall", wall_id=wall_id, type_name="OST_Windows-TypeA", offset_along_wall=1500)
    host_wall = window["parameters"][0]
    host_wall.update(source="tool")
    for p in window["parameters"][1:]:
        p["evidence"] = f"{p['name']}={p['value']}"
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": STEPS})), wall("wall"),
                 cite_result(tool_turn(("propose_spec", {"step_id": "window", "spec": window})), host_wall, "ElementId"))
    model.script("supervisor", json_turn(DONE), json_turn(DONE))
    _, frames = request(client, run="auto", message=BRIEF + ", type_name=OST_Windows-TypeA, offset_along_wall=1500")
    first = events(frames, "execution")[0]
    assert first["result"]["ElementId"] == 4242
    assert host_wall["evidence"] == f"result:{first['evidence_id']}#ElementId"
    assert "result:<evidence_id>#<path>" in model.requests_of("planner")[0]["messages"][0]["content"]
    assert len(writes) == (2 if ok else 1)
    assert events(frames, "spec")[-1].get("run_reason") == (None if ok else "unverified_evidence")
    if ok:
        assert [s["status"] for s in events(frames, "plan")[-1]["steps"]] == ["done", "done"]


@pytest.mark.parametrize("same_message,path,value,evidence_id,source,ok", [
    (True, "Geometry.0.Midpoint", 1750, None, "tool", True),
    (True, "Geometry.0.Midpoint", 1751, None, "tool", False),
    (True, "Geometry.1.Midpoint", 1750, None, "tool", False),
    (True, "Geometry.0.NoSuchField", 1750, None, "tool", False),
    (True, "Geometry.0.Flag", 1, None, "tool", False),
    (True, "Geometry.0.Midpoint", 1750, "ev_unknown", "tool", False),
    (True, "Geometry.0.Midpoint", 1750, None, "designer", False),
    (True, "Geometry.0.Midpoint", 1750, None, "answer", False),
    (False, "Geometry.0.Midpoint", 1750, None, "tool", False),
])
def test_direct_read_evidence_expires_at_the_next_message(client, model, revit, same_message, path, value,
                                                         evidence_id, source, ok):
    from tests.test_direct_reads import read_call
    writes = simulated_wall_counter(revit)
    original = revit.handler
    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "LocationCurve" in code and "Wall.Create" not in code:
            return FakeRevit.code_result(req["id"], {"Geometry": [{"Midpoint": 1750.0, "Flag": True}]})
        return original(req)
    revit.handler = handler
    proposal = wall()
    height = next(p for p in proposal["tool_calls"][0]["arguments"]["spec"]["parameters"] if p["name"] == "height")
    height.update(value=value, source=source)
    sid = None
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(read_call()))
    if not same_message:
        model.script("planner", text_turn("Read complete."))
        sid, read_frames = request(client, run="auto", message="Read element 72")
        evidence_id, = [e["read"]["result"]["evidence_id"] for e in events(read_frames, "tool") if "read" in e]
        model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", cite_result(proposal, height, path, evidence_id=evidence_id))
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, session_id=sid, message=BRIEF.replace("height=3000", "use its midpoint for height"), run="auto")
    assert events(frames, "spec")[0].get("run_reason") == (None if ok else "unverified_evidence")
    assert bool(writes) == ok


@pytest.mark.parametrize("with_read", [True, False])
def test_reading_element_72_does_not_supply_guessed_wall_coordinates(client, model, revit, with_read):
    from tests.test_direct_reads import read_call
    writes = simulated_wall_counter(revit)
    original = revit.handler
    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "LocationCurve" in code and "Wall.Create" not in code:
            return FakeRevit.code_result(req["id"], {"LengthMm": 5000.0, "Y": 0.0, "Z": 0.0})
        return original(req)
    revit.handler = handler
    proposal = wall()
    for p in proposal["tool_calls"][0]["arguments"]["spec"]["parameters"]:
        if p["name"] == "level_name":
            p["evidence"] = "L1"
        elif p["name"] != "height":
            p.update(source="designer", evidence="coordinates")
    model.script("analyst", json_turn(ROUTING_TOOL))
    if with_read:
        model.script("planner", tool_turn(read_call()))
    model.script("planner", proposal)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, run="auto", message="看一下元素 72，在 L1 上建一面墙")
    assert sum("read" in e for e in events(frames, "tool")) == int(with_read)
    assert not writes and not events(frames, "execution")
    assert events(frames, "spec")[0]["run_reason"] == "unverified_evidence"


@pytest.mark.parametrize("failure", ["error", "refusal", "validator"])
def test_closing_failed_plan_waits_for_click_until_new_request(client, model, revit, failure):
    writes = []
    original = revit.handler
    def handler(req):
        code = req.get("params", {}).get("code", "")
        if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
            return FakeRevit.code_result(req["id"], 1)
        if "Wall.Create" in code:
            writes.append(code)
            if failure != "validator":
                reason = "System.InvalidOperationException: REFUSED: fixture" if failure == "refusal" else "wall failed"
                return FakeRevit.code_result(req["id"], None, success=False, error=reason)
        return original(req)
    revit.handler = handler
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": [{"id": "a", "title": "First wall"},
                                                              {"id": "b", "title": "Second wall"}]})), wall("a"))
    model.script("supervisor", json_turn({"verdict": "ask", "report": "The wall failed.", "question": "Retry?"}))
    sid, failed = request(client, run="auto", message=BRIEF)
    receipt, = events(failed, "execution")
    assert receipt["success"] is False and bool(receipt.get("refused")) == (failure == "refusal")
    if failure == "validator":
        assert receipt["validation"]["passed"] is False
    assert events(failed, "plan")[-1]["steps"][0]["status"] == "failed"
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(("set_plan", {"steps": []})), wall())
    model.script("supervisor", json_turn(DONE))
    _, retry = request(client, session_id=sid, run="auto", message="Try again.")
    assert [s["status"] for s in events(retry, "plan")[-1]["steps"]] == ["skipped", "skipped"]
    assert len(writes) == 1 and not events(retry, "execution")
    assert events(retry, "spec")[0]["run_reason"] == "previous_failure"

    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", wall())
    _, fresh = request(client, session_id=sid, run="auto", message=BRIEF)
    assert events(fresh, "spec")[0]["run"] == "auto" and len(writes) == 2


def test_named_result_is_scoped_to_the_session_and_device(local_env, monkeypatch):
    from backend.api import bridge
    from backend.api.auto_run import AutoRun
    from backend.session import Session

    owner, visitor = Session("first"), Session("second")
    owner.read_results["device-A", "ev_a"] = {"measured": 3125}
    draft = wall()["tool_calls"][0]["arguments"]["spec"]
    draft["parameters"][-1].update(value=3125, source="tool", evidence="result:ev_a#measured")
    event = {"spec": draft, "reconcile": {"ready": True}, "errors": []}
    for session, device, expected in [(owner, "device-A", None), (owner, "device-B", "unverified_evidence"),
                                      (visitor, "device-A", "unverified_evidence")]:
        monkeypatch.setattr(bridge, "current_scope", lambda: device)
        gate = AutoRun(session, None, get_settings(), False, BRIEF, None)
        assert gate.inspect(event) == (expected, None)


@pytest.mark.parametrize("selected", [True, False])
def test_only_verified_level_pick_supports_the_wall(client, model, revit, selected):
    from tests.test_mentions import LEVEL_ROWS, picker_handler
    revit.handler = picker_handler({"levels": LEVEL_ROWS, "calls": []})
    writes = simulated_wall_counter(revit)
    proposal = wall()
    proposal["tool_calls"][0]["arguments"]["spec"]["parameters"][0].update(source="tool", evidence="mention:level#1")
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", proposal)
    model.script("supervisor", json_turn(DONE))
    _, frames = request(client, message=BRIEF.replace("level_name=L1", "the chosen level"),
                        mentions=[{"kind": "level", "id": 1}] if selected else [], run="auto")
    card, = events(frames, "spec")
    assert bool(writes) == selected
    if not selected:
        assert any(error["code"] == "unknown_mention" for error in card["errors"])


def test_public_confirmation_rejects_reserved_identity_in_any_case(client):
    response = client.post(f"{B}/spec/confirm", json={"spec": spec_for("create_wall", **WALL),
                                                      "confirmed_by": "  Designer:Auto-Run  "})
    assert response.status_code == 422 and response.json()["error"] == "invalid_confirmer"
