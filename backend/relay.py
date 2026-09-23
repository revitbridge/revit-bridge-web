"""WebSocket device relay: remote Revit add-ins connect here, the host talks back.

The add-in opens ``/api/v1/bridge/ws/{device_id}`` towards the server and
authenticates with its device token (see ``backend.api.devices``); the server
keeps ``device_id -> WebSocket`` and forwards JSON-RPC 2.0 requests over that
socket. Browser requests select a device with ``X-Device-Id`` /
``X-Device-Key``. One device handles one request at a time (Revit executes
serially) and ``MAX_DEVICES`` caps how many may be connected at once.

``WebSocketRevitClient`` gives a device the same ``send_command`` /
``send_code`` / ``ping`` / ``ensure_connected`` surface as
``revit_bridge.revit.RevitClient`` so the routes, the package's execution
flow and ``revit_bridge.snapshot.RevitQueryExecutor`` do not care which
transport is underneath. The failure contract is the TCP client's too: a
device with no add-in, an add-in that leaves mid-request or a socket that
cannot be written raise ``ConnectionError`` (the package then consumes no
token and writes no ledger line - nothing reached Revit); only a reply that
does not arrive in time is ``RevitResponse(success=False, "Timeout ...")``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from revit_bridge.revit import RevitResponse
from revit_bridge.revit.client import CONFIRM_TIMEOUT_SECONDS

_log = logging.getLogger("backend.relay")


@dataclass
class DeviceConnection:
    """One Revit add-in connection and the device it belongs to."""
    device_id: str
    ws: WebSocket
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: asyncio.Future | None = field(default=None, repr=False)
    connected_at: float = field(default_factory=time.time)
    request_count: int = 0

    @property
    def connected(self) -> bool:
        try:
            return self.ws.client_state.name == "CONNECTED"
        except Exception:
            return False


class DeviceRelay:
    """Registry of add-in connections, keyed by device id."""

    def __init__(self, max_devices: int = 20):
        self.max_devices = max_devices
        self._devices: dict[str, DeviceConnection] = {}

    # -- registration ----------------------------------------------------------

    @property
    def connected_ids(self) -> list[str]:
        return [d for d, c in self._devices.items() if c.connected]

    def register(self, device_id: str, ws: WebSocket) -> str | None:
        """Take the device's connection. None on success, else why not: ``"occupied"``
        (that device is connected already) or ``"full"`` (``MAX_DEVICES`` reached)."""
        existing = self._devices.get(device_id)
        if existing is not None and existing.connected:
            return "occupied"
        if existing is None and len(self.connected_ids) >= self.max_devices:
            return "full"
        self._devices[device_id] = DeviceConnection(device_id=device_id, ws=ws)
        _log.info("device %s connected (%d connected)", device_id, len(self.connected_ids))
        return None

    def unregister(self, device_id: str) -> None:
        conn = self._devices.pop(device_id, None)
        if conn:
            if conn.pending and not conn.pending.done():
                conn.pending.cancel()
            _log.info("device %s disconnected (%d connected)", device_id, len(self.connected_ids))

    async def disconnect(self, device_id: str, code: int = 4003, reason: str = "revoked") -> bool:
        """Close a device's socket: a revoked device must not keep driving Revit."""
        conn = self._devices.get(device_id)
        if conn is None:
            return False
        try:
            await conn.ws.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - it may already be gone
            pass
        self.unregister(device_id)
        return True

    def get_connection(self, device_id: str) -> DeviceConnection | None:
        conn = self._devices.get(device_id)
        return conn if conn and conn.connected else None

    # -- status ----------------------------------------------------------------

    def get_status(self) -> dict:
        """What ``GET /slots`` publishes: two numbers. No device ids - a visitor must
        not learn which devices exist."""
        return {"max_devices": self.max_devices, "connected": len(self.connected_ids)}

    def device_status(self, device_id: str) -> dict:
        """The relay's half of ``GET /devices/{id}``: connected, since when, how busy."""
        conn = self.get_connection(device_id)
        if conn is None:
            return {"online": False, "connected_at": None, "requests": 0}
        return {"online": True, "connected_at": conn.connected_at, "requests": conn.request_count}

    # -- messages --------------------------------------------------------------

    def resolve_response(self, device_id: str, data: str) -> None:
        """A message arrived from the add-in: it is the reply we are waiting for."""
        conn = self._devices.get(device_id)
        if conn and conn.pending and not conn.pending.done():
            conn.pending.set_result(data)
        else:
            _log.warning("device %s sent a message but nothing is pending", device_id)

    async def send_command(
        self, device_id: str, method: str, params: dict | None = None,
        timeout: float = 60.0,
    ) -> RevitResponse:
        """Send one JSON-RPC 2.0 request to the device and await its reply.

        Raises ``ConnectionError`` when the device has no add-in connected, the
        add-in leaves before answering or the socket cannot be written.
        """
        conn = self.get_connection(device_id)
        if not conn:
            raise ConnectionError(f"Device '{device_id}' has no connected Revit add-in")

        async with conn.lock:
            request_id = f"{int(time.time() * 1000)}{random.randint(100000, 999999)}"
            payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id}
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            raw = ""
            try:
                # The future must exist before the request leaves: the receive
                # loop may deliver a fast reply (say_hello) before send_text
                # returns, and resolve_response drops replies nobody waits for.
                conn.pending = loop.create_future()
                await conn.ws.send_text(json.dumps(payload, ensure_ascii=False))
                # Wait for the reply carrying our id; skip stray messages
                # instead of attributing them to this request.
                while True:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        return RevitResponse(success=False, error=f"Timeout after {timeout}s")
                    raw = await asyncio.wait_for(conn.pending, timeout=remaining)
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        return RevitResponse(success=False, error="Invalid JSON from Revit", raw=raw)
                    if parsed.get("id") != request_id:
                        _log.warning("device %s reply id mismatch: expected %s got %s",
                                     device_id, request_id, parsed.get("id"))
                        conn.pending = loop.create_future()  # keep waiting for ours
                        continue
                    break
                conn.request_count += 1
            except asyncio.TimeoutError:
                return RevitResponse(success=False, error=f"Timeout after {timeout}s")
            except asyncio.CancelledError:
                # unregister() cancels the pending future when the add-in leaves;
                # any other cancellation is the request itself being cancelled.
                if conn.pending is not None and conn.pending.cancelled():
                    raise ConnectionError(f"Device '{device_id}' left while waiting for a reply") from None
                raise
            except (WebSocketDisconnect, RuntimeError, OSError) as exc:
                # send_text on a closed socket (Starlette raises RuntimeError /
                # WebSocketDisconnect) - nothing reached Revit
                raise ConnectionError(f"Device '{device_id}' connection failed: {exc}") from exc
            finally:
                conn.pending = None

            if parsed.get("error"):
                err = parsed["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                # The code matters: -32001 is the designer saying No on the device,
                # which the package turns into declined_on_device.
                return RevitResponse(success=False, error=msg, raw=raw,
                                     error_code=code if isinstance(code, int) else None)
            return RevitResponse(success=True, result=parsed.get("result"), raw=raw)

    async def send_code(
        self, device_id: str, code: str, parameters: list | None = None,
        timeout: float = 60.0, confirm: dict | None = None,
    ) -> RevitResponse:
        """``send_code_to_revit`` with the same unwrapping as the TCP client.

        ``confirm`` (``{kind, title, message}``) asks the add-in to show the
        designer a Yes/No dialog first; such a request waits for a person, so
        never less than ``CONFIRM_TIMEOUT_SECONDS``.
        """
        params: dict = {"code": code, "parameters": parameters or []}
        if confirm is not None:
            params["confirm"] = confirm
            timeout = max(timeout, CONFIRM_TIMEOUT_SECONDS)
        resp = await self.send_command(device_id, "send_code_to_revit", params, timeout=timeout)
        if resp.success and isinstance(resp.result, dict) and "success" in resp.result:
            inner = resp.result
            inner_result = inner.get("result", "")
            if isinstance(inner_result, str) and inner_result.strip():
                try:
                    parsed = json.loads(inner_result)
                    inner_result = parsed if parsed is not None else inner_result
                except (json.JSONDecodeError, ValueError):
                    inner_result = {"raw_output": inner_result}
            ok = bool(inner.get("success", False))
            if ok and inner_result is None:
                inner_result = {"Status": "Success", "Message": "Code executed (no return)"}
            elif ok and inner_result == "":
                inner_result = {"Status": "Success", "Message": "Code executed (empty return)"}
            return RevitResponse(
                success=ok,
                result=inner_result,
                error=inner.get("errorMessage") or resp.error,
                raw=resp.raw,
            )
        return resp


class WebSocketRevitClient:
    """Adapter: a device looks like ``RevitClient`` to routes and query helpers."""

    def __init__(self, relay: DeviceRelay, device_id: str, timeout: float = 60.0):
        self._relay = relay
        self._device_id = device_id
        self._timeout = timeout

    @property
    def connected(self) -> bool:
        return self._relay.get_connection(self._device_id) is not None

    async def ensure_connected(self) -> None:
        """The package calls this before its timed probes: a device with no add-in is a
        transport failure now, not a probe timeout later."""
        if not self.connected:
            raise ConnectionError(f"Device '{self._device_id}' has no connected Revit add-in")

    async def send_command(self, method: str, params: dict | None = None) -> RevitResponse:
        return await self._relay.send_command(self._device_id, method, params, self._timeout)

    async def send_code(self, code: str, parameters: list | None = None,
                        confirm: dict | None = None) -> RevitResponse:
        return await self._relay.send_code(self._device_id, code, parameters, self._timeout, confirm=confirm)

    async def ping(self) -> bool:
        try:
            return (await self.send_command("say_hello", {"message": "ping"})).success
        except Exception:
            return False


_relay: DeviceRelay | None = None


def get_relay(max_devices: int | None = None) -> DeviceRelay:
    global _relay
    if _relay is None:
        if max_devices is None:
            from backend.config import get_settings
            max_devices = get_settings().max_devices
        _relay = DeviceRelay(max_devices=max_devices)
    return _relay


def reset_relay() -> None:
    global _relay
    _relay = None
