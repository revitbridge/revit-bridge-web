"""WebSocket slot relay: remote Revit add-ins connect here, the host talks back.

The add-in opens ``/api/v1/bridge/ws/{slot_id}`` towards the server; the
server keeps ``slot_id -> WebSocket`` and forwards JSON-RPC 2.0 requests over
that socket. Browser requests select a slot with ``X-Slot-Id``. One slot
handles one request at a time (Revit executes serially).

``WebSocketRevitClient`` gives a slot the same ``send_command`` /
``send_code`` / ``ping`` surface as ``revit_bridge.revit.RevitClient`` so the
routes and ``revit_bridge.snapshot.RevitQueryExecutor`` do not care which
transport is underneath.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

from revit_bridge.revit import RevitResponse

_log = logging.getLogger("backend.relay")


@dataclass
class SlotConnection:
    """One Revit add-in connection on a named slot."""
    slot_id: str
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


class SlotManager:
    """Registry of add-in connections, keyed by slot id."""

    def __init__(self, max_slots: int = 5):
        self.max_slots = max_slots
        self.slot_ids: frozenset[str] = frozenset(str(i) for i in range(1, max_slots + 1))
        self._slots: dict[str, SlotConnection] = {}

    # -- registration ----------------------------------------------------------

    def register(self, slot_id: str, ws: WebSocket) -> bool:
        """Claim a slot for a connection. False when the id is unknown or the slot is occupied."""
        if slot_id not in self.slot_ids:
            return False
        if slot_id in self._slots and self._slots[slot_id].connected:
            return False
        self._slots[slot_id] = SlotConnection(slot_id=slot_id, ws=ws)
        _log.info("slot %s registered (total %d)", slot_id, len(self._slots))
        return True

    def unregister(self, slot_id: str) -> None:
        conn = self._slots.pop(slot_id, None)
        if conn:
            if conn.pending and not conn.pending.done():
                conn.pending.cancel()
            _log.info("slot %s unregistered (total %d)", slot_id, len(self._slots))

    def get_connection(self, slot_id: str) -> SlotConnection | None:
        conn = self._slots.get(slot_id)
        return conn if conn and conn.connected else None

    # -- status ----------------------------------------------------------------

    def get_status(self) -> dict:
        slots = {}
        for i in range(1, self.max_slots + 1):
            sid = str(i)
            conn = self._slots.get(sid)
            if conn and conn.connected:
                slots[sid] = {
                    "status": "connected",
                    "connected_at": conn.connected_at,
                    "requests": conn.request_count,
                }
            else:
                slots[sid] = {"status": "free"}
        return {
            "max_slots": self.max_slots,
            "connected": sum(1 for c in self._slots.values() if c.connected),
            "slots": slots,
        }

    # -- messages --------------------------------------------------------------

    def resolve_response(self, slot_id: str, data: str) -> None:
        """A message arrived from the add-in: it is the reply we are waiting for."""
        conn = self._slots.get(slot_id)
        if conn and conn.pending and not conn.pending.done():
            conn.pending.set_result(data)
        else:
            _log.warning("slot %s sent a message but nothing is pending", slot_id)

    async def send_command(
        self, slot_id: str, method: str, params: dict | None = None,
        timeout: float = 60.0,
    ) -> RevitResponse:
        """Send one JSON-RPC 2.0 request over the slot and await its reply."""
        conn = self.get_connection(slot_id)
        if not conn:
            return RevitResponse(success=False, error=f"Slot '{slot_id}' not connected")

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
                        _log.warning("slot %s reply id mismatch: expected %s got %s",
                                     slot_id, request_id, parsed.get("id"))
                        conn.pending = loop.create_future()  # keep waiting for ours
                        continue
                    break
                conn.request_count += 1
            except asyncio.TimeoutError:
                return RevitResponse(success=False, error=f"Timeout after {timeout}s")
            except Exception as exc:
                return RevitResponse(success=False, error=str(exc))
            finally:
                conn.pending = None

            if parsed.get("error"):
                err = parsed["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                return RevitResponse(success=False, error=msg, raw=raw)
            return RevitResponse(success=True, result=parsed.get("result"), raw=raw)

    async def send_code(
        self, slot_id: str, code: str, parameters: list | None = None,
        timeout: float = 60.0,
    ) -> RevitResponse:
        """``send_code_to_revit`` with the same unwrapping as the TCP client."""
        resp = await self.send_command(
            slot_id, "send_code_to_revit", {"code": code, "parameters": parameters or []},
            timeout=timeout,
        )
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
    """Adapter: a slot looks like ``RevitClient`` to routes and query helpers."""

    def __init__(self, manager: SlotManager, slot_id: str, timeout: float = 60.0):
        self._mgr = manager
        self._slot_id = slot_id
        self._timeout = timeout

    @property
    def connected(self) -> bool:
        return self._mgr.get_connection(self._slot_id) is not None

    async def send_command(self, method: str, params: dict | None = None) -> RevitResponse:
        return await self._mgr.send_command(self._slot_id, method, params, self._timeout)

    async def send_code(self, code: str, parameters: list | None = None) -> RevitResponse:
        return await self._mgr.send_code(self._slot_id, code, parameters, self._timeout)

    async def ping(self) -> bool:
        try:
            return (await self.send_command("say_hello", {"message": "ping"})).success
        except Exception:
            return False


_manager: SlotManager | None = None


def get_slot_manager(max_slots: int | None = None) -> SlotManager:
    global _manager
    if _manager is None:
        if max_slots is None:
            from backend.config import get_settings
            max_slots = get_settings().max_slots
        _manager = SlotManager(max_slots=max_slots)
    return _manager


def reset_slot_manager() -> None:
    global _manager
    _manager = None
