"""Remote add-in relay: slot registration, token handshake, request routing."""
from __future__ import annotations

import json
import threading

from starlette.websockets import WebSocketDisconnect

WS = "/api/v1/bridge/ws/1"


def _addin_reply(request: dict, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})


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

        # A code execution over the slot unwraps the add-in's nested reply.
        def execute():
            outcome["exec"] = client.post(
                "/api/v1/bridge/execute", json={"code": "return 1;"}, headers={"X-Slot-Id": "1"},
            ).json()

        thread = threading.Thread(target=execute)
        thread.start()
        request = addin.receive_json()
        assert request["method"] == "send_code_to_revit"
        addin.send_text(_addin_reply(request, {
            "success": True, "result": json.dumps({"Status": "Created", "ElementId": 7}), "errorMessage": "",
        }))
        thread.join(timeout=5)
        assert outcome["exec"] == {"success": True, "result": {"Status": "Created", "ElementId": 7}, "error": None}

    assert client.get("/api/v1/bridge/slots").json()["connected"] == 0
    missing = client.get("/api/v1/bridge/revit-health", headers={"X-Slot-Id": "1"}).json()
    assert missing["revit_connected"] is False and "no connected" in missing["detail"]
    assert client.post("/api/v1/bridge/execute", json={"code": "return 1;"},
                       headers={"X-Slot-Id": "1"}).status_code == 502


def test_invalid_slot_and_occupied_slot(client):
    try:
        with client.websocket_connect("/api/v1/bridge/ws/9"):
            pass
        raised = False
    except WebSocketDisconnect as exc:
        raised = exc.code == 4001
    assert raised

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
