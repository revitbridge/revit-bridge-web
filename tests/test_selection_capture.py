"""Designer interaction through the HTTP chat/bridge routes, a fake add-in and a fake model."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
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
from tests.test_api_v1 import B, spec_for
from tests.test_host_loop import HEADERS, events, model, sse, tokens
from tests.test_mentions import picker
from tests.test_models_file import ONE_MODEL
from tests.test_provider_compat import app_client, call_item, keys, message_item, models_text, provider, reply
from tests.test_roles import ROUTING_QUERY, client as roles_client, model as roles_model

WATCH = f"{B}/watch-selection"
WATCH_ARGS = {"session_id": "tab", "watch_id": "watch", "mode": "single", "timeout_s": 4}
STATE = "get_selection_state_in_revit"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jCowAAAAASUVORK5CYII="


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


def test_click_watch_then_submit_verified_answer(client, model, picker):
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
        client.delete(f"{WATCH}/tab/other")
        assert not watching.done()
        assert client.delete(f"{WATCH}/tab/watch").status_code == 200
        assert watching.result(2).json() == {"cancelled": True}
    count = len(revit.requests)
    time.sleep(.45)
    assert len(revit.requests) == count


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


@pytest.mark.parametrize("vision", [True, False])
def test_capture_attachment_once_and_privacy(client, model, revit, env, tmp_path, caplog, vision):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ONE_MODEL.replace("W5A_FAKE_MAIN_KEY }", f"W5A_FAKE_MAIN_KEY, vision: {str(vision).lower()} }}"))
    env.setenv("W5A_FAKE_MAIN_KEY", "fake-long-key")
    reset_models()
    native(revit, {"export_view_image_in_revit": lambda args: {"png_base64": PNG, "width": 1, "height": 1, "view_name": "Plan"},
                   "activate_view_in_revit": lambda args: {"view_name": args["name"], "view_type": "Plan", "view_id": 15}})
    assert client.get(f"{B}/host-functions").json() == list_host_functions()
    model.turns += [tool_turn(("open_view", {"name": "Plan"}), ("capture_view_image", {})), text_turn("Captured")]
    response = client.post("/api/chat", json={"message": "Open Plan and capture it."})
    assert response.status_code == 200, response.text
    sid = response.headers["X-Session-Id"]
    frames = sse(response.text)
    image, = events(frames, "view_image")
    attachment = {k: image[k] for k in ("capture_id", "png_base64")}
    assert image["png_base64"] == PNG and image["view_name"] == "Plan"
    assert PNG not in str([(event, value) for event, value in frames if event != "view_image"])
    assert PNG not in json.dumps(model.requests)
    for attach in (False, True, False):
        model.turns.append(text_turn("OK"))
        response = client.post("/api/chat", json={"session_id": sid, "message": "Describe the view",
                                                  **({"capture": attachment} if attach else {})})
        assert response.status_code == 200
        assert PNG not in response.text
        assert ("vision_unavailable" in response.text) == (attach and not vision)
        assert (PNG in json.dumps(model.requests[-1])) == (attach and vision)
        if attach and vision:
            parts = model.requests[-1]["messages"][-1]["content"]
            assert parts[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}
            assert llm.responses_input([{"role": "user", "content": parts}])[0]["content"][1] == {
                "type": "input_image", "image_url": "data:image/png;base64," + PNG}
    if vision:
        model.turns.append(text_turn("OK"))
        forged = client.post("/api/chat", json={"session_id": sid, "message": "Another image",
                                                "capture": {**attachment, "png_base64": PNG + "AAAA"}})
        assert "capture_unavailable" in forged.text and PNG not in json.dumps(model.requests[-1])
        model.turns.append(text_turn(PNG))
        echoed = client.post("/api/chat", json={"session_id": sid, "message": "Try again", "capture": attachment})
        assert "Model endpoint echoed the image" in echoed.text and PNG not in echoed.text
    ledger = client.get(f"{B}/evidence").text
    assert "sha256" in ledger and "host:ui-only" in ledger and "host:read-only" in ledger
    for surface in [ledger, caplog.text, get_log_store().query(), get_log_store().calls_of_session(sid),
                    get_session_store().get(sid).history]:
        assert PNG not in str(surface)


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


def test_responses_planner_receives_pixels_only_on_the_attached_request(env, keys, tmp_path, provider, revit):
    native(revit, {"export_view_image_in_revit": lambda args: {"png_base64": PNG, "width": 1, "height": 1, "view_name": "Plan"}})
    provider.replies.extend([reply(call_item("capture_view_image", {}, "capture")), reply(message_item("Captured")),
                             reply(message_item("Described")), reply(message_item("Done"))])
    with app_client(env, tmp_path, models_text(main=", api: responses, vision: true")) as client:
        first = client.post("/api/chat", json={"message": "Capture"})
        sid = first.headers["X-Session-Id"]
        image, = events(sse(first.text), "view_image")
        attachment = {k: image[k] for k in ("capture_id", "png_base64")}
        for extra in ({"capture": attachment}, {}):
            assert client.post("/api/chat", json={"session_id": sid, "message": "Continue", **extra}).status_code == 200
    first, followup, attached, later = provider.responses_bodies()
    assert attached["input"][-1]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64," + PNG}
    assert PNG not in json.dumps([first, followup, later])
