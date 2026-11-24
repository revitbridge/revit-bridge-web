"""The designer's picks (@level, @type, @link): GET /mentions, the chat's ``mentions`` and
their event, the picks a session keeps, and the ``unknown_mention`` check of a spec on the
model's ``propose_spec`` and on the HTTP spec routes. Every listing, check and block text is
the package's; these tests pin what the host does with them."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from revit_bridge.mentions import mention_block
from revit_bridge.spec.models import TaskSpec

from backend.api import chat as chat_module
from backend.mention_check import UNKNOWN_MENTION, mention_errors
from backend.session import MAX_SESSION_PICKS, get_session_store
from tests.fake_model import text_turn, tool_turn
from tests.fake_revit import FakeRevit, model_handler
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_deploy_mode import LOCAL
from tests.test_devices import addin_reply, connect, serve
from tests.test_host_loop import HEADERS, events, model, sse, tokens  # noqa: F401 - the model fixture

# The open model behind the fake add-in. The levels are the snapshot's (tests/fake_revit.py),
# so a spec bound to a picked level reconciles.
LEVEL_ROWS = [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}, {"Id": 2, "Name": "L2", "ElevationMm": 3500.0}]
TYPE_ROWS = [
    {"FamilyTypeId": 401, "UniqueId": "u-401", "FamilyName": "Basic Wall", "TypeName": "Generic - 200mm",
     "Category": "Walls", "BuiltInCategory": "OST_Walls"},
    {"FamilyTypeId": 402, "UniqueId": "u-402", "FamilyName": "Basic Wall", "TypeName": "Exterior - Brick",
     "Category": "Walls", "BuiltInCategory": "OST_Walls"},
    {"FamilyTypeId": 451, "UniqueId": "u-451", "FamilyName": "Concrete-Rectangular-Column",
     "TypeName": "300 x 450mm", "Category": "Structural Columns", "BuiltInCategory": "OST_StructuralColumns"},
]
LINK_ROWS = [
    {"Id": 501, "Name": "Structure.rvt : 1 : location <Not Shared>", "TypeId": 500, "TypeName": "Structure.rvt",
     "Loaded": True, "DocumentTitle": "Structure", "Origin": {"X": 0.0, "Y": 0.0, "Z": 0.0}, "Rotated": False},
    {"Id": 502, "Name": "MEP.rvt : 1 : location <Not Shared>", "TypeId": 503, "TypeName": "MEP.rvt",
     "Loaded": False, "DocumentTitle": None, "Origin": None, "Rotated": False},
]
CATEGORY_ROWS = [{"Category": "OST_StructuralColumns", "DisplayName": "Structural Columns", "Count": 1},
                 {"Category": "OST_Walls", "DisplayName": "Walls", "Count": 2}]

L2 = {"kind": "level", "id": 2, "name": "L2", "label": "L2", "detail": "3500 mm", "elevation_mm": 3500.0}
GENERIC = {"kind": "type", "id": 401, "name": "Generic - 200mm", "label": "Basic Wall: Generic - 200mm",
           "detail": "Walls", "category": "OST_Walls", "family": "Basic Wall"}


def picker_handler(state: dict):
    """The add-in of a 0.9-era model: it offers list_model_items (levels, links, types,
    type_categories, by category and by ids) and answers the rest as tests/fake_revit.py does."""
    base = model_handler()

    def handler(request: dict) -> dict:
        method, rid, params = request.get("method"), request.get("id"), request.get("params") or {}
        if method == "get_bridge_capabilities":
            return FakeRevit.ok(rid, {"protocol": "1.0", "methods": [
                "list_model_items", "get_available_family_types", "send_code_to_revit"]})
        if method == "list_model_items":
            state["calls"].append(params)
            rows = {"levels": state["levels"], "links": LINK_ROWS, "types": TYPE_ROWS,
                    "type_categories": CATEGORY_ROWS}[params["kind"]]
            if params.get("category"):
                rows = [r for r in rows if r.get("BuiltInCategory") == params["category"]]
            if "ids" in params:
                rows = [r for r in rows if r.get("Id", r.get("FamilyTypeId")) in params["ids"]]
            return FakeRevit.ok(rid, {"Success": True, "Kind": params["kind"], "Items": rows, "Total": len(rows),
                                      "Truncated": False, "Warnings": [], "Error": None})
        code = params.get("code", "") if method == "send_code_to_revit" else ""
        if "OfClass(typeof(Level))" in code and "return levels;" in code and "CategoryNames" not in code:
            return FakeRevit.code_result(rid, state["levels"])
        return base(request)
    return handler


@pytest.fixture
def picker(env):
    """The fake add-in above, reachable over TCP; ``state["levels"]`` can be changed."""
    state: dict = {"levels": [dict(row) for row in LEVEL_ROWS], "calls": []}
    with FakeRevit(picker_handler(state)) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        state["fake"] = fake
        yield state


def pick(kind: str, item_id, label: str | None = None) -> dict:
    out = {"kind": kind, "id": item_id}
    if label is not None:
        out["label"] = label
    return out


def wall_spec(level: object = "L2", evidence: str = "mention:level#2", source: str = "tool") -> dict:
    """create_wall with every other parameter an answered question, the level bound as given."""
    spec = spec_for("create_wall", **WALL)
    for binding in spec["parameters"]:
        if binding["name"] == "level_name":
            binding.update(value=level, unit=None, source=source, evidence=evidence)
    return spec


def chat(client, message: str, mentions=None, session_id: str | None = None, **extra):
    body = {"message": message, **extra}
    if mentions is not None:
        body["mentions"] = mentions
    if session_id:
        body["session_id"] = session_id
    return client.post("/api/chat", json=body, headers=HEADERS)


# -- GET /mentions ------------------------------------------------------------------------

def test_levels_are_listed_as_the_package_lists_them(picker, client):
    resp = client.get(f"{B}/mentions", params={"kind": "level"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"kind": "level", "total": 2, "truncated": False, "items": [
        {"kind": "level", "id": 1, "name": "L1", "label": "L1", "detail": "0 mm"},
        {"kind": "level", "id": 2, "name": "L2", "label": "L2", "detail": "3500 mm"}]}
    # q filters (case-insensitive), limit cuts and says so.
    assert [i["id"] for i in client.get(f"{B}/mentions", params={"kind": "level", "q": "l2"}).json()["items"]] == [2]
    cut = client.get(f"{B}/mentions", params={"kind": "level", "limit": 1}).json()
    assert [i["id"] for i in cut["items"]] == [1] and cut["total"] == 2 and cut["truncated"] is True


def test_types_come_by_category_and_the_categories_first(picker, client):
    categories = client.get(f"{B}/mentions", params={"kind": "type"}).json()
    assert categories["kind"] == "type" and categories["total"] == 2
    assert [(i["kind"], i["id"], i["label"]) for i in categories["items"]] == [
        ("category", "OST_StructuralColumns", "Structural Columns"), ("category", "OST_Walls", "Walls")]
    walls = client.get(f"{B}/mentions", params={"kind": "type", "category": "OST_Walls"}).json()
    assert walls["items"] == [
        GENERIC,
        {"kind": "type", "id": 402, "name": "Exterior - Brick", "label": "Basic Wall: Exterior - Brick",
         "detail": "Walls", "category": "OST_Walls", "family": "Basic Wall"}]
    brick = client.get(f"{B}/mentions", params={"kind": "type", "category": "OST_Walls", "q": "brick"}).json()
    assert [i["id"] for i in brick["items"]] == [402]
    assert {"kind": "types", "category": "OST_Walls"} in picker["calls"]


def test_a_link_that_is_not_loaded_is_listed_but_not_selectable(picker, client):
    links = client.get(f"{B}/mentions", params={"kind": "link"}).json()
    loaded, unloaded = links["items"]
    assert loaded == {"kind": "link", "id": 501, "name": LINK_ROWS[0]["Name"], "label": "Structure.rvt",
                      "detail": "Structure"}
    assert unloaded["id"] == 502 and unloaded["disabled"] is True and unloaded["reason"] == "not_loaded"


@pytest.mark.parametrize("params, error", [
    ({"kind": "view"}, "unknown_kind"),
    ({"kind": "type", "category": "Walls"}, "invalid_args"),
    ({"kind": "type", "category": "OST_Walls; drop"}, "invalid_args"),
    ({"kind": "level", "category": "OST_Walls"}, "invalid_args"),
    ({}, "invalid_args"),                               # kind is required
    ({"kind": "level", "limit": "lots"}, "invalid_args"),
])
def test_a_kind_or_category_the_package_refuses_is_422(picker, client, params, error):
    resp = client.get(f"{B}/mentions", params=params)
    assert resp.status_code == 422 and resp.json()["error"] == error, resp.text
    if error == "unknown_kind":
        assert resp.json()["kinds"] == ["level", "type", "link"]


def test_no_revit_is_503(client):
    resp = client.get(f"{B}/mentions", params={"kind": "level"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "revit_unreachable" and resp.json()["kind"] == "level"


def test_the_route_is_in_the_contract(client):
    operation = client.get("/openapi.json").json()["paths"][f"{B}/mentions"]["get"]
    assert {"403", "422", "500", "503"} <= set(operation["responses"])
    assert [p["name"] for p in operation["parameters"]] == ["kind", "category", "q", "limit"]


def test_a_cloud_host_applies_the_device_rules(client, device):
    """The same as /query: a wrong device key is 403, a device without its add-in 503, and a
    connected device answers the listing over its socket."""
    headers, device_id, token = device
    wrong = client.get(f"{B}/mentions", params={"kind": "level"},
                       headers={"X-Device-Id": device_id, "X-Device-Key": "wrong"})
    assert wrong.status_code == 403 and wrong.json()["error"] == "invalid_device_key"
    absent = client.get(f"{B}/mentions", params={"kind": "level"}, headers=headers)
    assert absent.status_code == 503 and absent.json()["error"] == "revit_unreachable"

    outcome: dict = {}
    with connect(client, device_id, token) as ws:
        thread = threading.Thread(target=lambda: outcome.update(
            resp=client.get(f"{B}/mentions", params={"kind": "level"}, headers=headers)))
        thread.start()
        seen = serve(ws, 1, lambda r: addin_reply(r, LEVEL_ROWS))
        thread.join(timeout=10)
    assert seen[0]["method"] == "send_code_to_revit" and "return levels;" in seen[0]["params"]["code"]
    assert outcome["resp"].status_code == 200 and [i["name"] for i in outcome["resp"].json()["items"]] == ["L1", "L2"]


def test_a_local_host_serves_the_listing(local_env, make_client, picker):
    with make_client(**LOCAL) as client:
        resp = client.get(f"{B}/mentions", params={"kind": "level"})
    assert resp.status_code == 200 and resp.json()["total"] == 2


# -- the chat's mentions ------------------------------------------------------------------

def test_verified_picks_reach_the_model_as_the_package_block(picker, client, model):
    model.turns.append(text_turn("A wall on L2 of that type."))
    resp = chat(client, "Put a wall on @level:L2 with @type:Generic - 200mm",
                [pick("level", 2, "the page's own label"), pick("type", 401)])
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)
    # The event comes first, before the stage; the package's items as they are.
    assert frames[0] == ("mentions", {"resolved": [L2, GENERIC], "not_found": [], "invalid": [], "unresolved": []})
    assert frames[1][0] == "stage" and frames[1][1]["status"] == "start" and frames[-1] == ("done", None)
    # The block follows the designer's words in the user message; the system prompt is the session's.
    block = mention_block([L2, GENERIC], "en")
    assert block.startswith("Designer's selections") and 'level "L2" (id 2, 3500 mm)' in block
    sent = model.requests[0]["messages"]
    assert sent[0] == {"role": "system", "content": chat_module.build_system_prompt(True)}
    assert sent[-1] == {"role": "user", "content": f"Put a wall on @level:L2 with @type:Generic - 200mm\n\n{block}"}
    assert "the page's own label" not in str(model.requests)
    session = get_session_store().get(resp.headers["X-Session-Id"])
    assert session.picks_for("local") == {"mention:level#2": L2, "mention:type#401": GENERIC}
    assert session.picks_for("dev_other") == {}


def test_picks_not_found_or_not_valid_are_not_sent(picker, client, model):
    model.turns.append(text_turn("ok"))
    picks = [pick("level", 2), pick("level", 99), pick("link", 502), pick("level", 0), pick("view", 5),
             pick("link", 501)]
    frames = sse(chat(client, "these", picks).text)
    event = events(frames, "mentions")[0]
    assert [i["id"] for i in event["resolved"]] == [2, 501]
    assert event["not_found"] == [{"kind": "level", "id": 99, "reason": "not_found"},
                                  {"kind": "link", "id": 502, "reason": "not_loaded"}]
    assert event["invalid"] == [{"kind": "level", "id": 0, "reason": "invalid"},
                                {"kind": "view", "id": 5, "reason": "invalid"}]
    assert event["unresolved"] == []
    user = model.requests[0]["messages"][-1]["content"]
    assert user == "these\n\n" + mention_block(event["resolved"], "en")
    for absent in ("99", "502", "MEP", "view"):
        assert absent not in user, absent


def test_no_pick_verified_adds_no_block(picker, client, model):
    model.turns.append(text_turn("ok"))
    resp = chat(client, "level ninety-nine", [pick("level", 99)])
    assert events(sse(resp.text), "mentions")[0]["resolved"] == []
    assert model.requests[0]["messages"][-1] == {"role": "user", "content": "level ninety-nine"}
    assert get_session_store().get(resp.headers["X-Session-Id"]).picks == {}


def test_without_revit_the_turn_runs_and_every_pick_is_unresolved(client, model):
    model.turns.append(text_turn("I could not check the level."))
    resp = chat(client, "a wall on @level:L2", [pick("level", 2), pick("level", 0)])
    assert resp.status_code == 200
    frames = sse(resp.text)
    assert frames[0] == ("mentions", {"resolved": [], "not_found": [], "invalid": [], "unresolved": [
        {"kind": "level", "id": 2, "reason": "revit_unreachable"},
        {"kind": "level", "id": 0, "reason": "revit_unreachable"}]})
    assert tokens(frames) == "I could not check the level." and frames[-1] == ("done", None)
    assert model.requests[0]["messages"][-1] == {"role": "user", "content": "a wall on @level:L2"}


def test_an_add_in_that_cannot_say_is_never_read_as_not_found(picker, client, model, monkeypatch):
    from revit_bridge.snapshot import RevitQueryExecutor
    from revit_bridge.snapshot.query import RevitQueryError

    async def unreadable(self, ids):
        raise RevitQueryError("the add-in's capabilities could not be read (timeout)")

    monkeypatch.setattr(RevitQueryExecutor, "get_levels_by_id", unreadable)
    model.turns.append(text_turn("ok"))
    event = events(sse(chat(client, "x", [pick("level", 2)]).text), "mentions")[0]
    assert event["not_found"] == [] and event["unresolved"] == [{"kind": "level", "id": 2, "reason": "revit_error"}]


def test_a_session_keeps_its_picks_for_later_turns(picker, client, model):
    model.turns.append(text_turn("Which length?"))
    first = chat(client, "a wall on @level:L2", [pick("level", 2)])
    sid = first.headers["X-Session-Id"]
    # A later turn without picks: no event, no block - and a spec may still bind the pick.
    model.turns += [tool_turn(("propose_spec", {"spec": wall_spec()})), text_turn("The card is below.")]
    second = chat(client, "5000 long", session_id=sid)
    frames = sse(second.text)
    assert events(frames, "mentions") == []
    verdict = model.tool_results("propose_spec")[0]
    assert verdict["accepted"] is True and verdict["errors"] == [], verdict
    # The first turn's message, block included, stays as it was in the history; one system prompt.
    turn_one = "a wall on @level:L2\n\n" + mention_block([L2], "en")
    assert model.requests[1]["messages"][1] == {"role": "user", "content": turn_one}
    assert model.requests[1]["messages"][2:] == [{"role": "assistant", "content": "Which length?"},
                                                 {"role": "user", "content": "5000 long"}]
    assert len({r["messages"][0]["content"] for r in model.requests}) == 1
    assert get_session_store().get(sid).picks_for("local") == {"mention:level#2": L2}


def test_a_pick_verified_again_takes_its_new_reading(picker, client, model):
    model.turns += [text_turn("ok"), text_turn("ok")]
    sid = chat(client, "@level:L2", [pick("level", 2)]).headers["X-Session-Id"]
    picker["levels"][1]["Name"] = "Level 2 (renamed)"
    chat(client, "@level:Level 2 (renamed)", [pick("level", 2)], session_id=sid)
    assert get_session_store().get(sid).picks_for("local")["mention:level#2"]["name"] == "Level 2 (renamed)"


def test_a_session_keeps_a_bounded_number_of_picks():
    from backend.session import Session
    session = Session(session_id="s")
    session.remember_picks("local", [{"kind": "level", "id": i, "name": f"L{i}"} for i in range(1, MAX_SESSION_PICKS + 6)])
    picks = session.picks_for("local")
    assert len(picks) == MAX_SESSION_PICKS and "mention:level#1" not in picks
    assert list(picks)[-1] == f"mention:level#{MAX_SESSION_PICKS + 5}"


def test_twenty_picks_are_taken_and_twenty_one_refused(picker, client, model):
    model.turns.append(text_turn("ok"))
    assert chat(client, "many", [pick("level", 2)] * 20).status_code == 200
    resp = chat(client, "too many", [pick("level", 2)] * 21)
    assert resp.status_code == 422 and len(model.requests) == 1


@pytest.mark.parametrize("body", [
    {"message": "x", "mentions": [{"kind": "level", "id": 2, "name": "L2"}]},        # unknown field
    {"message": "x", "mentions": [{"kind": "level", "id": "2"}]},                    # an id is a number
    {"message": "x", "mentions": [{"kind": "level", "id": True}]},
    {"message": "x", "mentions": [{"kind": "level"}]},
    {"message": "x", "mentions": [{"kind": "level", "id": 2, "label": "x" * 501}]},
    {"execution": {"success": True}, "mentions": [{"kind": "level", "id": 2}]},      # picks go with a message
    {"message": "x", "bridge": False, "mentions": [{"kind": "level", "id": 2}]},     # and with the bridge
])
def test_a_request_with_bad_picks_is_refused_before_anything_runs(picker, client, model, body):
    resp = client.post("/api/chat", json=body, headers=HEADERS)
    assert resp.status_code == 422, resp.text
    assert model.requests == [] and picker["calls"] == []


def test_no_picks_no_event_and_the_message_as_typed(picker, client, model):
    model.turns += [text_turn("one"), text_turn("two")]
    for mentions in (None, []):
        frames = sse(chat(client, "plain words", mentions).text)
        assert [e for e, _ in frames if e == "mentions"] == []
        assert frames[0][0] == "stage"
    assert [r["messages"][-1] for r in model.requests] == [{"role": "user", "content": "plain words"}] * 2
    assert picker["calls"] == []


# -- unknown_mention: the model's propose_spec ----------------------------------------------

def propose(client, model, spec: dict, mentions=None, session_id=None):
    model.turns += [tool_turn(("propose_spec", {"spec": spec})), text_turn("done")]
    resp = chat(client, "a wall", mentions, session_id)
    return resp, model.tool_results("propose_spec")[-1]


def test_a_real_pick_is_accepted(picker, client, model):
    resp, verdict = propose(client, model, wall_spec(), [pick("level", 2)])
    assert verdict["accepted"] is True and verdict["errors"] == []
    assert events(sse(resp.text), "spec")[0]["errors"] == []


@pytest.mark.parametrize("spec, why", [
    (wall_spec(evidence="mention:level#1"), "is not a pick"),       # L1 exists, but was not picked
    (wall_spec(evidence="mention:level#99"), "is not a pick"),
    (wall_spec(evidence="mention:level#02"), "is not mention:<kind>#<id> exactly"),   # the canonical form only
    (wall_spec(evidence="Mention:level#2"), "is not mention:<kind>#<id> exactly"),
    (wall_spec(evidence="mention:type#2"), "is not a pick"),
    (wall_spec(level="L1"), "must be exactly that"),               # the pick is L2
    (wall_spec(source="answer"), "has source tool"),               # validate_spec takes answer here
])
def test_a_forged_or_wrong_pick_is_unknown_mention(picker, client, model, spec, why):
    resp, verdict = propose(client, model, spec, [pick("level", 2)])
    assert verdict["accepted"] is False
    refused = [e for e in verdict["errors"] if e["code"] == UNKNOWN_MENTION]
    assert len(refused) == 1 and refused[0]["param"] == "level_name" and why in refused[0]["message"]
    # The page sees the card with the same errors.
    assert events(sse(resp.text), "spec")[0]["errors"] == verdict["errors"]


def test_a_pick_of_another_session_is_unknown_mention(picker, client, model):
    model.turns.append(text_turn("ok"))
    chat(client, "@level:L2 here", [pick("level", 2)])                     # session A picks L2
    resp, verdict = propose(client, model, wall_spec())                     # session B never did
    assert verdict["accepted"] is False and [e["code"] for e in verdict["errors"]] == [UNKNOWN_MENTION]


def test_a_spec_without_mention_evidence_is_not_touched(picker, client, model):
    _, verdict = propose(client, model, spec_for("create_wall", **WALL))
    assert verdict["accepted"] is True and verdict["errors"] == []


# -- unknown_mention: the HTTP spec routes --------------------------------------------------

def session_with_pick(client, model) -> str:
    model.turns.append(text_turn("ok"))
    return chat(client, "@level:L2", [pick("level", 2)]).headers["X-Session-Id"]


def test_confirm_takes_a_real_pick_with_its_session(picker, client, model):
    sid = session_with_pick(client, model)
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 200, resp.text
    assert resp.json()["token"]
    assert client.post(f"{B}/spec/reconcile", json={"spec": wall_spec(), "session_id": sid}).json()["ready"] is True


@pytest.mark.parametrize("spec, session", [
    (wall_spec(evidence="mention:level#1"), "own"),        # forged
    (wall_spec(level="L1"), "own"),                         # wrong value
    (wall_spec(source="answer"), "own"),                    # wrong source
    (wall_spec(), "other"),                                 # another session's pick
    (wall_spec(), None),                                    # no session
    (wall_spec(), "gone"),                                  # a session that is not there
])
def test_the_spec_routes_refuse_an_unknown_mention(picker, client, model, spec, session):
    own = session_with_pick(client, model)
    model.turns.append(text_turn("ok"))
    other = chat(client, "nothing picked").headers["X-Session-Id"]
    body = {"spec": spec}
    if session is not None:
        body["session_id"] = {"own": own, "other": other, "gone": "0" * 32}[session]
    for route in ("confirm", "reconcile"):
        resp = client.post(f"{B}/spec/{route}", json=body)
        assert resp.status_code == 422, (route, resp.text)
        out = resp.json()
        assert out["error"] == "invalid_spec", route
        assert [(e["code"], e["param"]) for e in out["errors"]] == [(UNKNOWN_MENTION, "level_name")], route
    assert "level_name" in client.post(f"{B}/spec/reconcile", json=body).json()["message"]


def test_the_spec_routes_take_the_session_id_in_the_contract(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    for name in ("ReconcileRequest", "ConfirmRequest"):
        assert "session_id" in schemas[name]["properties"] and "session_id" not in schemas[name].get("required", [])


# -- second round: stale and foreign picks, the confirm-time reading, timeouts, kinds ---------

def test_a_pick_a_later_turn_finds_gone_leaves_the_session(picker, client, model):
    sid = session_with_pick(client, model)
    picker["levels"] = [dict(LEVEL_ROWS[0])]                            # L2 deleted in Revit
    model.turns += [tool_turn(("propose_spec", {"spec": wall_spec()})), text_turn("done")]
    frames = sse(chat(client, "again @level:L2", [pick("level", 2)], session_id=sid).text)
    assert events(frames, "mentions")[0]["not_found"] == [{"kind": "level", "id": 2, "reason": "not_found"}]
    assert get_session_store().get(sid).picks == {}
    verdict = model.tool_results("propose_spec")[0]
    assert verdict["accepted"] is False and "is not a pick" in verdict["errors"][0]["message"]


def test_a_pick_of_another_device_does_not_count(picker, client, model):
    """Picks are kept per device scope: one read from another device's model binds nothing here."""
    sid = session_with_pick(client, model)
    session = get_session_store().get(sid)
    session.picks = {("dev_elsewhere", evidence): item for (_, evidence), item in session.picks.items()}
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == UNKNOWN_MENTION
    _, verdict = propose(client, model, wall_spec(), session_id=sid)
    assert verdict["accepted"] is False and [e["code"] for e in verdict["errors"]] == [UNKNOWN_MENTION]


