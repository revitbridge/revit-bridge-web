"""Remote add-in relay: request routing over a paired device, and its failure contract.

Pairing and the handshake themselves are ``tests/test_devices.py``; here the
device is connected and the traffic matters.
"""
from __future__ import annotations

import json
import threading

import pytest

from tests.test_api_v1 import B, spec_for
from tests.test_devices import addin_reply, connect, pack_handler, serve

DEV = "dev_abcdefghijkl"


def test_a_confirmed_execution_travels_over_the_device(client, device):
    """Health, then a confirmed execution: the add-in's nested reply is unwrapped and
    the ledger says which device ran it."""
    from backend.api.bridge import get_ledger

    headers, device_id, token = device
    outcome: dict = {}
    with connect(client, device_id, token) as ws:
        assert client.get(f"{B}/slots").json() == {"max_devices": 20, "connected": 1}

        thread = threading.Thread(target=lambda: outcome.update(
            {"health": client.get(f"{B}/revit-health", headers=headers).json()}))
        thread.start()
        request = serve(ws, 1, lambda r: json.dumps({"jsonrpc": "2.0", "id": r["id"],
                                                     "result": {"message": "hi"}}))[0]
        thread.join(timeout=5)
        assert request["method"] == "say_hello"
        assert outcome["health"]["revit_connected"] is True and outcome["health"]["mode"] == "websocket"

        issued = client.post(f"{B}/spec/confirm", json={"spec": {
            "task": "one", "action": {"kind": "execute_code", "code": "return 1;"}, "parameters": []}},
            headers=headers).json()

        def execute():
            outcome["exec"] = client.post(f"{B}/execute", json={"code": "return 1;", "token": issued["token"]},
                                          headers=headers).json()

        def handler(request):
            if request["params"]["code"] == "return 1;":
                return addin_reply(request, {"Status": "Created", "ElementId": 7})
            return addin_reply(request, {"Title": "Project1", "RevitVersion": "2026"})

        thread = threading.Thread(target=execute)
        thread.start()
        serve(ws, 2, handler)                      # document probe, then the code
        thread.join(timeout=5)

    out = outcome["exec"]
    assert out["success"] is True and out["result"] == {"Status": "Created", "ElementId": 7}
    record = get_ledger().get(out["evidence_id"])
    assert record["host"] == "web" and record["scope"] == device_id
    assert record["document"] == {"title": "Project1", "revit_version": "2026"}

    assert client.get(f"{B}/slots").json()["connected"] == 0
    missing = client.get(f"{B}/revit-health", headers=headers).json()
    assert missing["revit_connected"] is False and "no connected" in missing["detail"]
    gone = client.post(f"{B}/execute", json={"code": "return 1;", "token": issued["token"]}, headers=headers)
    assert gone.status_code == 503 and gone.json()["error"] == "revit_unreachable"


def test_pack_run_over_a_device_counts_usage_in_the_data_root(client, device, tmp_path):
    headers, device_id, token = device
    outcome: dict = {}
    with connect(client, device_id, token) as ws:
        issued = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")},
                             headers=headers).json()

        def run():
            outcome["run"] = client.post(f"{B}/tools/query_levels/run",
                                         json={"params": {}, "token": issued["token"]}, headers=headers).json()

        thread = threading.Thread(target=run)
        thread.start()
        serve(ws, 4, pack_handler([{"Id": 1, "Name": "L1", "ElevationMm": 0.0}], count=3))
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
    listed = {t["name"]: t for t in client.get(f"{B}/tools").json()}
    assert listed["query_levels"]["used"] == 1


def test_a_device_that_leaves_mid_request_is_a_transport_failure(client, device):
    """The add-in goes away between the route's check and the reply: the package sees
    ConnectionError (like the TCP client), consumes no token and writes no ledger line."""
    from backend.api.bridge import get_gate, get_ledger

    headers, device_id, token = device
    outcome: dict = {}
    with connect(client, device_id, token) as ws:
        issued = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")},
                             headers=headers).json()

        def run():
            resp = client.post(f"{B}/tools/query_levels/run",
                               json={"params": {}, "token": issued["token"]}, headers=headers)
            outcome["run"] = (resp.status_code, resp.json())

        thread = threading.Thread(target=run)
        thread.start()
        probe = ws.receive_json()                     # the package's document probe
        assert probe["method"] == "send_code_to_revit"
        ws.close()                                    # ...and the add-in leaves instead of answering
        thread.join(timeout=10)

    status, out = outcome["run"]
    if status == 200:                                # the package's "nothing reached Revit"
        assert out["success"] is False and out["error"] and out.get("evidence_id") is None
    else:
        assert status == 503 and out["error"] == "revit_unreachable"
    assert get_gate().peek(issued["token"]).used_at is None        # still redeemable
    assert get_ledger().recent(scope=device_id) == []              # nothing reached Revit, nothing recorded

    # A read over a device whose add-in left is 503, not a 200 with an error string.
    with connect(client, device_id, token) as ws:
        def query():
            resp = client.post(f"{B}/query", json={"kind": "levels"}, headers=headers)
            outcome["query"] = (resp.status_code, resp.json())

        thread = threading.Thread(target=query)
        thread.start()
        assert ws.receive_json()["method"] == "send_code_to_revit"
        ws.close()
        thread.join(timeout=10)
    status, out = outcome["query"]
    assert status == 503 and out["error"] == "revit_unreachable" and out["kind"] == "levels"


