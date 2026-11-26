"""Chat attachments and execution gates with a scripted model and fake add-in."""
import pytest

from tests.fake_model import json_turn, text_turn, tool_turn
from tests.test_api_v1 import B, spec_for
from tests.test_auto_run import client, rotate_turn  # noqa: F401
from tests.test_host_loop import HEADERS, events
from tests.test_mentions import picker  # noqa: F401
from tests.test_multi_step import request
from tests.test_roles import DONE, ROUTING_TOOL, model  # noqa: F401
from tests.test_selection_capture import STATE, native, select_one

SELECTED = {"count": 1, "stamp": "selected", "document_id": "fake-document"}


def many(picker, count=21):
    rows = [{"id": 100 + i, "name": "Target", "category": "OST_Walls" if i else "OST_Doors",
             "level": "L1"} for i in range(count)]
    picker.update(elements=rows, selection=rows)
    state = {**SELECTED, "count": count}
    native(picker["fake"], {STATE: lambda _: dict(state)})
    return state


def rotate_pick():
    spec = spec_for("rotate_elements", angle=90, element_ids="93")
    for p in spec["parameters"]:
        if p["name"] == "angle":
            p.update(source="designer", evidence="angle=90", unit=None)
        else:
            p.update(source="tool", evidence="mention:element#93")
    return tool_turn(("propose_spec", {"spec": spec}))


def test_large_selection_allows_an_ordinary_chat_message(client, model, picker):
    state = many(picker)
    model.script("analyst", json_turn({"route": "chat", "facts_needed": [], "note": "Hello."}))
    _, frames = request(client, message="Hello", current_selection=state)
    report, = events(frames, "mentions")
    assert not report["resolved"] and report["current_selection"]["count"] == 21
    assert model.roles == ["analyst"]


