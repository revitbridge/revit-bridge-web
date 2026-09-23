"""Pairing, the device list, revocation and what a device's browser key may see."""
from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager

from backend.api import devices as devices_api
from backend.api.bridge import get_gate, get_ledger
from tests.test_api_v1 import B, spec_for

ADMIN = {"X-Admin-Token": "hunter2"}
CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
DEVICE_ID_RE = re.compile(r"^dev_[a-z2-7]{12}$")


def addin_reply(request: dict, payload, success: bool = True, error: str = "") -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {
        "success": success, "result": json.dumps(payload) if payload is not None else None,
        "errorMessage": error}})


@contextmanager
def connect(client, device_id: str, token: str, *, auth: dict | None = None):
    """The add-in's socket, authenticated like the real one (first message = auth).

    Yields once the relay has registered the device: the handshake is processed
    asynchronously and a request sent before that would be answered "no add-in
    connected" while this socket waited for it. Always closed on the way out - a
    socket left open blocks the TestClient's shutdown.
    """
    from backend.relay import get_relay

    with client.websocket_connect(f"{B}/ws/{device_id}") as ws:
        ws.send_text(json.dumps(auth if auth is not None else
                                {"type": "auth", "device_id": device_id, "token": token}))
        deadline = time.monotonic() + 5
        while get_relay().get_connection(device_id) is None:
            if time.monotonic() > deadline:
                raise AssertionError(f"device {device_id} never registered")
            time.sleep(0.01)
        yield ws


def serve(ws, count: int, handler) -> list[dict]:
    """Answer ``count`` requests on the add-in side (the package's flow is deterministic)."""
    seen = []
    for _ in range(count):
        request = ws.receive_json()
        seen.append(request)
        ws.send_text(handler(request))
    return seen


def pack_handler(levels=None, count: int = 2):
    """A fake add-in for one ``query_levels`` run: probe, count, the pack, count."""
    def handler(request):
        code = request["params"]["code"]
        if "GetElementCount" in code:
            return addin_reply(request, count)
        if "return levels;" in code:
            return addin_reply(request, levels if levels is not None else [])
        return addin_reply(request, {"Title": "Project1", "RevitVersion": "2026"})
    return handler


def run_pack_over(client, ws, headers, token, *, tool: str = "query_levels", handler=None) -> dict:
    """Drive one pack run over a connected device and return the route's answer."""
    outcome: dict = {}

    def run():
        outcome["run"] = client.post(f"{B}/tools/{tool}/run",
                                     json={"params": {}, "token": token}, headers=headers).json()

    thread = threading.Thread(target=run)
    thread.start()
    serve(ws, 4, handler or pack_handler())
    thread.join(timeout=5)
    return outcome["run"]


# -- pairing -------------------------------------------------------------------------

def test_pair_returns_a_code_a_browser_key_and_an_install_command(client, tmp_path):
    resp = client.post(f"{B}/devices/pair", json={"label": "Studio PC"})
    assert resp.status_code == 200, resp.text
    paired = resp.json()
    assert set(paired) == {"code", "device_id", "expires_at", "browser_key", "install_command"}
    assert CODE_RE.fullmatch(paired["code"]) and DEVICE_ID_RE.fullmatch(paired["device_id"])
    assert len(paired["browser_key"]) >= 24

    # Exactly what the installer takes: the site address and the code, never a ws:// URL.
    assert paired["install_command"] == (
        "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/revitbridge/"
        "revit-bridge-addin/main/installer/install.ps1))) "
        f"-Mode remote -Server http://testserver -Pair {paired['code']}")
    assert "ws://" not in paired["install_command"] and "wss://" not in paired["install_command"]

    # The key already drives the device; the label came through.
    headers = {"X-Device-Id": paired["device_id"], "X-Device-Key": paired["browser_key"]}
    view = client.get(f"{B}/devices/{paired['device_id']}", headers=headers).json()
    assert view == {"device_id": paired["device_id"], "label": "Studio PC", "online": False,
                    "last_seen": None, "requests": 0, "revoked": False, "addin_version": None}

    # Only hashes on disk: neither the code nor the key is recoverable from the file.
    stored = (tmp_path / "data" / "auth" / "devices.json").read_text(encoding="utf-8")
    assert paired["code"] not in stored and paired["browser_key"] not in stored
    assert json.loads(stored)["schema_version"] == 1


