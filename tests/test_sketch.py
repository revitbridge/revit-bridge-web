"""A sketch on a view capture: the capture and resolve routes, and the chat's ``sketch``. The
frame, the conversion and the text are the package's; these tests pin what the host does."""
from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

from backend import sketches
from backend.session import get_session_store
from tests.fake_model import text_turn
from tests.fake_revit import FakeRevit, model_handler
from tests.test_api_v1 import B
from tests.test_host_loop import HEADERS, events, model, sse  # noqa: F401 - the model fixture
from tests.test_selection_capture import configure_vision

FEET = 304.8
# The view shows x from -6000 to 10000 mm and y from -2000 to 6000 mm: 10 mm per pixel at 1600 x 800.
RAW = {"document_id": "fake-document", "view_id": "32", "view_uid": "fake-view", "view_name": "L1",
       "view_type": "FloorPlan", "level": "L1", "elevation": 0.0,
       "c0": [-6000 / FEET, -2000 / FEET, 0.0], "c1": [10000 / FEET, 6000 / FEET, 0.0],
       "right": [1.0, 0.0, 0.0], "up": [0.0, 1.0, 0.0], "direction": [0.0, 0.0, 1.0]}
TILTED = {**RAW, "view_type": "ThreeD", "view_name": "Tilted box", "is_3d": True, "is_perspective": False,
          "level": None, "elevation": None, "levels": [{"name": "Shelf", "elevation": 2000 / FEET},
                                                       {"name": "Base", "elevation": 0}],
          "right": [1, 0, 0], "up": [0, .8, .6], "direction": [0, -.6, .8],
          "c0": [-6000 / FEET, -4000 / FEET, 2000 / FEET], "c1": [10000 / FEET, 2400 / FEET, 6800 / FEET]}
WALLS = [{"id": "11", "straight": True, "a": [-3000 / FEET, 1000 / FEET], "b": [-3000 / FEET, 5000 / FEET]},
         {"id": "12", "straight": True, "a": [0.0, -1000 / FEET], "b": [0.0, 6000 / FEET]}]
STROKE = [[310, 108], [590, 90]]      # from the end of wall 11 to wall 12


def png(width: int, height: int) -> str:
    """A real, small PNG of this size."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = (b"\x00" + b"\xff" * width) * height
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")).decode()


PICTURE = png(1600, 800)


@pytest.fixture
def plan_view(env):
    """A fake add-in with a plan view open: the frame, the export and the view's walls."""
    state: dict = {"frame": dict(RAW), "frames": 0}
    base = model_handler()

    def handler(request: dict) -> dict:
        method, rid, params = request.get("method"), request.get("id"), request.get("params") or {}
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(rid, {"protocol": 2, "methods": ["export_view_image_in_revit", "send_code_to_revit"]})
        if method == "export_view_image_in_revit":
            return FakeRevit.ok(rid, {"png_base64": PICTURE, "width": 1600, "height": 800, "view_name": state["frame"]["view_name"]})
        code = params.get("code", "") if method == "send_code_to_revit" else ""
        if "GetZoomCorners" in code:
            state["frames"] += 1
            if state.get("moves") and state["frames"] % 2 == 0:
                return FakeRevit.code_result(rid, {**state["frame"], "c1": [10500 / FEET, 6000 / FEET, 0.0]})
            return FakeRevit.code_result(rid, state["frame"])
        if "return new { walls, grids, columns, framing, complete }" in code:
            if state.get("gone"):
                return FakeRevit.code_result(rid, None, success=False,
                                             error="The view of the sketch is no longer open; capture the view again.")
            assert params["parameters"] == ["fake-view", "fake-document", state.get("expected_level", "")]
            return FakeRevit.code_result(rid, {"walls": WALLS, "complete": True})
        return base(request)

    with FakeRevit(handler) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        yield state


def capture(client) -> dict:
    reply = client.post(f"{B}/sketch/capture")
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_capture_returns_the_picture_and_keeps_the_frame_on_the_host(plan_view, client):
    shot = capture(client)
    assert set(shot) == {"capture_id", "png_base64", "width", "height", "view_name", "level", "kind", "levels"}
    assert shot["kind"] == "plan" and shot["levels"] == []
    assert shot["png_base64"] == PICTURE and (shot["width"], shot["height"]) == (1600, 800)
    assert shot["view_name"] == "L1" and shot["level"] == "L1"
    assert "frame" not in json.dumps(shot) and "fake-view" not in json.dumps(shot)