@pytest.mark.parametrize("size", [1, 21])
@pytest.mark.parametrize("reattach", [False, True])
def test_only_this_messages_selection_is_an_auto_origin(client, model, picker, size, reattach):
    state = many(picker) if size > 1 else SELECTED
    if size == 1:
        select_one(picker)
    model.script("analyst", json_turn(ROUTING_TOOL), json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Ready."), rotate_pick() if size == 1 else rotate_turn())
    model.script("supervisor", json_turn(DONE))
    sid, _ = request(client, message="Remember these", current_selection=state)
    _, frames = request(client, session_id=sid, message="Rotate, angle=90", run="auto",
                        **({"current_selection": state} if reattach else {}))
    card, = events(frames, "spec")
    assert card["run"] == ("auto" if reattach else "confirm")
    assert bool(events(frames, "execution")) == reattach
    if reattach:
        assert events(frames, "execution")[0]["success"]
    else:
        assert card["run_reason"] == "unverified_evidence"


@pytest.mark.parametrize("count,extra,reference", [(20, [], False), (21, [], True),
    (20, [{"kind": "element", "id": 100, "document_id": "fake-document"}], False),
    (20, [{"kind": "level", "id": 1}], True)])
def test_combined_limit_and_reference_card(client, model, picker, count, extra, reference):
    state = many(picker, count)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", rotate_turn(), text_turn("Review."))
    sid, frames = request(client, message="Rotate, angle=90", current_selection=state, mentions=extra)
    report, = events(frames, "mentions")
    card, = events(frames, "spec")
    assert len(report["resolved"]) == (len(extra) if reference else count)
    if reference:
        ref = card["selection_reference"]
        assert ref == {**state, "categories": {"OST_Doors": 1, "OST_Walls": count - 1}}
        assert "leave element parameters empty" in str(model.requests).lower()
        for key in ("stamp", "document_id"):
            previous, state[key] = state[key], "different"
            result = client.post(B + "/spec/confirm", json={"session_id": sid, "spec": card["spec"]})
            assert result.status_code == 409 and result.json()["error"] == "selection_changed"
            state[key] = previous
        assert client.post(B + "/spec/confirm", json={"session_id": sid, "spec": card["spec"]}).is_success
    else:
        assert "selection_reference" not in card


def test_changed_reference_waits_before_auto_execution(client, model, picker):
    state = many(picker)
    reads = 0

    def change_after_message(_):
        nonlocal reads
        reads += 1
        return {**state, "stamp": "other" if reads > 2 else state["stamp"]}

    native(picker["fake"], {STATE: change_after_message})
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", rotate_turn())
    _, frames = request(client, message="Rotate, angle=90", current_selection=state, run="auto")
    card, = events(frames, "spec")
    assert card["run"] == "confirm" and card["run_reason"] == "selection_changed"
    assert not events(frames, "execution")
    assert not any("RotateElement" in r["params"].get("code", "") for r in picker["fake"].requests)


def test_attachment_excluded_from_answers_and_receipts(client, model, picker):
    select_one(picker)
    model.script("analyst", json_turn(ROUTING_TOOL))
    form = {"questions": [{"id": "angle", "text": "Angle?", "options": [{"label": "90", "value": 90},
                                                               {"label": "45", "value": 45}]}]}
    model.script("planner", tool_turn(("ask_designer", form)))
    sid, frames = request(client, message="Rotate something")
    q, = events(frames, "questions")
    for extra in ({"message": "90", "answers": {"ask_id": q["ask_id"], "items": [{"id": "angle", "label": "90", "value": 90}]}},
                  {"execution": {"success": True}}, {"message": "Picked", "selection_answer": q["ask_id"]},
                  {"message": "Hello", "bridge": False}):
        reply = client.post("/api/chat", headers=HEADERS, json={"session_id": sid, **extra, "current_selection": SELECTED})
        assert reply.status_code == 422 and reply.json()["error"] == "invalid_args"
    assert STATE not in [r["method"] for r in picker["fake"].requests]


def test_disabled_and_unresolved_are_omitted_with_reasons(client, model, picker, monkeypatch):
    from backend.api import chat
    state = many(picker, 3)
    candidates = chat.mention_candidates

    async def disable_one(*args, **kwargs):
        page = await candidates(*args, **kwargs)
        page["items"][0]["disabled"] = True
        picker["elements"] = picker["elements"][:2]  # Third id can no longer resolve.
        return page

    monkeypatch.setattr(chat, "mention_candidates", disable_one)
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Received."))
    _, frames = request(client, message="Inspect", current_selection=state)
    report, = events(frames, "mentions")
    assert [row["id"] for row in report["resolved"]] == [101]
    assert report["current_selection"]["count"] == 1
    assert report["current_selection"]["omitted"] == {"disabled": 1, "not_found": 1}


def test_unreadable_detail_page_keeps_the_elements_that_resolve(client, model, picker):
    state = many(picker, 3)
    picker["elements"] = picker["elements"][1:]
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Received."))
    _, frames = request(client, message="Inspect", current_selection=state)
    report, = events(frames, "mentions")
    assert {row["id"] for row in report["resolved"]} == {101, 102}
    assert report["current_selection"]["omitted"] == {"revit_error": 1}


def test_reference_has_bounded_counts_and_server_observed_size(client, model, picker):
    state = many(picker)
    for row in picker["elements"]:
        row["category"] = f"OST_Category{row['id']}"
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Received."))
    _, frames = request(client, message="Inspect", current_selection={**state, "count": 1})
    report, = events(frames, "mentions")
    ref = report["current_selection"]
    assert ref["count"] == 21 and ref["categories_partial"]
    assert len(ref["categories"]) == 20 and set(ref["categories"].values()) == {1}
    assert not report["resolved"] and "mention:element#" not in str(model.requests)


def test_mismatch_invalidates_cached_indicator_without_waiting(client, picker):
    state = many(picker)
    old = client.get(B + "/selection-state").json()
    state["stamp"] = "new"
    reply = client.post("/api/chat", headers=HEADERS, json={"message": "Inspect", "current_selection": old})
    assert reply.status_code == 409 and reply.json()["error"] == "selection_changed"
    assert client.get(B + "/selection-state").json()["stamp"] == "new"


def test_unavailable_state_sends_text_and_reports_omissions(client, model, picker):
    native(picker["fake"], {STATE: lambda _: {"invalid": True}})
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", text_turn("Received."))
    _, frames = request(client, message="Inspect", current_selection=SELECTED)
    report, = events(frames, "mentions")
    assert not report["resolved"] and report["current_selection"]["omitted"] == {"selection_unavailable": 1}