def test_the_install_command_names_the_public_address(client):
    """Behind a reverse proxy the request is plain http to 127.0.0.1: the visitor's
    address comes from X-Forwarded-Proto / X-Forwarded-Host."""
    proxied = client.post(f"{B}/devices/pair", json={},
                          headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "demo.example"}).json()
    assert proxied["install_command"].endswith(f"-Mode remote -Server https://demo.example -Pair {proxied['code']}")

    redeemed = client.post(f"{B}/devices/redeem", json={"code": proxied["code"]},
                           headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "demo.example"}).json()
    assert redeemed["ws_url"] == f"wss://demo.example/api/v1/bridge/ws/{proxied['device_id']}"

    # A comma-separated chain (several proxies) uses the first, the client's own.
    chained = client.post(f"{B}/devices/pair", json={},
                          headers={"X-Forwarded-Proto": "https, http",
                                   "X-Forwarded-Host": "demo.example, internal:7860"}).json()
    assert "-Server https://demo.example " in chained["install_command"]

    # Without the headers: the address the request itself carries.
    direct = client.post(f"{B}/devices/pair", json={}).json()
    assert "-Server http://testserver " in direct["install_command"]
    assert client.post(f"{B}/devices/redeem", json={"code": direct["code"]}).json()["ws_url"].startswith(
        "ws://testserver/api/v1/bridge/ws/")


def test_pair_is_rate_limited_on_its_own_table(make_client, env):
    env.setenv("CHAT_RATE_LIMIT", "2")
    client = make_client()
    assert client.post(f"{B}/devices/pair", json={}).status_code == 200
    assert client.post(f"{B}/devices/pair", json={}).status_code == 200
    refused = client.post(f"{B}/devices/pair", json={})
    assert refused.status_code == 429 and refused.json()["error"] == "rate_limited"
    # The confirm counter is untouched: a confirmation still goes through.
    assert client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")}).status_code == 200


def test_redeem_turns_the_code_into_a_device_token_once(client):
    paired = client.post(f"{B}/devices/pair", json={"label": "Laptop"}).json()

    resp = client.post(f"{B}/devices/redeem", json={"code": paired["code"], "addin_version": "0.2.0"})
    assert resp.status_code == 200, resp.text
    redeemed = resp.json()
    assert set(redeemed) == {"device_id", "device_token", "ws_url"}
    assert redeemed["device_id"] == paired["device_id"]
    assert redeemed["ws_url"].endswith(f"/api/v1/bridge/ws/{paired['device_id']}")
    assert len(redeemed["device_token"]) >= 32

    # One code, one redemption; an unknown or malformed code is the same answer.
    again = client.post(f"{B}/devices/redeem", json={"code": paired["code"]})
    assert again.status_code == 404 and again.json()["error"] == "invalid_code"
    assert client.post(f"{B}/devices/redeem", json={"code": "ZZZZ-ZZZZ"}).status_code == 404
    assert client.post(f"{B}/devices/redeem", json={"code": "nonsense"}).status_code == 404

    headers = {"X-Device-Id": paired["device_id"], "X-Device-Key": paired["browser_key"]}
    view = client.get(f"{B}/devices/{paired['device_id']}", headers=headers).json()
    assert view["addin_version"] == "0.2.0" and view["last_seen"] is not None


def test_redeem_has_its_own_low_limit(make_client, env):
    env.setenv("CHAT_RATE_LIMIT", "2")          # the pair limit stays separate
    client = make_client()
    for _ in range(devices_api.REDEEM_LIMIT):
        assert client.post(f"{B}/devices/redeem", json={"code": "ZZZZ-ZZZZ"}).status_code == 404
    refused = client.post(f"{B}/devices/redeem", json={"code": "ZZZZ-ZZZZ"})
    assert refused.status_code == 429 and refused.json()["error"] == "rate_limited"


# -- the device -----------------------------------------------------------------------