def test_a_request_driving_a_device_does_not_see_the_local_picks(picker, client, model, device):
    headers, device_id, _ = device
    sid = session_with_pick(client, model)                              # picked on the local add-in
    assert get_session_store().get(sid).picks_for("local")
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid}, headers=headers)
    assert resp.status_code == 422 and resp.json()["errors"][0]["code"] == UNKNOWN_MENTION
    assert get_session_store().get(sid).picks_for(device_id) == {}


def test_confirm_reads_the_picks_again_gone(picker, client, model):
    sid = session_with_pick(client, model)
    picker["levels"] = [dict(LEVEL_ROWS[0])]                            # L2 deleted after the pick
    picker["calls"].clear()
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 422, resp.text
    error = resp.json()["errors"][0]
    assert (error["code"], error["param"]) == (UNKNOWN_MENTION, "level_name")
    assert "no longer in the open model" in error["message"]
    assert picker["calls"] == [{"kind": "levels", "ids": [2]}]           # one read, by id
    assert get_session_store().get(sid).picks == {}                     # and the session lets it go


def test_confirm_reads_the_picks_again_renamed(picker, client, model):
    sid = session_with_pick(client, model)
    picker["levels"][1]["Name"] = "L2 (renamed)"
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 422 and "is now 'L2 (renamed)'" in resp.json()["errors"][0]["message"]
    # The session takes the new reading: the old name is refused, a spec bound to the new one is confirmed.
    assert get_session_store().get(sid).picks_for("local")["mention:level#2"]["name"] == "L2 (renamed)"
    assert client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid}).status_code == 422
    renamed = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(level="L2 (renamed)"), "session_id": sid})
    assert renamed.status_code == 200, renamed.text


