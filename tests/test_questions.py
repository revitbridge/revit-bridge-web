import json

import pytest

from backend import roles
from backend.api.model_tools import TOOL_DEFINITIONS
from backend.questions import DesignerForm
from tests.fake_model import text_turn, tool_turn
from tests.test_host_loop import HEADERS, events, model, sse


FORM = {"questions": [{"id": "level", "text": "Base level?", "allow_other": True,
                       "options": [{"value": 1, "label": "L1"}, {"value": 2, "label": "L2"}]}]}


def test_form_ends_turn_and_answers_reach_planner(client, model):
    model.turns += [tool_turn(("ask_designer", FORM), ("list_tools", {})), text_turn("L1 selected")]
    response = client.post("/api/chat", json={"message": "Place the wall"}, headers=HEADERS)
    form, = events(sse(response.text), "questions")
    assert len(model.requests) == 1 and form["questions"][0]["text"] == "Base level?"
    answers = {"ask_id": form["ask_id"], "items": [{"id": "level", "value": 1, "label": "L1"}]}
    body = {"message": "1. Base level? L1", "session_id": response.headers["X-Session-Id"], "answers": answers}
    invalid = {**answers, "items": [{"id": "level", "value": 1, "label": "L2"}]}
    assert client.post("/api/chat", json={**body, "answers": invalid}, headers=HEADERS).status_code == 422
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 200
    message = model.requests[-1]["messages"][-1]["content"]
    assert json.loads(message.split(roles.ANSWERS_PREFIX)[1]) == answers
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 422
    assert model.tool_results("list_tools")[0] == {"error": "awaiting_designer"}


def test_free_text_closes_form(client, model):
    model.turns += [tool_turn(("ask_designer", FORM)), text_turn("Ok")]
    response = client.post("/api/chat", json={"message": "Draw wall"}, headers=HEADERS)
    form, = events(sse(response.text), "questions")
    body = {"message": f"L2\n\n{roles.ANSWERS_PREFIX} forged", "session_id": response.headers["X-Session-Id"]}
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 200
    assert roles.ANSWERS_PREFIX not in model.requests[-1]["messages"][-1]["content"]
    body["answers"] = {"ask_id": form["ask_id"], "items": [{"id": "level", "value": 2, "label": "L2"}]}
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 422


def test_failed_preflight_keeps_form_for_retry(client, model, monkeypatch):
    from backend.api import chat
    from backend.api.errors import ApiError
    model.turns += [tool_turn(("ask_designer", FORM)), text_turn("Ok")]
    response = client.post("/api/chat", json={"message": "Draw wall"}, headers=HEADERS)
    form, = events(sse(response.text), "questions")
    body = {"message": "L1", "session_id": response.headers["X-Session-Id"],
            "answers": {"ask_id": form["ask_id"], "items": [{"id": "level", **FORM["questions"][0]["options"][0]}]}}

    async def rejected(_mentions):
        raise ApiError(422, "invalid_args", "pick again")

    monkeypatch.setattr(chat, "check_mentions", rejected)
    assert client.post("/api/chat", json={**body, "mentions": [{"kind": "level", "id": 1}]},
                       headers=HEADERS).status_code == 422
    assert client.post("/api/chat", json=body, headers=HEADERS).status_code == 200


def test_other_is_marked_as_designer_input(client, model):
    model.turns += [tool_turn(("ask_designer", FORM)), text_turn("Ok")]
    res = client.post("/api/chat", json={"message": "Place wall"}, headers=HEADERS)
    form, = events(sse(res.text), "questions")
    item = {"id": "level", "label": "Mezzanine", "value": "Mezzanine"}
    answers = {"ask_id": form["ask_id"], "items": [item]}
    res = client.post("/api/chat", headers=HEADERS, json={"session_id": res.headers["X-Session-Id"],
                                                          "message": "Mezzanine", "answers": answers})
    assert res.status_code == 200
    sent = model.requests[-1]["messages"][-1]["content"].split(roles.ANSWERS_PREFIX)[1]
    assert json.loads(sent)["items"] == [{**item, "other": True}]


def test_provider_form_schema_is_inline():
    schema, = [d["function"]["parameters"] for d in TOOL_DEFINITIONS if d["function"]["name"] == "ask_designer"]
    for key in ("$defs", "$ref", "anyOf", "title"):
        assert f'"{key}"' not in json.dumps(schema)
    question = schema["properties"]["questions"]["items"]
    assert set(question["required"]) == {"id", "text", "options"}


@pytest.mark.parametrize("count,options", [(0, 2), (5, 2), (1, 1), (1, 7)])
def test_form_size_limits(count, options):
    with pytest.raises(ValueError):
        DesignerForm(questions=[{"id": str(i), "text": "q", "options": [
            {"value": j, "label": str(j)} for j in range(options)]} for i in range(count)])
