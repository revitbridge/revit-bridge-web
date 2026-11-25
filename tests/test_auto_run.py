"""Exercise the request switch, form answers and dispatch with a fake add-in and model."""
import pytest

from backend.config import get_settings
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_host_loop import HEADERS, events, sse
from tests.test_multi_step import request
from tests.test_roles import DONE, ROUTING_TOOL, model  # noqa: F401
from fastapi.testclient import TestClient


@pytest.fixture
def client(local_env):
    from backend.main import create_app
    local_env.delenv('LLM_SINGLE_MODEL')
    local_env.setenv('ROLES_ANALYST', 'on')
    get_settings.cache_clear()
    with TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 45000)) as tc:
        yield tc

BRIEF = "Build two walls with " + ", ".join(f"{k}={v}" for k, v in WALL.items())


@pytest.mark.parametrize("words,quote,value", [("start_x=125", "25", 25), ("start_x=-25", "25", 25),
                                               ("start_x=1.5", "1", 1), ("height=100 mm", "100 mm", 100),
                                               ("wall height is 100 mm", "100 mm", 100), ("height: 100", "100", 100)])
def test_original_speech_must_prove_the_parameter(words, quote, value):
    from types import SimpleNamespace
    from backend.api.auto_run import _supported
    binding = SimpleNamespace(name="start_x", evidence=quote, value=value)
    assert not _supported(binding, None, words, [])
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


@pytest.mark.parametrize("limit,reason", [(1, "step_limit"), (0, "step_limit")])
def test_budget_reverts_to_card(client, model, revit, monkeypatch, limit, reason):
    monkeypatch.setenv("AUTO_RUN_MAX_STEPS", str(limit))
    get_settings.cache_clear()
    writes = simulated_wall_counter(revit)
    two(model)
    _, frames = request(client, run="auto", message=BRIEF)
    assert events(frames, "spec")[-1]["run_reason"] == reason and len(writes) == limit


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


def test_disconnect_finishes_only_the_started_step(client, model, revit, monkeypatch):
    import asyncio
    from starlette.requests import Request
    from backend.api import bridge, chat
    writes = simulated_wall_counter(revit)
    two(model)
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
    _, resumed = request(client, session_id=sid, message="Continue", run="auto")
    assert events(resumed, "execution")[0]["success"] and "run" not in events(resumed, "spec")[-1]
    assert len(writes) == 1 and client.get(f"{B}/evidence").text.count("designer:auto-run") == 1