def test_confirm_without_revit_to_read_the_picks_is_503(picker, client, model, monkeypatch):
    from backend.api import bridge
    from backend.api.errors import revit_unreachable
    sid = session_with_pick(client, model)

    async def no_addin():
        raise revit_unreachable(ConnectionRefusedError("refused"), endpoint="127.0.0.1:1")

    monkeypatch.setattr(bridge, "get_revit_client", no_addin)
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 503 and resp.json()["error"] == "revit_unreachable"
    # A spec that binds no pick does not need Revit to be confirmed.
    plain = client.post(f"{B}/spec/confirm", json={"spec": spec_for("create_wall", **WALL), "session_id": sid})
    assert plain.status_code == 200, plain.text


def test_confirm_that_cannot_read_the_picks_in_time_or_at_all(picker, client, model, monkeypatch):
    import asyncio

    from revit_bridge.snapshot.query import RevitQueryError

    from backend.api import bridge
    sid = session_with_pick(client, model)

    async def slow(executor, wanted):
        await asyncio.sleep(5)

    monkeypatch.setattr(bridge, "PICK_RECHECK_TIMEOUT", 0.05)
    monkeypatch.setattr(bridge, "resolve_mentions", slow)
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 504 and resp.json()["error"] == "revit_timeout"

    async def cannot_say(executor, wanted):
        raise RevitQueryError("the add-in's capabilities could not be read (timeout)")

    monkeypatch.setattr(bridge, "resolve_mentions", cannot_say)
    resp = client.post(f"{B}/spec/confirm", json={"spec": wall_spec(), "session_id": sid})
    assert resp.status_code == 503 and resp.json()["error"] == "revit_error"
    assert get_session_store().get(sid).picks_for("local") == {"mention:level#2": L2}   # nothing dropped