def test_device_view_needs_its_key_or_the_admin_password(make_client, env):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    paired = client.post(f"{B}/devices/pair", json={}).json()
    other = client.post(f"{B}/devices/pair", json={}).json()

    assert client.get(f"{B}/devices/{paired['device_id']}").status_code == 403
    wrong = client.get(f"{B}/devices/{paired['device_id']}",
                       headers={"X-Device-Id": paired["device_id"], "X-Device-Key": other["browser_key"]})
    assert wrong.status_code == 403 and wrong.json()["error"] == "invalid_device_key"

    missing = client.get(f"{B}/devices/dev_aaaaaaaaaaaa", headers=ADMIN)
    assert missing.status_code == 404 and missing.json()["error"] == "unknown_device"

    # The admin token is checked first: with it the device headers do not matter.
    for extra in ({}, {"X-Device-Id": paired["device_id"]},
                  {"X-Device-Id": paired["device_id"], "X-Device-Key": ""},
                  {"X-Device-Id": paired["device_id"], "X-Device-Key": "wrong"}):
        assert client.get(f"{B}/devices/{paired['device_id']}", headers={**ADMIN, **extra}).status_code == 200
        assert client.get(f"{B}/tools", headers={**ADMIN, **extra}).status_code == 200

    # An unredeemed pairing polls as offline, not as an error (what the connect page does).
    view = client.get(f"{B}/devices/{paired['device_id']}", headers=ADMIN).json()
    assert view["online"] is False and view["last_seen"] is None and view["requests"] == 0


def test_device_list_is_admin_only(make_client, env):
    client = make_client()
    assert client.post(f"{B}/devices/pair", json={"label": "One"}).status_code == 200
    assert client.get(f"{B}/devices").status_code == 503          # no ADMIN_PASSWORD configured

    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    paired = client.post(f"{B}/devices/pair", json={"label": "Two"}).json()
    assert client.get(f"{B}/devices").status_code == 403
    listing = client.get(f"{B}/devices", headers=ADMIN)
    assert listing.status_code == 200
    body = listing.json()
    assert [d["label"] for d in body["devices"]] == ["One", "Two"]   # the same data root, both pairings
    assert body["devices"][1]["device_id"] == paired["device_id"]
    assert all(d["online"] is False and d["requests"] == 0 for d in body["devices"])
    # Nothing secret in the listing.
    text = json.dumps(body)
    assert "hash" not in text and paired["browser_key"] not in text and paired["code"] not in text


def test_revoking_closes_the_socket_and_kills_the_key(client, device):
    from starlette.websockets import WebSocketDisconnect

    headers, device_id, token = device
    with connect(client, device_id, token) as ws:
        assert client.get(f"{B}/devices/{device_id}", headers=headers).json()["online"] is True

        revoked = client.delete(f"{B}/devices/{device_id}", headers=headers)
        assert revoked.status_code == 200
        assert revoked.json() == {"status": "revoked", "device_id": device_id, "disconnected": True}

        try:
            ws.receive_text()
            closed = None
        except WebSocketDisconnect as exc:
            closed = exc.code
        assert closed == 4003

    # The key is dead: every device-scoped route refuses it the same way.
    assert client.get(f"{B}/devices/{device_id}", headers=headers).status_code == 403
    assert client.get(f"{B}/devices/{device_id}", headers=headers).json()["error"] == "invalid_device_key"
    assert client.get(f"{B}/tools", headers=headers).status_code == 403
    assert client.get(f"{B}/slots").json()["connected"] == 0

    # ...and its token cannot come back through the relay either.
    try:
        with client.websocket_connect(f"{B}/ws/{device_id}") as ws:
            ws.send_text(json.dumps({"type": "auth", "device_id": device_id, "token": token}))
            ws.receive_text()
        code = None
    except WebSocketDisconnect as exc:
        code = exc.code
    assert code == 4003


# -- the handshake ---------------------------------------------------------------------