def test_relay_client_failure_contract_matches_the_tcp_client():
    import asyncio

    from backend.relay import DeviceRelay, WebSocketRevitClient

    relay = DeviceRelay(max_devices=2)
    revit = WebSocketRevitClient(relay, DEV, timeout=1)

    async def scenario():
        with pytest.raises(ConnectionError):
            await revit.ensure_connected()
        with pytest.raises(ConnectionError):
            await revit.send_command("say_hello", {})
        with pytest.raises(ConnectionError):
            await relay.send_code("dev_zzzzzzzzzzzz", "return 1;")
        assert await revit.ping() is False

    asyncio.run(scenario())


class _InstantAddin:
    """A socket whose reply is delivered before send_text returns.

    That is what happens when the receive loop wins the race against the
    sender: resolve_response runs while send_text is still awaiting.
    """

    def __init__(self, relay, device_id: str, reply_for):
        self._relay = relay
        self._device = device_id
        self._reply_for = reply_for
        self.sent: list[dict] = []

    class client_state:  # what DeviceConnection.connected inspects
        name = "CONNECTED"

    async def send_text(self, text: str) -> None:
        request = json.loads(text)
        self.sent.append(request)
        for reply in self._reply_for(request):
            self._relay.resolve_response(self._device, json.dumps(reply))


def test_reply_arriving_before_send_returns_is_not_lost():
    import asyncio

    from backend.relay import DeviceRelay

    async def scenario():
        relay = DeviceRelay(max_devices=2)
        ws = _InstantAddin(relay, DEV, lambda req: [{"jsonrpc": "2.0", "id": req["id"],
                                                    "result": {"message": "hi"}}])
        assert relay.register(DEV, ws) is None
        resp = await relay.send_command(DEV, "say_hello", {"message": "ping"}, timeout=1.0)
        assert resp.success and resp.result == {"message": "hi"}
        assert relay.device_status(DEV)["requests"] == 1
        assert relay.get_connection(DEV).pending is None
        return ws.sent[0]["method"]

    assert asyncio.run(scenario()) == "say_hello"


def test_stray_reply_before_ours_is_skipped_not_misattributed():
    import asyncio

    from backend.relay import DeviceRelay

    async def scenario():
        relay = DeviceRelay(max_devices=2)
        # A late reply from an earlier request comes first, then the real one.
        ws = _InstantAddin(relay, DEV, lambda req: [
            {"jsonrpc": "2.0", "id": "stale-1", "result": {"message": "old"}},
        ])
        assert relay.register(DEV, ws) is None

        async def deliver_real_reply_later():
            await asyncio.sleep(0.05)
            relay.resolve_response(DEV, json.dumps({"jsonrpc": "2.0", "id": ws.sent[0]["id"],
                                                    "result": {"message": "new"}}))

        task = asyncio.create_task(deliver_real_reply_later())
        resp = await relay.send_command(DEV, "say_hello", {}, timeout=1.0)
        await task
        return resp

    resp = asyncio.run(scenario())
    assert resp.success and resp.result == {"message": "new"}


def test_the_relay_says_why_it_refused_a_connection():
    from backend.relay import DeviceRelay

    relay = DeviceRelay(max_devices=1)
    ws = _InstantAddin(relay, DEV, lambda req: [])
    assert relay.register(DEV, ws) is None
    assert relay.register(DEV, ws) == "occupied"
    assert relay.register("dev_zzzzzzzzzzzz", ws) == "full"
    assert relay.get_status() == {"max_devices": 1, "connected": 1}
    relay.unregister(DEV)
    assert relay.register("dev_zzzzzzzzzzzz", ws) is None
    assert relay.device_status(DEV) == {"online": False, "connected_at": None, "requests": 0}


def test_an_error_reply_carries_its_json_rpc_code():
    """-32001 must survive the relay: the package reads it as declined_on_device."""
    import asyncio

    from backend.relay import DeviceRelay

    async def scenario():
        relay = DeviceRelay(max_devices=1)
        ws = _InstantAddin(relay, DEV, lambda req: [
            {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32001, "message": "declined on device"}},
        ])
        assert relay.register(DEV, ws) is None
        return await relay.send_command(DEV, "send_code_to_revit", {"code": "return 1;"}, timeout=1.0)

    resp = asyncio.run(scenario())
    assert resp.success is False and resp.error_code == -32001 and "declined" in resp.error


def test_confirm_travels_with_a_longer_timeout():
    import asyncio

    from revit_bridge.revit.client import CONFIRM_TIMEOUT_SECONDS

    from backend.relay import DeviceRelay

    async def scenario():
        relay = DeviceRelay(max_devices=1)
        ws = _InstantAddin(relay, DEV, lambda req: [
            {"jsonrpc": "2.0", "id": req["id"], "result": {"success": True, "result": "1", "errorMessage": ""}},
        ])
        assert relay.register(DEV, ws) is None
        confirm = {"kind": "execute_code", "title": "revit-bridge", "message": "card"}
        resp = await relay.send_code(DEV, "return 1;", None, timeout=5.0, confirm=confirm)
        assert resp.success
        return ws.sent[0]["params"]

    params = asyncio.run(scenario())
    assert params["confirm"] == {"kind": "execute_code", "title": "revit-bridge", "message": "card"}
    assert CONFIRM_TIMEOUT_SECONDS > 5.0        # the request waits for a person, not for Revit
