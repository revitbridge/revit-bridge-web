"""Remote add-in relay: slot registration, token handshake, request routing."""
from __future__ import annotations

import json
import threading

import pytest
from starlette.websockets import WebSocketDisconnect

WS = "/api/v1/bridge/ws/1"


def _addin_reply(request: dict, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


def _serve(addin, count: int, handler) -> list[dict]:
    """Answer ``count`` requests on the add-in side (the package's flow is deterministic)."""
    seen = []
    for _ in range(count):
        request = addin.receive_json()
        assert request["method"] == "send_code_to_revit"
        seen.append(request)
        addin.send_text(json.dumps(handler(request)))
    return seen


def test_slot_roundtrip_without_tokens(client):
    with client.websocket_connect(WS) as addin:
        assert client.get("/api/v1/bridge/slots").json()["slots"]["1"]["status"] == "connected"

        outcome: dict = {}

        def browser():
            outcome["health"] = client.get("/api/v1/bridge/revit-health", headers={"X-Slot-Id": "1"}).json()

        thread = threading.Thread(target=browser)
        thread.start()
        request = addin.receive_json()
        assert request["method"] == "say_hello"
        addin.send_text(_addin_reply(request, {"message": "hi"}))
        thread.join(timeout=5)

        assert outcome["health"]["revit_connected"] is True
        assert outcome["health"]["mode"] == "websocket"

        # A confirmed code execution over the slot: the package probes the document,
        # sends the code and unwraps the add-in's nested reply; the ledger says "web".
        token = client.post("/api/v1/bridge/spec/confirm", json={"spec": {
            "task": "one", "action": {"kind": "execute_code", "code": "return 1;"}, "parameters": []}},
            headers={"X-Slot-Id": "1"}).json()["token"]

        def execute():
            outcome["exec"] = client.post(
                "/api/v1/bridge/execute", json={"code": "return 1;", "token": token}, headers={"X-Slot-Id": "1"},
            ).json()

        def handler(request):
            code = request["params"]["code"]
            if code == "return 1;":
                return {"jsonrpc": "2.0", "id": request["id"], "result": {
                    "success": True, "result": json.dumps({"Status": "Created", "ElementId": 7}), "errorMessage": ""}}
            return {"jsonrpc": "2.0", "id": request["id"], "result": {
                "success": True, "result": json.dumps({"Title": "Project1", "RevitVersion": "2026"}), "errorMessage": ""}}

        thread = threading.Thread(target=execute)
        thread.start()
        _serve(addin, 2, handler)                     # document probe, then the code
        thread.join(timeout=5)
        out = outcome["exec"]
        assert out["success"] is True and out["result"] == {"Status": "Created", "ElementId": 7} and out["error"] is None
        from backend.api.bridge import get_ledger
        record = get_ledger().get(out["evidence_id"])
        assert record["host"] == "web" and record["document"] == {"title": "Project1", "revit_version": "2026"}

    assert client.get("/api/v1/bridge/slots").json()["connected"] == 0
    missing = client.get("/api/v1/bridge/revit-health", headers={"X-Slot-Id": "1"}).json()
    assert missing["revit_connected"] is False and "no connected" in missing["detail"]
    gone = client.post("/api/v1/bridge/execute", json={"code": "return 1;", "token": token}, headers={"X-Slot-Id": "1"})
    assert gone.status_code == 503 and gone.json()["error"] == "revit_unreachable"


def test_pack_run_over_the_slot_counts_usage_in_the_data_root(client, tmp_path):
    with client.websocket_connect(WS) as addin:
        outcome: dict = {}
        token = client.post("/api/v1/bridge/spec/confirm", json={"spec": {
            "task": "levels", "action": {"kind": "run_tool", "tool": "query_levels"}, "parameters": []}},
            headers={"X-Slot-Id": "1"}).json()["token"]

        def run():
            outcome["run"] = client.post(
                "/api/v1/bridge/tools/query_levels/run", json={"params": {}, "token": token},
                headers={"X-Slot-Id": "1"},
            ).json()

        def handler(request):
            code = request["params"]["code"]
            if "GetElementCount" in code:                       # the count_delta probe, before and after
                payload = 3
            elif "return levels;" in code:                      # the pack itself
                payload = [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}]
            else:                                               # the document probe
                payload = {"Title": "Project1", "RevitVersion": "2026"}
            return _addin_reply(request, {"success": True, "result": json.dumps(payload), "errorMessage": ""})

        thread = threading.Thread(target=run)
        thread.start()
        _serve(addin, 4, lambda r: json.loads(handler(r)))   # probe, count, the pack, count
        thread.join(timeout=5)
        out = outcome["run"]
        assert out["success"] is True and out["tool"] == "query_levels" and out["error"] is None
        assert out["result"] == [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}]
        assert out["validation"]["passed"] is True and out["evidence_id"].startswith("ev_")

    # The package counts the run in usage.json under its data root and leaves
    # the built-in pack file alone (it is read-only inside the wheel).
    user_dir = tmp_path / "data" / "capabilities"
    usage = json.loads((user_dir / "usage.json").read_text(encoding="utf-8"))
    assert usage["query_levels"]["execution_count"] == 1 and usage["query_levels"]["failure_count"] == 0
    assert not (user_dir / "query_levels.yaml").exists()
    listed = {t["name"]: t for t in client.get("/api/v1/bridge/tools").json()}
    assert listed["query_levels"]["used"] == 1


