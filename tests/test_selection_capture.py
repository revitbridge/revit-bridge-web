"""Designer interaction through the HTTP chat/bridge routes, a fake add-in and a fake model."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest
from fastapi import Request
from revit_bridge import list_host_functions
from revit_bridge.capabilities import ToolStore

from backend import llm
from backend.api import bridge
from backend.log_store import get_log_store
from backend.models_file import reset_models
from backend.session import get_session_store
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit
from tests.test_api_v1 import B, confirm, spec_for
from tests.test_host_loop import HEADERS, events, model, sse, tokens
from tests.test_mentions import picker
from tests.test_models_file import ONE_MODEL
from tests.test_provider_compat import app_client, call_item, keys, message_item, models_text, provider, reply
from tests.test_roles import ROUTING_QUERY, client as roles_client, model as roles_model

WATCH = f"{B}/watch-selection"
WATCH_ARGS = {"session_id": "tab", "watch_id": "watch", "mode": "single", "timeout_s": 4}
STATE = "get_selection_state_in_revit"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jCowAAAAASUVORK5CYII="


@pytest.mark.parametrize("document", ["README.md", "CHANGELOG.md"])
def test_public_capture_and_selection_contract(client, document):
    schemas = client.app.openapi()["components"]["schemas"]
    assert "capture" not in schemas["ChatRequest"]["properties"] and "CaptureAttachment" not in schemas
    guide = " ".join(Path(document).read_text(encoding="utf-8").casefold().split())
    for term in ("chatrequest.capture", "captureattachment", "two images per planner round", "full tool route",
                 "analyst", "remote mcp", "stdio mcp", "user tools", "workbench"):
        assert term in guide


def native(fake, commands):
    original = fake.handler

    def handler(req):
        name = req["method"]
        if name == "get_bridge_capabilities":
            return FakeRevit.ok(req["id"], {"protocol": "1", "methods": ["send_code_to_revit", *commands]})
        if name in commands:
            return FakeRevit.ok(req["id"], commands[name](req["params"]))
        return original(req)
    fake.handler = handler


def select_one(picker, changed=False):
    row = {"id": 93, "name": "Wall", "category": "OST_Walls", "shown": "Walls", "level": "L1"}
    picker.update(elements=[row], selection=[row])
    def state(args):
        initial = args.get("focus")
        return {"count": 0 if initial else 1, "stamp": "start" if initial else "selected",
                "document_id": "other" if changed and not initial else "fake-document"}
    native(picker["fake"], {STATE: state})


def test_watch_then_submit_verified_answer(client, model, picker):
    select_one(picker)
    question = {"prompt": "Choose the wall", "mode": "single", "category": "OST_Walls"}
    model.turns.append(tool_turn(("ask_selection", question), ("watch_selection", {"mode": "single"})))
    response = client.post("/api/chat", headers=HEADERS, json={"message": "Which wall?"})
    sid = response.headers["X-Session-Id"]
    ask, = events(sse(response.text), "ask_selection")
    assert ask["selection"] == question
    assert not picker["fake"].requests and len(model.requests) == 1
    reply = client.post(WATCH, json={**WATCH_ARGS, "session_id": sid, "category": "OST_Walls"}).json()
    item, = reply["items"]
    assert (item["kind"], item["id"], item["document_id"], reply["filtered_out"]) == ("element", 93, "fake-document", 0)
    assert len(model.requests) == 1
    body = {"session_id": sid, "message": "My selection", "selection_answer": ask["ask_id"],
            "mentions": [{k: item[k] for k in ("kind", "id", "document_id")}]}
    model.turns.append(text_turn("Wall received"))
    response = client.post("/api/chat", headers=HEADERS, json=body)
    assert events(sse(response.text), "mentions")[0]["resolved"][0]["id"] == 93
    sent = model.requests[-1]["messages"][-1]["content"]
    assert ask["ask_id"] in sent and "mention:element#93" in sent
    assert client.post("/api/chat", headers=HEADERS, json=body).status_code == 422


def test_watch_unsupported(client, picker):
    assert client.post(WATCH, json=WATCH_ARGS).json()["error"] == "unsupported_addin"


def test_answer_resumes_the_query_planner_without_rerouting(roles_client, roles_model, picker):
    select_one(picker)
    roles_model.script("analyst", json_turn(ROUTING_QUERY))
    roles_model.script("planner", tool_turn(("ask_selection", {"mode": "single", "prompt": "Choose a wall"})), text_turn("Received"))
    first = roles_client.post("/api/chat", headers=HEADERS, json={"message": "Inspect a wall"})
    ask, = events(sse(first.text), "ask_selection")
    body = {"session_id": first.headers["X-Session-Id"], "message": "Chosen", "selection_answer": ask["ask_id"],
            "mentions": [{"kind": "element", "id": 93, "document_id": "fake-document"}]}
    answer = roles_client.post("/api/chat", headers=HEADERS, json=body)
    assert answer.status_code == 200 and tokens(sse(answer.text)) == "Received"
    assert len(roles_model.requests_of("analyst")) == 1
    assert len(roles_model.requests_of("planner")) == 2
    assert all(name in roles_model.requests_of("analyst")[0]["messages"][-1]["content"]
               for name in ("open_view", "capture_view_image"))


def test_watch_terminal_states(client, picker):
    select_one(picker, changed=True)
    assert client.post(WATCH, json={**WATCH_ARGS, "timeout_s": .06}).json() == {"timed_out": True}
    assert client.post(WATCH, json=WATCH_ARGS).json() == {"document_changed": True}


@pytest.mark.parametrize("mode", ["multiple", "box"])
def test_settled_watch_applies_category(client, picker, mode):
    select_one(picker)
    page = client.post(WATCH, json={**WATCH_ARGS, "mode": mode, "category": "OST_Doors"}).json()
    assert page["filtered_out"] == 1 and page["items"] == [] and page["total"] == 0


def test_unreachable_watch(client):
    response = client.post(WATCH, json=WATCH_ARGS)
    assert response.status_code == 503 and response.json()["error"] == "revit_unreachable"


def test_duplicate_and_cancel_stop_polling(client, revit):
    polled = threading.Event()
    def unchanged(args):
        if not args.get("focus"):
            polled.set()
        return {"count": 0, "stamp": "empty", "document_id": "doc"}
    native(revit, {STATE: unchanged})
    with ThreadPoolExecutor(1) as worker:
        watching = worker.submit(client.post, WATCH, json=WATCH_ARGS)
        assert polled.wait(3)
        assert client.post(WATCH, json={**WATCH_ARGS, "watch_id": "other"}).status_code == 409
        other = client.post(WATCH, json={**WATCH_ARGS, "session_id": "other", "timeout_s": .06})
        assert other.status_code == 409 and "device" in other.json()["message"]
        client.delete(f"{WATCH}/other/watch")
        client.delete(f"{WATCH}/tab/other")
        assert not watching.done()
        assert client.delete(f"{WATCH}/tab/watch").status_code == 200
        assert watching.result(2).json() == {"cancelled": True}
    count = len(revit.requests)
    time.sleep(.45)
    assert len(revit.requests) == count
    focus = [r["params"].get("focus", False) for r in revit.requests if r["method"] == STATE]
    assert len(focus) >= 2 and focus == [True, *([False] * (len(focus) - 1))]


def test_rate_limited_watch_never_polls(make_client, env, revit):
    env.setenv("CHAT_RATE_LIMIT", "1")
    with make_client() as client:
        assert client.post(f"{B}/spec/confirm", json={"spec": {"task": "x"}}).status_code == 422
        response = client.post(WATCH, json=WATCH_ARGS)
        assert response.status_code == 429 and response.json()["error"] == "rate_limited"
        assert not revit.requests


@pytest.mark.parametrize("assistant_changed", [True, False])
def test_watch_only_clears_the_selection_it_started_with(client, revit, monkeypatch, assistant_changed):
    session = get_session_store().get_or_create()
    before = {"tool": "select_elements", "count": 1, "ids": ["11"]}
    after = {**before, "ids": ["22"]}
    session.assistant_selections["local"] = before

    async def observe(executor, mode, category, timeout_s):
        if assistant_changed:
            session.assistant_selections["local"] = after
        else:
            session.assistant_selections.clear()  # Designer started a new request during the watch.
        return {"kind": "element", "items": [{"id": 22}]}

    monkeypatch.setattr(bridge.RevitQueryExecutor, "watch_selection", observe)
    response = client.post(WATCH, json={**WATCH_ARGS, "session_id": session.session_id})
    if assistant_changed:
        assert response.status_code == 409 and response.json()["error"] == "selection_changed"
        assert "assistant changed the selection" in response.json()["message"]
        assert session.assistant_selections["local"] == after
    else:
        assert response.status_code == 200 and response.json()["items"] == [{"id": 22}]


def test_planner_tool_schemas_use_plain_optional_properties(client, model):
    model.turns.append(text_turn("Ready"))
    client.post("/api/chat", headers=HEADERS, json={"message": "What tools can I use?"})
    tools = model.requests[0]["tools"]
    assert '"anyOf"' not in json.dumps(tools)
    question = next(t["function"]["parameters"] for t in tools if t["function"]["name"] == "ask_selection")
    assert set(question["required"]) == {"prompt", "mode"}
    assert question["properties"]["mode"]["enum"] == ["single", "multiple", "box"]
    assert question["properties"]["category"]["type"] == "string"
    assert not any(key in json.dumps(question) for key in ('"default"', '"pattern"', '"null"'))


def test_disconnect_cancels_package_coroutine(client, revit):
    called = threading.Event()
    def unchanged(args):
        called.set()
        return {"count": 0, "stamp": "empty", "document_id": "doc"}
    native(revit, {STATE: unchanged})
    async def disconnect():
        assert await asyncio.to_thread(called.wait, 2)
        return {"type": "http.disconnect"}
    async def run():
        req = Request({"type": "http", "app": client.app}, disconnect)
        assert await bridge.watch_selection(bridge.WatchRequest(**WATCH_ARGS), req) == {"cancelled": True}
        count = len(revit.requests)
        await asyncio.sleep(.45)
        assert count == len(revit.requests) and not client.app.state.selection_watches
    asyncio.run(run())


@pytest.mark.parametrize("override,named", [(False, False), (False, True), (True, True)])
def test_ui_only_origin_plan_and_counter(client, model, revit, override, named):
    name = "clear_selection"
    if override:
        store = ToolStore()
        store.user_dir.mkdir(parents=True, exist_ok=True)
        (store.user_dir / (name + ".yaml")).write_bytes(store.path_of(name).read_bytes())
    step = {"id": "a", "title": "Clear the selection", "tool": name if named else "create_wall"}
    model.turns.extend([tool_turn(("set_plan", {"steps": [step, {"id": "b", "title": "Next"}]})),
                        tool_turn(("propose_spec", {"spec": spec_for(name), "step_id": "a"})), text_turn("Ready")])
    response = client.post("/api/chat", headers=HEADERS, json={"message": "Clear selection then continue."})
    frames = sse(response.text)
    reads = [row["read"] for row in events(frames, "tool") if "read" in row]
    session = get_session_store().get(response.headers["X-Session-Id"])
    assert session.direct_reads == (0 if override else 1) and session.auto_count == 0
    assert bool(events(frames, "spec")) == override
    assert session.plan.steps[0].status == ("proposed" if override else "done" if named else "pending")
    if override:
        assert not reads and "host:ui-only" not in client.get(f"{B}/evidence").text
    else:
        assert len(reads) == 1 and reads[0]["result"]["ui_only"] and reads[0]["result"]["success"]
        assert "host:ui-only" in client.get(f"{B}/evidence").text


def test_ui_and_reader_share_direct_budget_in_auto_mode(client, model, revit, env):
    from backend.config import reset_settings
    from tests.conftest import FIXTURE_ADMIN_PASSWORD
    env.setenv("AUTO_RUN", "on")
    env.setenv("DIRECT_RUN_LIMIT", "1")
    reset_settings()
    model.turns.extend([tool_turn(("propose_spec", {"spec": spec_for("clear_selection")}),
                                 ("propose_spec", {"spec": spec_for("get_element_geometry", element_id=93)})),
                        text_turn("Cleared")])
    response = client.post("/api/chat", headers={**HEADERS, "X-Admin-Token": FIXTURE_ADMIN_PASSWORD},
                           json={"message": "Clear selection and inspect element 93", "run": "auto"})
    frames = sse(response.text)
    reads = [row["read"]["name"] for row in events(frames, "tool") if "read" in row]
    assert reads == ["clear_selection"] and "read_limit" in response.text
    assert not events(frames, "spec") and not events(frames, "execution")
    assert get_session_store().get(response.headers["X-Session-Id"]).auto_count == 0


def configure_vision(env, tmp_path, vision=True):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ONE_MODEL.replace("W5A_FAKE_MAIN_KEY }", f"W5A_FAKE_MAIN_KEY, vision: {str(vision).lower()} }}"))
    env.setenv("W5A_FAKE_MAIN_KEY", "fake-long-key")
    reset_models()


@pytest.mark.parametrize("vision", [True, False])
def test_capture_context_on_following_turns_and_privacy(client, model, revit, env, tmp_path, caplog, vision):
    configure_vision(env, tmp_path, vision)
    native(revit, {"export_view_image_in_revit": lambda args: {"png_base64": PNG, "width": 1, "height": 1, "view_name": "Plan"},
                   "activate_view_in_revit": lambda args: {"view_name": args["name"], "view_type": "Plan", "view_id": 15}})
    assert client.get(f"{B}/host-functions").json() == list_host_functions()
    model.turns += [tool_turn(("open_view", {"name": "Plan"}), ("capture_view_image", {})), text_turn("Captured")]
    response = client.post("/api/chat", json={"message": "Open Plan and capture it."})
    assert response.status_code == 200, response.text
    sid = response.headers["X-Session-Id"]
    frames = sse(response.text)
    image, = events(frames, "view_image")
    assert image["png_base64"] == PNG and image["view_name"] == "Plan"
    assert PNG not in str([(event, value) for event, value in frames if event != "view_image"])
    assert PNG not in json.dumps(model.requests)
    for attach in (True, False, True):
        if attach:
            model.turns.append(tool_turn(("query", {"kind": "levels"})))
        model.turns.append(text_turn("OK"))
        response = client.post("/api/chat", json={"session_id": sid, "message": "Describe the view",
                                                  "hidden_captures": [] if attach else [image["capture_id"]]})
        assert response.status_code == 200
        assert PNG not in response.text
        assert ("vision_unavailable" in response.text) == (not vision and attach)
        assert (PNG in json.dumps(model.requests[-1])) == (attach and vision)
        if attach and vision:
            first, second = [r["messages"] for r in model.requests[-2:]]
            assert second[:len(first)] == first
            parts, = [m["content"] for m in first if isinstance(m.get("content"), list)]
            assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}
            assert llm.responses_input([{"role": "user", "content": parts}])[0]["content"][1] == {
                "type": "input_image", "image_url": "data:image/png;base64," + PNG}
    if vision:
        model.turns.append(text_turn(PNG))
        echoed = client.post("/api/chat", json={"session_id": sid, "message": "Try again"})
        assert "Model endpoint echoed the image" in echoed.text and PNG not in echoed.text
    ledger = client.get(f"{B}/evidence").text
    assert "sha256" in ledger and "host:ui-only" in ledger and "host:read-only" in ledger
    for surface in [ledger, caplog.text, get_log_store().query(), get_log_store().calls_of_session(sid),
                    get_session_store().get(sid).history]:
        assert PNG not in str(surface)


@pytest.mark.parametrize("surface", ["text", "tool"])
def test_partial_image_echo_never_reaches_chat_or_logs(client, model, env, tmp_path, caplog, surface):
    configure_vision(env, tmp_path)
    model.turns.append(text_turn("Ready"))
    sid = client.post("/api/chat", json={"message": "Hello"}).headers["X-Session-Id"]
    get_session_store().get(sid).captures.add("local", {"width": 1, "height": 1, "view_name": "Plan", "png_base64": PNG})
    fragment = PNG[39:73]
    model.turns.append(text_turn(fragment) if surface == "text" else tool_turn(("query", {"kind": fragment})))
    reply = client.post("/api/chat", json={"message": "Describe", "session_id": sid})
    assert "Model endpoint echoed the image" in reply.text
    for value in (reply.text, caplog.text, get_session_store().get(sid).history,
                  get_log_store().query(), get_log_store().calls_of_session(sid)):
        assert fragment not in str(value)


def test_capture_on_query_answer_skips_analyst_and_uses_full_tool_route(roles_client, roles_model, picker, env, tmp_path):
    configure_vision(env, tmp_path)
    select_one(picker)
    roles_model.script("analyst", json_turn(ROUTING_QUERY))
    roles_model.script("planner", tool_turn(("ask_selection", {"mode": "single", "prompt": "Choose a wall"})),
                       text_turn("Ready"))
    first = roles_client.post("/api/chat", json={"message": "Inspect"})
    sid = first.headers["X-Session-Id"]
    get_session_store().get(sid).captures.add("local", {"width": 1, "height": 1, "view_name": "Plan", "png_base64": PNG})
    form, = events(sse(first.text), "ask_selection")
    reply = roles_client.post("/api/chat", json={"message": "Use this wall and view",
        "session_id": sid, "selection_answer": form["ask_id"],
        "mentions": [{"kind": "element", "id": 93, "document_id": "fake-document"}]})
    assert reply.status_code == 200 and roles_model.roles == ["analyst", "planner", "planner"]
    assert "propose_spec" in [t["function"]["name"] for t in roles_model.requests[-1]["tools"]]
    assert PNG in json.dumps(roles_model.requests[-1]["messages"])


@pytest.mark.parametrize("too_wide", [True, False])
def test_capture_outside_either_limit_is_not_delivered(client, model, revit, too_wide):
    encoded = PNG if too_wide else base64.b64encode(base64.b64decode(PNG) + bytes(1024 * 1024)).decode()
    native(revit, {"export_view_image_in_revit": lambda args: {
        "png_base64": encoded, "width": 1601 if too_wide else 1, "height": 1, "view_name": "Plan"}})
    model.turns.extend([tool_turn(("capture_view_image", {})), text_turn("Export failed")])
    response = client.post("/api/chat", headers=HEADERS, json={"message": "Capture"})
    assert response.status_code == 200 and "capture_failed" in response.text
    assert not events(sse(response.text), "view_image") and encoded[:40] not in response.text
    assert encoded[:40] not in client.get(f"{B}/evidence").text


def test_responses_planner_receives_pixels_on_following_requests(env, keys, tmp_path, provider, revit):
    native(revit, {"export_view_image_in_revit": lambda args: {"png_base64": PNG, "width": 1, "height": 1, "view_name": "Plan"}})
    provider.replies.extend([reply(call_item("capture_view_image", {}, "capture")), reply(message_item("Captured")),
                             reply(message_item("Described")), reply(message_item("Done"))])
    with app_client(env, tmp_path, models_text(main=", api: responses, vision: true")) as client:
        first = client.post("/api/chat", json={"message": "Capture"})
        sid = first.headers["X-Session-Id"]
        image, = events(sse(first.text), "view_image")
        for extra in ({}, {}):
            assert client.post("/api/chat", json={"session_id": sid, "message": "Continue", **extra}).status_code == 200
    first, followup, attached, later = provider.responses_bodies()
    assert attached["input"][-1]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64," + PNG}
    assert PNG not in json.dumps([first, followup]) and PNG in json.dumps(later)


def test_responses_blocks_partial_image_echo_in_tool_arguments(env, keys, tmp_path, provider):
    fragment = PNG[-37:]
    provider.replies.extend([reply(message_item("Ready")), reply(call_item("query", {"kind": fragment}, "echo"))])
    with app_client(env, tmp_path, models_text(main=", api: responses, vision: true")) as client:
        sid = client.post("/api/chat", json={"message": "Hello"}).headers["X-Session-Id"]
        get_session_store().get(sid).captures.add("local", {"width": 1, "height": 1, "view_name": "Plan", "png_base64": PNG})
        response = client.post("/api/chat", json={"session_id": sid, "message": "Describe"})
        assert "Model endpoint echoed the image" in response.text
        for value in (response.text, get_log_store().query(), get_session_store().get(sid).history):
            assert fragment not in str(value)

def test_selection_indicator_shares_a_light_read_and_send_verifies_document(client, model, picker):
    select_one(picker)
    url = B + "/selection-state"
    state = client.get(url).json()
    assert state == {"count": 1, "stamp": "selected", "document_id": "fake-document", "assistant_set": False}
    cached = client.get(url)
    assert cached.headers["cache-control"] == "no-store" and cached.json() == state
    assert [(r["method"], r["params"]) for r in picker["fake"].requests] == [(STATE, {})]
    model.turns.append(text_turn("Checked"))
    response = client.post("/api/chat", headers=HEADERS, json={"message": "Inspect", "current_selection": state})
    assert response.status_code == 200
    report, = events(sse(response.text), "mentions")
    assert report["resolved"][0]["id"] == 93 and report["current_selection"]["count"] == 1
    sent = model.requests[-1]["messages"][-1]["content"]
    assert 'Current selection (from the host' in sent and '"OST_Walls"' in sent and "Wall" in sent
    assert get_session_store().get(response.headers["X-Session-Id"]).picks_for("local")["mention:element#93"]["document_id"] == "fake-document"
    for key in ("document_id", "stamp"):
        response = client.post("/api/chat", headers={**HEADERS, "Accept-Language": "zh"},
                               json={"message": "Inspect", "current_selection": {**state, key: "stale"}})
        assert response.status_code == 409 and response.json()["error"] == "selection_changed"
        assert "选择已变化" in response.json()["message"]
    assert len(model.requests) == 1


def test_implicit_picks_exclude_a_host_selection_even_in_another_session(client, model, picker):
    select_one(picker)
    model.turns.extend([tool_turn(("propose_spec", {"spec": spec_for("select_elements", element_ids="93")})), text_turn("Selected")])
    first = client.post("/api/chat", headers=HEADERS, json={"message": "Select 93"})
    assert first.status_code == 200
    reading = client.get(B + "/selection-state").json()
    assert reading["assistant_set"] is True
    model.turns.append(text_turn("Ready"))
    second = client.post("/api/chat", headers=HEADERS, json={"message": "What is here?", "current_selection": reading})
    assert second.status_code == 200 and not events(sse(second.text), "mentions")
    assert not get_session_store().get(second.headers["X-Session-Id"]).picks_for("local")


def test_capture_retention_context_limits_and_prefix(client, model, revit, env, tmp_path):
    configure_vision(env, tmp_path)
    names = iter(["North", "South", "West"])
    native(revit, {"export_view_image_in_revit": lambda _: {"png_base64": PNG, "width": 1, "height": 1, "view_name": next(names)}})
    model.turns.extend([tool_turn(*[("capture_view_image", {}) for _ in range(3)]), text_turn("Captured"),
                        text_turn("One"), text_turn("Two")])
    sid = client.post("/api/chat", json={"message": "Capture views"}).headers["X-Session-Id"]
    for message in ["One?", "Two?"]:
        assert client.post("/api/chat", json={"message": message, "session_id": sid}).is_success
    first, second = (r["messages"] for r in model.requests[-2:])
    assert second[:len(first)] == first
    assert json.dumps(second).count(PNG) == 2 and all(name in str(second) for name in ("North", "South", "West"))
    assert all("1 x 1" in str(m) for m in second if isinstance(m["content"], list))
    assert not get_session_store().get(sid).captures.for_scope("other")
    env.setenv("CAPTURE_CONTEXT", "off")
    from backend.config import reset_settings
    reset_settings()
    model.turns.append(text_turn("Text only"))
    off = client.post("/api/chat", json={"message": "Again", "session_id": sid})
    assert events(sse(off.text), "image_notice") == [{"reason": "context_disabled"}]
    assert PNG not in str(model.requests[-1]) and "North" not in str(model.requests[-1])


@pytest.mark.parametrize("language,question", [("zh", "没有现成的工具能完成这件事，要现在添加吗？"), ("en", "No existing tool can do this. Add one now?")])
def test_missing_tool_form_uses_request_language(client, model, language, question):
    from backend.api.model_tools import WORKBENCH_LINK
    model.turns.append(text_turn("No tool fits. " + WORKBENCH_LINK))
    response = client.post("/api/chat", headers={**HEADERS, "Accept-Language": language}, json={"message": "Draft a spiral"})
    form, = events(sse(response.text), "missing_tool")
    assert form["request"] == "Draft a spiral" and form["question"] == question
    assert set(form["choices"]) == {"authoring", "remote_mcp", "dismiss"}
    assert len(model.requests) == 1

def test_planner_watch_is_bound_to_the_question_device(client, model, picker, device):
    select_one(picker)
    model.turns.append(tool_turn(("ask_selection", {"prompt": "选择墙", "mode": "single"})))
    first = client.post("/api/chat", headers=HEADERS, json={"message": "Inspect"})
    form, = events(sse(first.text), "ask_selection")
    args = {**WATCH_ARGS, "session_id": first.headers["X-Session-Id"], "ask_id": form["ask_id"]}
    assert client.post(WATCH, headers=device[0], json=args).status_code == 409
    assert not picker["fake"].requests
    reply = client.post(WATCH, json=args)
    assert reply.json()["items"][0]["id"] == 93
    focus, = [r["params"] for r in picker["fake"].requests if r["params"].get("focus")]
    assert focus["prompt"] == "选择墙"


def test_watch_discards_cross_session_assistant_write(client, revit, monkeypatch):
    store = get_session_store()
    first, second = store.get_or_create(), store.get_or_create()
    native(revit, {STATE: lambda _: {"count": 1, "stamp": "host", "document_id": "doc"}})

    async def observe(executor, mode, category, timeout_s):
        second.assistant_selections["local"] = {"tool": "select_elements", "ids": ["22"], "count": 1}
        await bridge.device_selection().mark_write(executor.client)
        return {"items": [{"id": 22}]}

    monkeypatch.setattr(bridge.RevitQueryExecutor, "watch_selection", observe)
    response = client.post(WATCH, json={**WATCH_ARGS, "session_id": first.session_id})
    assert response.status_code == 409 and response.json()["error"] == "selection_changed"
    assert client.get(B + "/selection-state").json()["assistant_set"]


def test_hide_survives_omitted_mask_and_new_session_releases_pixels(client, model, env, tmp_path, device):
    configure_vision(env, tmp_path)
    session = get_session_store().get_or_create()
    image = {"width": 1, "height": 1, "view_name": "Plan", "png_base64": PNG}
    local = session.captures.add("local", image)
    session.captures.add(device[1], image)
    for extra in ({"hidden_captures": [local]}, {}):
        model.turns.append(text_turn("Ready"))
        response = client.post("/api/chat", json={"session_id": session.session_id, "message": "Describe", **extra})
        assert events(sse(response.text), "capture_context") == [{"states": {local: "hidden"}}]
        assert PNG not in str(model.requests[-1])
    assert client.delete(f"/api/chat/{session.session_id}/captures").is_success
    assert not session.captures.for_scope("local") and session.captures.for_scope(device[1])


def test_other_tabs_cannot_poll_during_a_stream_or_watch(client, revit):
    native(revit, {STATE: lambda _: {"count": 0, "stamp": "empty", "document_id": "doc"}})
    state = bridge.device_selection()
    state.streams = 1
    assert client.get(B + "/selection-state").status_code == 409
    state.streams = 0
    client.app.state.selection_watches["local"] = ("tab", "watch", None)
    assert client.get(B + "/selection-state").status_code == 409
    assert not revit.requests


def test_confirmed_selector_marks_device_before_a_chat_receipt(client, picker, model):
    select_one(picker)
    params = {"element_ids": "93"}
    token = confirm(client, spec_for("select_elements", **params))
    result = client.post(f"{B}/tools/select_elements/run", json={"params": params, "token": token})
    assert result.json()["success"], result.text
    current = client.get(B + "/selection-state").json()
    assert current["assistant_set"] is True
    model.turns.append(text_turn("Ready"))
    response = client.post("/api/chat", headers=HEADERS, json={"message": "Look here", "current_selection": current})
    assert response.is_success and not events(sse(response.text), "mentions")


def test_newly_visible_older_capture_appends_after_existing_context(client, model, env, tmp_path):
    configure_vision(env, tmp_path)
    session = get_session_store().get_or_create()
    ids = [session.captures.add("local", {"png_base64": PNG, "width": 1, "height": 1, "view_name": name})
           for name in ("North", "South", "East")]
    model.turns.extend([text_turn("One"), text_turn("Two")])
    for message, hidden in [("First", ids[:1]), ("Next", [])]:
        reply = client.post("/api/chat", json={"message": message, "session_id": session.session_id, "hidden_captures": hidden})
        assert reply.is_success
    first, second = [r["messages"] for r in model.requests]
    assert second[:len(first)] == first
    assert "North" not in str(first) and "North" in str(second[len(first):])


def test_tabs_share_backoff_after_a_failed_state_read(client, revit):
    native(revit, {STATE: lambda _: {"invalid": True}})
    for _ in range(2):
        response = client.get(B + "/selection-state")
        assert response.status_code == 503 and response.json()["error"] == "selection_unavailable"
    assert [r["method"] for r in revit.requests] == [STATE]
