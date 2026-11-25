"""Same-turn read-only proposals with a scripted model and the normal package executor."""
import asyncio
import json

from revit_bridge.capabilities import ToolStore

from backend.api import direct_reads
from tests.fake_model import json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit
from tests.test_api_v1 import B, WALL, spec_for
from tests.test_multi_step import request
from tests.test_host_loop import events
from tests.test_roles import ROUTING_TOOL, client, model  # noqa: F401

READER = "get_element_geometry"


def test_pick_is_rejected_if_the_document_changed_before_send(monkeypatch):
    from backend.api import chat
    async def resolve(*args):
        return {"resolved": [{"kind": "element", "id": 72, "document_id": "B"}], "not_found": []}
    async def client():
        return None
    monkeypatch.setattr(chat, "resolve_mentions", resolve)
    monkeypatch.setattr(chat, "get_revit_client", client)
    report = asyncio.run(chat.check_mentions([chat.ChatMention(kind="element", id=72, document_id="A")]))
    assert not report["resolved"] and report["not_found"][0]["reason"] == "document_changed"


def declare_reader(monkeypatch):
    # Simulate the next published catalogue while exercising the installed execution package.
    original = ToolStore.load

    def load(self, name):
        tool = original(self, name)
        if tool:
            tool.read_only = name == READER
        return tool

    monkeypatch.setattr(ToolStore, "load", load)


def read_call():
    return ("propose_spec", {"spec": spec_for(READER, element_id=72)})


def test_reader_returns_to_planner_without_a_confirmation_or_supervisor(client, model, revit, monkeypatch):
    declare_reader(monkeypatch)
    original = revit.handler
    def handler(req):
        if "LocationCurve" in req.get("params", {}).get("code", ""):
            return FakeRevit.code_result(req["id"], {"Midpoint": 1750})
        return original(req)
    revit.handler = handler
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(read_call()),
                 tool_turn(("propose_spec", {"spec": spec_for("create_wall", **{**WALL, "start_x": 1750})})),
                 text_turn("Wall card"))
    _, frames = request(client, message="Read element 72 and build a wall from its midpoint.")
    reads = [e["read"] for e in events(frames, "tool") if "read" in e]
    assert len(reads) == 1 and reads[0]["result"]["success"], (reads, frames)
    assert reads[0]["result"]["result"] == {"Midpoint": 1750}
    assert "1750" in json.dumps(model.requests_of("planner")[1]["messages"])
    assert not model.requests_of("supervisor")
    card, = events(frames, "spec")
    assert card["spec"]["action"]["tool"] == "create_wall"
    assert 'host:read-only' in client.get(f"{B}/evidence").text


def test_explicit_reader_step_records_evidence_and_rejects_stale_ids(client, model, revit, monkeypatch):
    declare_reader(monkeypatch)
    name, args = read_call()
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn((name, {**args, "step_id": "missing"})),
                 tool_turn(("set_plan", {"steps": [{"id": "a", "title": "Read", "tool": READER},
                                                   {"id": "b", "title": "Build", "tool": "create_wall"}]})),
                 tool_turn((name, {**args, "step_id": "a"})),
                 tool_turn(("propose_spec", {"step_id": "b", "spec": spec_for("create_wall", **WALL)})),
                 text_turn("Wall card."))
    _, frames = request(client, message="Read the element, then build the wall.")
    assert sum("read" in e for e in events(frames, "tool")) == 1
    steps = events(frames, "plan")[-1]["steps"]
    assert [step["status"] for step in steps] == ["done", "proposed"] and steps[0]["evidence_id"]


def test_a_user_override_never_gets_direct_execution(client, model, revit, monkeypatch):
    declare_reader(monkeypatch)
    store = ToolStore()
    store.user_dir.mkdir(parents=True, exist_ok=True)
    (store.user_dir / (READER + ".yaml")).write_bytes(store.path_of(READER).read_bytes())
    model.script("analyst", json_turn(ROUTING_TOOL))
    model.script("planner", tool_turn(read_call()), text_turn("Confirm the card."))
    _, frames = request(client, message="Get the geometry.")
    assert len(events(frames, "spec")) == 1
    assert not any("read" in e for e in events(frames, "tool"))


def test_limit_is_per_designer_message(client, model, revit, monkeypatch):
    declare_reader(monkeypatch)
    monkeypatch.setattr(direct_reads, "MAX_READS", 1)
    sid = None
    for _ in range(2):
        model.script("analyst", json_turn(ROUTING_TOOL))
        model.script("planner", tool_turn(read_call(), read_call()), text_turn("Read."))
        sid, frames = request(client, message="Read the element.", session_id=sid)
        assert sum("read" in e for e in events(frames, "tool")) == 1
        assert "read_limit" in json.dumps(model.requests_of("planner")[-1])
