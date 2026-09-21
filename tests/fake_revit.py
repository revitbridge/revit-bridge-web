"""A minimal stand-in for the Revit add-in's TCP JSON-RPC listener.

Speaks the wire format of the add-in's SocketService: one raw UTF-8 JSON
request per message, one raw JSON response written back, no delimiters.
The server runs on its own event loop in a background thread so the
synchronous tests (TestClient drives the app on another loop) can point
``REVIT_BRIDGE_PORT`` at it. This is a cut-down copy of the helper the
package keeps for its own tests; nothing is imported across repositories.
"""
from __future__ import annotations

import asyncio
import json
import threading

from revit_bridge.execution import DOCUMENT_PROBE


class FakeRevit:
    """``handler(request: dict) -> dict`` answers each JSON-RPC request."""

    def __init__(self, handler=None):
        self.handler = handler or self.default_handler
        self.requests: list[dict] = []
        self.port: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._writers: set[asyncio.StreamWriter] = set()

    # -- lifecycle ---------------------------------------------------------------

    def __enter__(self) -> FakeRevit:
        self._thread = threading.Thread(target=self._run, name="fake-revit", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("fake Revit did not start")
        return self

    def __exit__(self, *exc) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def start():
            self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        loop.run_until_complete(start())
        try:
            loop.run_forever()
        finally:
            # Clients (the app's pooled connection) keep their sockets open;
            # wait_closed() would wait for them, so drop them first.
            for writer in list(self._writers):
                writer.close()
            self._server.close()
            loop.run_until_complete(asyncio.wait_for(self._server.wait_closed(), timeout=2))
            loop.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            buf = b""
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    request = json.loads(buf.decode("utf-8"))
                except ValueError:
                    continue                      # incomplete: keep reading
                buf = b""
                self.requests.append(request)
                writer.write(json.dumps(self.handler(request)).encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    # -- response helpers ---------------------------------------------------------

    @staticmethod
    def ok(request_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def error(request_id, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @classmethod
    def code_result(cls, request_id, payload, success: bool = True, error: str = "") -> dict:
        """A send_code_to_revit reply: the inner result is a JSON string."""
        return cls.ok(request_id, {
            "success": success,
            "result": json.dumps(payload) if payload is not None else None,
            "errorMessage": error,
        })

    @classmethod
    def default_handler(cls, request: dict) -> dict:
        method, rid = request.get("method"), request.get("id")
        if method == "say_hello":
            return cls.ok(rid, {"message": "Hello from fake Revit"})
        if method == "send_code_to_revit":
            code = request.get("params", {}).get("code", "")
            if code == "return document.Title;":
                return cls.code_result(rid, "Project1")
            return cls.code_result(rid, {"Status": "Created", "ElementId": 4242})
        if method == "get_available_family_types":
            cats = request.get("params", {}).get("categoryList", [])
            return cls.ok(rid, [{"name": f"{c}-TypeA"} for c in cats])
        return cls.error(rid, -32601, f"Method '{method}' not found")


# -- a model with one level and some walls -----------------------------------------

SNAPSHOT_RESULT = {
    "Document": {"Title": "Project1", "RevitVersion": "2026", "IsWorkshared": False},
    "Units": {"LengthUnit": "autodesk.unit.unit:millimeters-1.0.1", "DisplayName": "Millimeters"},
    "ActiveView": {"Name": "Level 1", "ViewType": "FloorPlan", "Level": "L1"},
    "Levels": [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}, {"Id": 2, "Name": "L2", "ElevationMm": 3500.0}],
    "Grids": {"Count": 2, "Names": ["A", "1"]},
    "Selection": [], "SelectionCount": 0, "Links": [], "Phases": ["New Construction"],
    "CategoryNames": {"OST_Walls": "Walls"},
    "Warnings": [],
}

LEVELS_RESULT = [{"Id": 1, "Name": "L1", "ElevationMm": 0.0}, {"Id": 2, "Name": "L2", "ElevationMm": 3500.0}]


def model_handler(walls_before: int = 1, walls_after: int = 2, result=None):
    """A fake add-in behind a small model: answers the snapshot block, the level query,
    the count probe of a ``count_delta`` validator (``walls_before`` until code has run,
    ``walls_after`` after) and any other code with ``result``."""
    state = {"ran": False}

    def handler(request: dict) -> dict:
        method, rid = request.get("method"), request.get("id")
        if method == "send_code_to_revit":
            code = request["params"]["code"]
            if "CategoryNames" in code:                                    # the snapshot block
                return FakeRevit.code_result(rid, SNAPSHOT_RESULT)
            if code == DOCUMENT_PROBE:
                return FakeRevit.code_result(rid, {"Title": "Project1", "RevitVersion": "2026"})
            if code == "return document.Title;":
                return FakeRevit.code_result(rid, "Project1")
            if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
                return FakeRevit.code_result(rid, walls_after if state["ran"] else walls_before)
            if "OfClass(typeof(Level))" in code and "return levels;" in code:
                return FakeRevit.code_result(rid, LEVELS_RESULT)
            state["ran"] = True
            return FakeRevit.code_result(rid, result if result is not None else {"Status": "Created", "ElementId": 4242})
        if method == "get_available_family_types":
            # The add-in labels each type with its category's display name; the
            # snapshot groups by that label (see CategoryNames above).
            cats = request.get("params", {}).get("categoryList", [])
            return FakeRevit.ok(rid, [
                {"FamilyName": "Basic", "TypeName": f"{c}-TypeA", "Category": SNAPSHOT_RESULT["CategoryNames"].get(c, "")}
                for c in cats
            ])
        return FakeRevit.default_handler(request)
    return handler