def test_a_slot_that_leaves_mid_request_is_a_transport_failure(client):
    """The add-in goes away between the route's check and the reply: the package sees
    ConnectionError (like the TCP client), consumes no token and writes no ledger line."""
    from backend.api.bridge import get_gate, get_ledger

    headers = {"X-Slot-Id": "1"}
    outcome: dict = {}
    with client.websocket_connect(WS) as addin:
        token = client.post("/api/v1/bridge/spec/confirm", json={"spec": {
            "task": "levels", "action": {"kind": "run_tool", "tool": "query_levels"}, "parameters": []}},
            headers=headers).json()["token"]

        def run():
            resp = client.post("/api/v1/bridge/tools/query_levels/run",
                               json={"params": {}, "token": token}, headers=headers)
            outcome["run"] = (resp.status_code, resp.json())

        thread = threading.Thread(target=run)
        thread.start()
        probe = addin.receive_json()                      # the package's document probe
        assert probe["method"] == "send_code_to_revit"
        addin.close()                                     # ...and the add-in leaves instead of answering
        thread.join(timeout=10)

    status, out = outcome["run"]
    if status == 200:                                    # the package's "nothing reached Revit"
        assert out["success"] is False and out["error"] and out.get("evidence_id") is None
    else:
        assert status == 503 and out["error"] == "revit_unreachable"
    assert get_gate().peek(token).used_at is None        # still redeemable
    assert get_ledger().recent() == []                   # nothing reached Revit, nothing recorded

    # A read over a slot whose add-in leaves is 503, not a 200 with an error string.
    with client.websocket_connect(WS) as addin:
        def query():
            resp = client.post("/api/v1/bridge/query", json={"kind": "levels"}, headers=headers)
            outcome["query"] = (resp.status_code, resp.json())

        thread = threading.Thread(target=query)
        thread.start()
        assert addin.receive_json()["method"] == "send_code_to_revit"
        addin.close()
        thread.join(timeout=10)
    status, out = outcome["query"]
    assert status == 503 and out["error"] == "revit_unreachable" and out["kind"] == "levels"


def test_relay_client_failure_contract_matches_the_tcp_client():
    import asyncio

    from backend.relay import SlotManager, WebSocketRevitClient

    mgr = SlotManager(max_slots=2)
    client = WebSocketRevitClient(mgr, "1", timeout=1)

    async def scenario():
        with pytest.raises(ConnectionError):
            await client.ensure_connected()
        with pytest.raises(ConnectionError):
            await client.send_command("say_hello", {})
        with pytest.raises(ConnectionError):
            await mgr.send_code("2", "return 1;")
        assert await client.ping() is False

    asyncio.run(scenario())


def _rejected_at_handshake(client, path: str) -> int | None:
    try:
        with client.websocket_connect(path):
            pass
    except WebSocketDisconnect as exc:
        return exc.code
    return None


def test_invalid_slot_and_occupied_slot(client):
    arabic_one, fullwidth_one = chr(0x0661), chr(0xFF11)  # str.isdigit() accepts both
    for bad in ("9", "0", "01", "1 ", arabic_one, fullwidth_one, "one"):
        assert _rejected_at_handshake(client, f"/api/v1/bridge/ws/{bad}") == 4001, bad
    assert client.get("/api/v1/bridge/slots").json()["connected"] == 0

    with client.websocket_connect(WS):
        try:
            with client.websocket_connect(WS) as second:
                second.receive_text()  # the server accepted, then closed the socket
            second_rejected = False
        except WebSocketDisconnect as exc:
            second_rejected = exc.code == 4002
        assert second_rejected