def test_the_check_of_a_message_s_picks_times_out_and_the_turn_runs(picker, client, model, monkeypatch):
    import asyncio

    async def slow(executor, wanted):
        await asyncio.sleep(5)

    monkeypatch.setattr(chat_module, "MENTION_CHECK_TIMEOUT", 0.05)
    monkeypatch.setattr(chat_module, "resolve_mentions", slow)
    model.turns.append(text_turn("I could not check them."))
    frames = sse(chat(client, "@level:L2 and @type:x", [pick("level", 2), pick("type", 401)]).text)
    assert frames[0] == ("mentions", {"resolved": [], "not_found": [], "invalid": [], "unresolved": [
        {"kind": "level", "id": 2, "reason": "timeout"}, {"kind": "type", "id": 401, "reason": "timeout"}]})
    assert tokens(frames) == "I could not check them." and frames[-1] == ("done", None)
    assert model.requests[0]["messages"][-1] == {"role": "user", "content": "@level:L2 and @type:x"}


def test_a_pick_of_the_wrong_kind_is_unknown_mention(picker, client, model):
    spec = wall_spec(level="Generic - 200mm", evidence="mention:type#401")
    _, verdict = propose(client, model, spec, [pick("level", 2), pick("type", 401)])
    assert verdict["accepted"] is False
    assert [(e["code"], e["param"]) for e in verdict["errors"]] == [(UNKNOWN_MENTION, "level_name")]
    assert "takes a level" in verdict["errors"][0]["message"]