@pytest.mark.parametrize("change,status,code", [
    ({"frame": {**RAW, "view_type": "ThreeD"}}, 409, "unsupported_view"),
    ({"moves": True}, 409, "view_changed")])
def test_capture_refuses_a_view_that_is_not_a_plan_or_that_moved(plan_view, client, change, status, code):
    plan_view.update(change)
    reply = client.post(f"{B}/sketch/capture")
    assert reply.status_code == status and reply.json()["error"] == code and reply.json()["message"]
    assert "png_base64" not in reply.text


def test_capture_without_revit_is_503(client):
    reply = client.post(f"{B}/sketch/capture")
    assert reply.status_code == 503 and reply.json()["error"] == "revit_unreachable"


def test_resolve_is_the_package_reply(plan_view, client):
    shot = capture(client)
    reply = client.post(f"{B}/sketch/resolve", json={"capture_id": shot["capture_id"], "strokes": [STROKE]})
    assert reply.status_code == 200, reply.text
    assert reply.json() == {
        "view_name": "L1", "view_type": "FloorPlan", "view_kind": "plan", "level": "L1", "elevation_mm": 0.0, "tolerance_mm": 250,
        "warnings": [], "lines": [{"line": 1, "length_mm": 3000, "pixels": [[300.0, 100.0], [600.0, 100.0]], "points": [
            {"x": -3000, "y": 5000, "snap": {"kind": "wall_end", "wall_id": 11}},
            {"x": 0, "y": 5000, "snap": {"kind": "on_wall", "wall_id": 12}}]}]}


def test_3d_capture_and_resolve_use_the_packages_levels(plan_view, client):
    plan_view.update(frame=TILTED, expected_level="Shelf")
    shot = capture(client)
    assert shot["kind"] == "axonometric" and shot["level"] is None
    assert shot["levels"] == [{"name": "Base", "elevation_mm": 0}, {"name": "Shelf", "elevation_mm": 2000}]
    assert "frame" not in shot and "fake-view" not in json.dumps(shot)
    body = {"capture_id": shot["capture_id"], "strokes": [[[310, 88], [590, 70]]]}
    for extra in ({}, {"level": "Missing"}):
        refused = client.post(f"{B}/sketch/resolve", json={**body, **extra})
        assert refused.status_code == 422 and refused.json()["error"] == "invalid_args"
        assert "Base, Shelf" in refused.json()["message"]
    reply = client.post(f"{B}/sketch/resolve", json={**body, "level": "Shelf"})
    assert reply.status_code == 200
    result = reply.json()
    assert (result["view_kind"], result["level"], result["elevation_mm"]) == ("axonometric", "Shelf", 2000)
    assert result["lines"][0]["pixels"] == [[300, 80], [600, 80]]


def test_a_closed_line_returns_each_corner_once_and_includes_the_closing_edge(plan_view, client):
    rectangle = [[800, 200], [1000, 200], [1000, 400], [800, 400], [800, 200]]
    reply = client.post(f"{B}/sketch/resolve", json={"capture_id": capture(client)["capture_id"], "strokes": [rectangle]})
    assert reply.status_code == 200
    line, = reply.json()["lines"]
    assert line["closed"] is True and line["pixels"] == rectangle[:-1] and line["length_mm"] == 8000


def test_resolve_refuses_what_it_cannot_read(plan_view, client):
    shot = capture(client)
    unknown = client.post(f"{B}/sketch/resolve", json={"capture_id": "ab" * 16, "strokes": [STROKE]})
    assert unknown.status_code == 404 and unknown.json()["error"] == "unknown_sketch"
    for bad in ({"capture_id": shot["capture_id"], "strokes": []}, {"capture_id": "../x", "strokes": [STROKE]},
                {"capture_id": shot["capture_id"], "strokes": [STROKE], "frame": RAW},
                {"capture_id": shot["capture_id"], "strokes": [[[1, 1], [2, 2]]]},
                {"capture_id": shot["capture_id"], "strokes": [[[0, 0], [5000, 0]]]}):
        reply = client.post(f"{B}/sketch/resolve", json=bad)
        assert reply.status_code == 422 and reply.json()["error"] == "invalid_args", bad
    plan_view.update(gone=True)
    gone = client.post(f"{B}/sketch/resolve", json={"capture_id": shot["capture_id"], "strokes": [STROKE]})
    assert gone.status_code == 409 and gone.json()["error"] == "view_changed"