def test_slot_tokens_guard_http_and_handshake(make_client, env):
    env.setenv("MCP_BRIDGE_REQUIRE_SLOT_TOKEN", "1")
    env.setenv("MCP_BRIDGE_SLOT_TOKEN_1", "top-secret")
    client = make_client()

    assert client.get("/config.json").json()["features"]["slotTokenRequired"] is True
    # Only relay status is public; every functional route needs a slot.
    assert client.get("/api/v1/bridge/slots").status_code == 200
    assert client.get("/api/v1/bridge/tools").status_code == 403
    assert client.get("/api/v1/bridge/tools", headers={"X-Slot-Id": "1"}).status_code == 403
    assert client.get("/api/v1/bridge/tools", headers={"X-Slot-Id": "1", "X-Slot-Token": "nope"}).status_code == 403
    assert client.get("/api/v1/bridge/tools", headers={"X-Slot-Id": "1", "X-Slot-Token": "top-secret"}).status_code == 200

    # Add-in handshake: wrong token closes with 4003, right token registers.
    try:
        with client.websocket_connect(WS) as addin:
            addin.send_text(json.dumps({"type": "auth", "slot_id": "1", "token": "wrong"}))
            addin.receive_text()
        rejected = False
    except WebSocketDisconnect as exc:
        rejected = exc.code == 4003
    assert rejected

    with client.websocket_connect(WS) as addin:
        addin.send_text(json.dumps({"type": "auth", "slot_id": "1", "token": "top-secret"}))
        # Registration happens after the handshake; give the loop a request to prove it.
        outcome: dict = {}
        thread = threading.Thread(target=lambda: outcome.update(
            client.get("/api/v1/bridge/service-health").json()))
        thread.start(); thread.join(timeout=5)
        assert outcome["connected_slots"] == 1


class _InstantAddin:
    """A socket whose reply is delivered before send_text returns.

    That is what happens when the receive loop wins the race against the
    sender: resolve_response runs while send_text is still awaiting.
    """

    def __init__(self, manager, slot_id: str, reply_for):
        self._mgr = manager
        self._slot = slot_id
        self._reply_for = reply_for
        self.sent: list[dict] = []

    class client_state:  # what SlotConnection.connected inspects
        name = "CONNECTED"

    async def send_text(self, text: str) -> None:
        request = json.loads(text)
        self.sent.append(request)
        for reply in self._reply_for(request):
            self._mgr.resolve_response(self._slot, json.dumps(reply))


def test_reply_arriving_before_send_returns_is_not_lost():
    import asyncio

    from backend.relay import SlotManager

    async def scenario():
        mgr = SlotManager(max_slots=2)
        ws = _InstantAddin(mgr, "1", lambda req: [{"jsonrpc": "2.0", "id": req["id"], "result": {"message": "hi"}}])
        assert mgr.register("1", ws)
        resp = await mgr.send_command("1", "say_hello", {"message": "ping"}, timeout=1.0)
        assert resp.success and resp.result == {"message": "hi"}
        assert mgr.get_status()["slots"]["1"]["requests"] == 1
        assert mgr.get_connection("1").pending is None
        return ws.sent[0]["method"]

    assert asyncio.run(scenario()) == "say_hello"


def test_stray_reply_before_ours_is_skipped_not_misattributed():
    import asyncio

    from backend.relay import SlotManager

    async def scenario():
        mgr = SlotManager(max_slots=2)
        # A late reply from an earlier request comes first, then the real one.
        ws = _InstantAddin(mgr, "1", lambda req: [
            {"jsonrpc": "2.0", "id": "stale-1", "result": {"message": "old"}},
        ])
        assert mgr.register("1", ws)

        async def deliver_real_reply_later():
            await asyncio.sleep(0.05)
            mgr.resolve_response("1", json.dumps({"jsonrpc": "2.0", "id": ws.sent[0]["id"], "result": {"message": "new"}}))

        task = asyncio.create_task(deliver_real_reply_later())
        resp = await mgr.send_command("1", "say_hello", {}, timeout=1.0)
        await task
        return resp

    resp = asyncio.run(scenario())
    assert resp.success and resp.result == {"message": "new"}


def test_slot_manager_only_registers_literal_slot_ids():
    from backend.relay import SlotManager

    mgr = SlotManager(max_slots=2)
    assert mgr.slot_ids == {"1", "2"}
    for bad in ("01", "3", chr(0x0661), ""):
        assert mgr.register(bad, ws=None) is False
    assert mgr.get_status()["connected"] == 0