def test_the_model_s_reconcile_runs_the_pick_check(picker, client, model):
    model.turns += [tool_turn(("reconcile", {"spec": wall_spec(evidence="mention:level#1")})),
                    tool_turn(("reconcile", {"spec": wall_spec()})), text_turn("done")]
    chat(client, "a wall on @level:L2", [pick("level", 2)])
    forged, real = model.tool_results("reconcile")
    assert forged["error"] == "invalid_spec" and "level_name" in forged["message"]
    assert [(e["code"], e["param"]) for e in forged["errors"]] == [(UNKNOWN_MENTION, "level_name")]
    assert real["ready"] is True and "error" not in real


def test_confirm_documents_its_revit_errors(client):
    responses = client.get("/openapi.json").json()["paths"][f"{B}/spec/confirm"]["post"]["responses"]
    assert {"422", "429", "503", "504"} <= set(responses)


# -- the rule itself ------------------------------------------------------------------------

PACK = SimpleNamespace(parameters=[
    {"name": "level_name", "type": "string", "source": "tool:levels", "choices_from": "levels"},
    {"name": "wall_type", "type": "string", "source": "tool:family_types"},
    {"name": "walls", "type": "string", "source": "tool:family_types", "choices_from": "family_types:OST_Walls"},
    {"name": "floor_type", "type": "string", "source": "tool:floor_types", "choices_from": "floor_types"},
    {"name": "host", "type": "int", "source": "tool:elements", "choices_from": "elements:OST_Walls"},
    {"name": "label", "type": "string", "source": "designer"},
    {"name": "height", "type": "double", "source": "default", "default": 3000},
])
LINK = {"kind": "link", "id": 501, "name": "Structure.rvt : 1", "label": "Structure.rvt"}
UNLABELLED = {"kind": "link", "id": 503, "name": "Arch.rvt : 1", "label": ""}
COLUMN = {"kind": "type", "id": 451, "name": "300 x 450mm", "label": "Concrete: 300 x 450mm",
          "detail": "Structural Columns", "category": "OST_StructuralColumns", "family": "Concrete"}