def test_a_capture_belongs_to_its_device_and_expires(plan_view, client, monkeypatch):
    shot = capture(client)
    store = sketches.get_sketch_store()
    assert store.get("another-device", shot["capture_id"]) is None
    monkeypatch.setattr(sketches.time, "monotonic", lambda: 10 ** 9)
    assert store.get(next(iter(store._items.values())).scope, shot["capture_id"]) is None


def chat(client, message, **extra):
    return client.post("/api/chat", json={"message": message, **extra}, headers=HEADERS)


def test_the_chat_gets_the_sketch_as_model_coordinates_after_the_designers_words(plan_view, client, model):
    shot = capture(client)
    model.turns.append(text_turn("ok"))
    reply = chat(client, "draw a wall on the red line", sketch={"capture_id": shot["capture_id"], "strokes": [STROKE]})
    assert reply.status_code == 200, reply.text
    assert events(sse(reply.text), "error") == []
    sent = model.last_messages()[-1]["content"]
    text = sent if isinstance(sent, str) else " ".join(p.get("text", "") for p in sent)
    assert text.startswith("draw a wall on the red line\n\nDesigner's sketch (drawn by the designer on a capture of "
                           "the plan view \"L1\", level \"L1\"")
    assert "line 1 from (-3000, 5000) [the end of wall id 11] to (0, 5000) [on wall id 12], length 3000 mm" in text
    assert "source designer" in text
    # No image went along: none was sent, and this model takes none.
    assert "image_url" not in json.dumps(model.requests) and PICTURE not in json.dumps(model.requests)


def test_the_sketch_is_read_again_by_the_host_and_bad_ones_stop_the_turn(plan_view, client, model):
    shot = capture(client)
    good = {"capture_id": shot["capture_id"], "strokes": [STROKE]}
    for body, status, code in (
            ({"sketch": {**good, "capture_id": "ab" * 16}}, 404, "unknown_sketch"),
            ({"sketch": {**good, "strokes": [[[1, 1], [2, 2]]]}}, 422, "invalid_args"),
            ({"sketch": {**good, "image": "not base64 !"}}, 422, "invalid_args"),
            ({"sketch": {**good, "image": png(800, 400)}}, 422, "invalid_args"),
            ({"sketch": {**good, "lines": [{"points": [{"x": 1, "y": 2}]}]}}, 422, "invalid_args"),
            ({"sketch": good, "bridge": False}, 422, "invalid_args")):
        reply = chat(client, "draw a wall here", **body)
        assert reply.status_code == status and code in reply.text, (body, reply.text)
    assert model.requests == []                                    # no model was called


def test_chat_projects_the_3d_sketch_onto_the_requested_level(plan_view, client, model):
    plan_view.update(frame=TILTED, expected_level="Shelf")
    model.turns.append(text_turn("ok"))
    reply = chat(client, "draw here", sketch={"capture_id": capture(client)["capture_id"], "strokes": [STROKE], "level": "Shelf"})
    assert reply.status_code == 200
    assert '3D view "Tilted box", projected onto level "Shelf"' in model.last_messages()[-1]["content"]


def test_the_drawn_picture_becomes_a_session_capture_and_never_reaches_a_model_without_vision(plan_view, client, model):
    shot = capture(client)
    drawn = png(1600, 800)
    model.turns.append(text_turn("ok"))
    reply = chat(client, "draw a wall here", sketch={"capture_id": shot["capture_id"], "strokes": [STROKE], "image": drawn})
    assert reply.status_code == 200, reply.text
    assert drawn not in json.dumps(model.requests)
    session = next(iter(get_session_store()._sessions.values()))
    (entry,) = session.captures.entries
    assert entry.png == drawn and entry.summary == "L1 - designer's sketch (1600 x 800)"