def test_the_handshake_needs_the_device_token(client, device):
    from starlette.websockets import WebSocketDisconnect

    headers, device_id, token = device

    def rejected(auth, path_id: str | None = None) -> int | None:
        try:
            with client.websocket_connect(f"{B}/ws/{path_id or device_id}") as ws:
                if auth is not None:
                    ws.send_text(auth if isinstance(auth, str) else json.dumps(auth))
                ws.receive_text()
            return None
        except WebSocketDisconnect as exc:
            return exc.code

    assert rejected({"type": "auth", "device_id": device_id, "token": "wrong"}) == 4003
    assert rejected({"type": "hello", "device_id": device_id, "token": token}) == 4003
    assert rejected({"type": "auth", "device_id": "dev_aaaaaaaaaaaa", "token": token}) == 4003
    assert rejected({"type": "auth", "device_id": device_id, "token": token}, path_id="dev_aaaaaaaaaaaa") == 4003
    assert rejected("not json") == 4003
    assert client.get(f"{B}/slots").json()["connected"] == 0

    # The right token registers the device and the relay serves it.
    with connect(client, device_id, token) as ws:
        assert client.get(f"{B}/slots").json() == {"max_devices": 20, "connected": 1}
        health: dict = {}
        thread = threading.Thread(target=lambda: health.update(
            client.get(f"{B}/revit-health", headers=headers).json()))
        thread.start()
        serve(ws, 1, lambda r: json.dumps({"jsonrpc": "2.0", "id": r["id"], "result": {"message": "hi"}}))
        thread.join(timeout=5)
        assert health["revit_connected"] is True and health["mode"] == "websocket"
        assert health["endpoint"] == f"device {device_id}"
        assert health["devices"] == {"max_devices": 20, "connected": 1}
        assert client.get(f"{B}/devices/{device_id}", headers=headers).json()["requests"] == 1

        # A second socket for the same device is refused; this one keeps working.
        assert rejected({"type": "auth", "device_id": device_id, "token": token}) == 4002


def test_a_handshake_that_never_comes_is_closed(client, device, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    from backend.api import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "HANDSHAKE_TIMEOUT", 0.2)
    _, device_id, _ = device
    try:
        with client.websocket_connect(f"{B}/ws/{device_id}") as ws:
            ws.receive_text()                       # no auth message at all
        code = None
    except WebSocketDisconnect as exc:
        code = exc.code
    assert code == 4003
    assert client.get(f"{B}/slots").json()["connected"] == 0


def test_the_relay_is_capped_by_max_devices(make_client, env):
    from starlette.websockets import WebSocketDisconnect

    env.setenv("MAX_DEVICES", "1")
    client = make_client()
    first = client.post(f"{B}/devices/pair", json={}).json()
    first_token = client.post(f"{B}/devices/redeem", json={"code": first["code"]}).json()["device_token"]
    second = client.post(f"{B}/devices/pair", json={}).json()
    second_token = client.post(f"{B}/devices/redeem", json={"code": second["code"]}).json()["device_token"]

    with connect(client, first["device_id"], first_token):
        assert client.get(f"{B}/slots").json() == {"max_devices": 1, "connected": 1}
        try:
            with client.websocket_connect(f"{B}/ws/{second['device_id']}") as other:
                other.send_text(json.dumps({"type": "auth", "device_id": second["device_id"],
                                            "token": second_token}))
                other.receive_text()
            code = None
        except WebSocketDisconnect as exc:
            code = exc.code
        assert code == 4001

    # With the first one gone the second is welcome.
    with connect(client, second["device_id"], second_token):
        assert client.get(f"{B}/slots").json() == {"max_devices": 1, "connected": 1}


# -- headers and scope -----------------------------------------------------------------

def test_the_old_slot_headers_are_refused(client, device):
    headers, device_id, _ = device
    resp = client.get(f"{B}/tools", headers={"X-Slot-Id": "1"})
    assert resp.status_code == 422 and resp.json()["error"] == "invalid_args"
    assert "X-Device-Id" in resp.json()["message"]
    assert client.get(f"{B}/tools", headers={"X-Slot-Token": "x"}).status_code == 422
    # A device header without its key, or with a wrong one, is 403.
    assert client.get(f"{B}/tools", headers={"X-Device-Id": device_id}).status_code == 403
    assert client.get(f"{B}/tools", headers={**headers, "X-Device-Key": "nope"}).status_code == 403
    assert client.get(f"{B}/tools", headers={"X-Device-Id": "dev_aaaaaaaaaaaa",
                                            "X-Device-Key": headers["X-Device-Key"]}).status_code == 403
    # No headers at all: the local add-in, no key needed.
    assert client.get(f"{B}/tools").status_code == 200
    assert client.get(f"{B}/slots").status_code == 200       # public either way