NO_CATEGORY = {"kind": "type", "id": 460, "name": "Thin", "label": "Thin", "detail": "Walls"}
PICKS = {"mention:level#2": L2, "mention:type#401": GENERIC, "mention:link#501": LINK,
         "mention:link#503": UNLABELLED, "mention:type#451": COLUMN, "mention:type#460": NO_CATEGORY}


def bound(name: str, value, source: str, evidence: str) -> TaskSpec:
    return TaskSpec.model_validate({"task": "t", "action": {"kind": "run_tool", "tool": "x"}, "parameters": [
        {"name": name, "value": value, "source": source, "evidence": evidence}]})


@pytest.mark.parametrize("name, value, source, evidence, ok", [
    ("level_name", "L2", "tool", "mention:level#2", True),         # choices_from: tool
    ("level_name", "L2", "answer", "mention:level#2", False),
    ("level_name", "L2", "designer", "mention:level#2", False),
    ("wall_type", "Generic - 200mm", "tool", "mention:type#401", True),   # a tool:* source: tool
    ("wall_type", "Basic Wall: Generic - 200mm", "tool", "mention:type#401", False),  # the name, not the label
    ("label", "L2", "designer", "mention:level#2", True),          # a designer parameter: designer
    ("label", "L2", "answer", "mention:level#2", False),
    ("label", "L2", "preference", "mention:level#2", False),
    ("height", "L2", "default", "mention:level#2", False),         # any other parameter: designer
    ("height", "L2", "designer", "mention:level#2", True),
    ("not_in_pack", "L2", "designer", "mention:level#2", True),
    ("not_in_pack", "L2", "tool", "mention:level#2", False),
    # a link binds its name as the package's block shows it: the label, else the name
    ("label", "Structure.rvt", "designer", "mention:link#501", True),
    ("label", "Structure.rvt : 1", "designer", "mention:link#501", False),
    ("label", 501, "designer", "mention:link#501", False),
    ("label", "501", "designer", "mention:link#501", False),
    ("label", True, "designer", "mention:link#501", False),
    ("label", "Arch.rvt : 1", "designer", "mention:link#503", True),
    # the kind fits the parameter as the pack declares it
    ("level_name", "Generic - 200mm", "tool", "mention:type#401", False),     # levels: a level
    ("level_name", "Structure.rvt", "tool", "mention:link#501", False),
    ("wall_type", "L2", "tool", "mention:level#2", False),                    # tool:family_types: a type
    ("wall_type", "Structure.rvt", "tool", "mention:link#501", False),
    ("floor_type", "L2", "tool", "mention:level#2", False),                   # floor_types: a type
    ("floor_type", "Generic - 200mm", "tool", "mention:type#401", True),      # (no category to compare)
    ("walls", "Generic - 200mm", "tool", "mention:type#401", True),           # family_types:OST_Walls
    ("walls", "300 x 450mm", "tool", "mention:type#451", False),              # a column type
    ("walls", "Thin", "tool", "mention:type#460", True),                      # no category known: not refused
    ("host", "L2", "tool", "mention:level#2", True),                          # elements: the pack says nothing
    ("host", "Structure.rvt", "tool", "mention:link#501", True),
    # evidence that looks like a pick must be one exactly
    ("label", "L2", "designer", "mention: level#2", False),
    ("label", "L2", "designer", "mention:level#2 ", False),
    ("label", "L2", "designer", " mention:level#2", False),
    ("label", "L2", "designer", "Mention:level#2", False),
    ("label", "L2", "designer", "mention:Level#2", False),
    ("label", "L2", "designer", "mention:level#02", False),
    ("label", "L2", "designer", "mention:level#0", False),
    ("label", "L2", "designer", "mention:view#2", False),
    ("label", "L2", "designer", "mention:level", False),
    ("label", "L2", "designer", "MENTIONED by the designer", False),
    ("label", "L2", "designer", "q_label", True),                  # not a pick: not looked at
    ("label", "L2", "designer", "the designer mentioned L2", True),
])
def test_the_rule(name, value, source, evidence, ok):
    errors = mention_errors(bound(name, value, source, evidence), PACK, PICKS)
    assert (errors == []) is ok, errors
    assert all(e.code == UNKNOWN_MENTION and e.param == name for e in errors)


def test_the_rule_says_what_is_wrong():
    def why(*args):
        return mention_errors(bound(*args), PACK, PICKS)[0].message
    assert "not mention:<kind>#<id> exactly" in why("label", "L2", "designer", "Mention:level#2")
    assert "not a pick the designer made" in why("label", "L2", "designer", "mention:level#7")
    assert "takes a level" in why("level_name", "Generic - 200mm", "tool", "mention:type#401")
    assert "takes a type of OST_Walls" in why("walls", "300 x 450mm", "tool", "mention:type#451")
    assert "'Structure.rvt'" in why("label", 501, "designer", "mention:link#501")
    assert "has source tool" in why("level_name", "L2", "answer", "mention:level#2")


def test_the_rule_without_a_pack_or_picks():
    spec = bound("level_name", "L2", "designer", "mention:level#2")
    assert mention_errors(spec, None, PICKS) == []
    assert [e.code for e in mention_errors(spec, None, {})] == [UNKNOWN_MENTION]