def test_late_refusal_does_not_store_a_picture_and_retry_shows_it_to_the_planner(plan_view, roles_env, client, model, tmp_path):
    from backend.api import chat as chat_module

    configure_vision(roles_env, tmp_path)
    session = get_session_store().get_or_create()
    body = {"session_id": session.session_id, "message": "draw here",
            "sketch": {"capture_id": capture(client)["capture_id"], "strokes": [STROKE], "image": PICTURE}}
    # Simulate another turn completing while the device read yields, just before the final lock check.
    async def competing_turn(*args):
        session.next_turn()
        return None, set(), None

    with roles_env.context() as patch:
        patch.setattr(chat_module, "message_picks", competing_turn)
        refused = client.post("/api/chat", json=body)
    assert refused.status_code == 409 and refused.json()["error"] == "turn_in_progress"
    assert not session.captures.entries and not model.requests and PICTURE not in refused.text
    model.turns.append(text_turn("ok"))
    accepted = client.post("/api/chat", json=body)
    assert accepted.status_code == 200 and not events(sse(accepted.text), "error")
    entry, = session.captures.entries
    assert events(sse(accepted.text), "capture_context") == [{"states": {entry.id: "used"}}]
    request, = model.requests
    assert json.dumps(request["messages"]).count("data:image/png;base64," + PICTURE) == 1
    assert PICTURE not in accepted.text and PICTURE not in str(session.history)


def test_checked_png_reads_only_the_header():
    assert sketches.checked_png(PICTURE, 1600, 800) == PICTURE
    for bad, why in ((png(1600, 801), "size"), ("####", "base64"), (base64.b64encode(b"GIF89a" + bytes(40)).decode(), "PNG"),
                     ("A" * (4 * sketches.MAX_IMAGE_BYTES), "too large")):
        with pytest.raises(ValueError, match=why):
            sketches.checked_png(bad, 1600, 800)


# -- what a request may ask for (review of the first version) -----------------------------

def test_a_stroke_has_at_most_the_points_the_page_sends_and_a_point_two_numbers(plan_view, client):
    shot = capture(client)
    most = [[300 + i * 0.4, 100] for i in range(sketches.MAX_STROKE_POINTS)]
    assert client.post(f"{B}/sketch/resolve", json={"capture_id": shot["capture_id"], "strokes": [most]}).status_code == 200
    for bad in ([most + [[600, 100]]], [[[310, 108, 5], [590, 90]]], [[[310], [590, 90]]], [[[310, 108]]],
                [STROKE] * 9):
        reply = client.post(f"{B}/sketch/resolve", json={"capture_id": shot["capture_id"], "strokes": bad})
        assert reply.status_code == 422 and reply.json()["error"] == "invalid_args"
        refused = client.post("/api/chat", json={"message": "draw", "sketch": {"capture_id": shot["capture_id"], "strokes": bad}},
                              headers=HEADERS)
        assert refused.status_code == 422


def test_a_refused_chat_sketch_does_not_echo_the_picture(plan_view, client):
    shot = capture(client)
    huge = "A" * (4 * sketches.MAX_IMAGE_BYTES)
    reply = client.post("/api/chat", json={"message": "draw", "sketch": {
        "capture_id": shot["capture_id"], "strokes": [STROKE], "image": huge}}, headers=HEADERS)
    assert reply.status_code == 422 and reply.json() == {"error": "invalid_args"}
    assert "AAAA" not in reply.text


def test_one_device_keeps_a_few_captures_and_cannot_push_out_anothers():
    store = sketches.SketchStore()
    reply = {"frame": {}, "width": 1600, "height": 800, "view_name": "L1"}
    other = store.add("device-b", reply)
    mine = [store.add("device-a", reply) for _ in range(sketches.MAX_CAPTURES + 5)]
    assert store.get("device-b", other.id) is other
    kept = [item for item in mine if store.get("device-a", item.id)]
    assert kept == mine[-sketches.MAX_CAPTURES_PER_SCOPE:]


def test_the_sketch_routes_are_rate_limited_like_confirm(plan_view, client, monkeypatch):
    from backend.api import bridge
    shot = capture(client)
    monkeypatch.setattr(bridge.confirm_limiter, "allow", lambda *args, **kwargs: False)
    for path, body in ((f"{B}/sketch/capture", None), (f"{B}/sketch/resolve", {"capture_id": shot["capture_id"], "strokes": [STROKE]})):
        reply = client.post(path, json=body)
        assert reply.status_code == 429 and reply.json()["error"] == "rate_limited"


@pytest.mark.parametrize("name", [r"Plan\q", "Plan\\", r'Plan "A"', r"L1"])
def test_a_view_name_cannot_break_the_text_block(name):
    from backend.api.chat import sketch_block
    drawn = {"view_name": name, "level": "L1", "lines": [{"line": 1, "length_mm": 10, "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}]}]}
    text = sketch_block(drawn)
    assert text.startswith("Designer's sketch (") and "\\" not in text.split("; the host converted")[0]
    assert ', level "L1"; the host converted' in text