def test_a_confirmation_belongs_to_the_device_that_asked(client, device):
    headers, device_id, token = device
    with connect(client, device_id, token) as ws:
        issued = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")},
                             headers=headers).json()
        assert get_gate().peek(issued["token"]).scope == device_id

        # On its own device it runs, and the ledger line carries the scope.
        run = run_pack_over(client, ws, headers, issued["token"],
                            handler=pack_handler([{"Id": 1, "Name": "L1", "ElevationMm": 0.0}]))
        assert run["success"] is True
        record = get_ledger().get(run["evidence_id"])
        assert record["scope"] == device_id and record["host"] == "web"

        # A token issued for the local add-in is not redeemable on the device.
        local_token = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")}).json()["token"]
        assert get_gate().peek(local_token).scope == "local"
        refused = client.post(f"{B}/tools/query_levels/run",
                              json={"params": {}, "token": local_token}, headers=headers).json()
        assert refused == {"success": False, "error": "confirmation_invalid", "reason": "mismatch",
                           "message": "the execution does not match the confirmed spec"}
        assert get_gate().peek(local_token).used_at is None       # a mismatch consumes nothing


def test_one_device_cannot_see_another_device_s_evidence(client, device):
    headers, device_id, token = device
    other = client.post(f"{B}/devices/pair", json={}).json()
    other_headers = {"X-Device-Id": other["device_id"], "X-Device-Key": other["browser_key"]}
    client.post(f"{B}/devices/redeem", json={"code": other["code"]})

    with connect(client, device_id, token) as ws:
        issued = client.post(f"{B}/spec/confirm", json={"spec": spec_for("query_levels")},
                             headers=headers).json()
        run = run_pack_over(client, ws, headers, issued["token"])
        assert run["success"] is True
        evidence_id = run["evidence_id"]

    assert [r["id"] for r in client.get(f"{B}/evidence", headers=headers).json()] == [evidence_id]
    assert client.get(f"{B}/evidence", headers=other_headers).json() == []
    assert client.get(f"{B}/evidence").json() == []                      # the local add-in sees nothing either
    denied = client.post(f"{B}/evidence/{evidence_id}/validate", headers=other_headers)
    assert denied.status_code == 404 and denied.json()["error"] == "unknown_evidence"


def test_execute_over_a_device_asks_the_device_and_takes_no_for_an_answer(client, device):
    headers, device_id, token = device
    outcome: dict = {}
    with connect(client, device_id, token) as ws:
        issued = client.post(f"{B}/spec/confirm", json={"spec": {
            "task": "count walls", "action": {"kind": "execute_code", "code": "return 1;"}, "parameters": []}},
            headers=headers).json()

        def execute():
            outcome["exec"] = client.post(f"{B}/execute", json={"code": "return 1;", "token": issued["token"]},
                                          headers=headers).json()

        def handler(request):
            if request["params"]["code"] == "return 1;":
                # The designer says No on the device: JSON-RPC -32001.
                return json.dumps({"jsonrpc": "2.0", "id": request["id"],
                                   "error": {"code": -32001, "message": "declined on device"}})
            return addin_reply(request, {"Title": "Project1", "RevitVersion": "2026"})

        thread = threading.Thread(target=execute)
        thread.start()
        requests = serve(ws, 2, handler)
        thread.join(timeout=5)

    # The code request carried the confirmation prompt, the probe did not.
    probe, code_request = requests
    assert "confirm" not in probe["params"]
    confirm = code_request["params"]["confirm"]
    assert confirm["kind"] == "execute_code" and "count walls" in confirm["message"]

    result = outcome["exec"]
    assert result["success"] is False and result["error"] == "declined_on_device"
    assert get_gate().peek(issued["token"]).used_at is not None   # the designer confirmed; the device said no
    record = get_ledger().get(result["evidence_id"])
    assert record["scope"] == device_id and record["error"] == "declined_on_device"


def test_a_pack_run_never_asks_the_device(client, device):
    """Only ad-hoc code needs a device confirmation; a capability pack does not."""
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
        requests = serve(ws, 4, pack_handler())
        thread.join(timeout=5)
    assert outcome["run"]["success"] is True
    assert all("confirm" not in r["params"] for r in requests)
