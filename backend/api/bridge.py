"""Revit routes - thin wrappers over ``revit_bridge``.

Prefix ``/api/v1/bridge``. Two transports to Revit:

- TCP: the add-in listens locally (``REVIT_BRIDGE_HOST:PORT``), used when no
  ``X-Slot-Id`` header is sent;
- WebSocket relay: a remote add-in connected to ``/ws/{slot_id}``; the
  browser selects it with ``X-Slot-Id`` (+ ``X-Slot-Token`` when tokens are
  configured).

Every route validates or executes through the package: ``sandbox.review``
before any code is dispatched, ``ToolStore`` for packs (built-in packs from
the wheel plus the user packs under the package's data root,
``REVIT_BRIDGE_DATA_DIR``), ``RevitQueryExecutor`` for model queries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from revit_bridge import __version__ as bridge_version
from revit_bridge.auth import parse_handshake_token, verify_slot_token
from revit_bridge.capabilities import ToolStore
from revit_bridge.mcp_server import check_connection
from revit_bridge.revit import RevitClientPool, RevitSettings, sandbox
from revit_bridge.snapshot import RevitQueryExecutor

from backend.config import get_settings
from backend.relay import WebSocketRevitClient, get_slot_manager

_log = logging.getLogger("backend.bridge")

PUBLIC_PATHS = {"/api/v1/bridge/service-health", "/api/v1/bridge/slots"}

# Set per request by the router dependency; read when a Revit client is needed.
request_slot_id: ContextVar[str | None] = ContextVar("slot_id", default=None)


async def set_slot_context(connection: HTTPConnection) -> None:
    """Read ``X-Slot-Id`` and enforce ``X-Slot-Token`` when tokens are configured.

    WebSocket connections authenticate with their first message instead.
    When ``MCP_BRIDGE_REQUIRE_SLOT_TOKEN`` is on, only relay status routes
    stay reachable without a slot.
    """
    slot_id = connection.headers.get("x-slot-id")
    request_slot_id.set(slot_id)
    settings = get_settings()
    if not slot_id:
        if (
            connection.scope.get("type") == "http"
            and settings.slot_token_required
            and connection.url.path not in PUBLIC_PATHS
        ):
            raise HTTPException(403, "Missing X-Slot-Id")
        return
    if not settings.slot_tokens:
        _log.warning("no slot tokens configured - X-Slot-Token check skipped (insecure)")
        return
    if not verify_slot_token(settings.slot_tokens, slot_id, connection.headers.get("x-slot-token")):
        raise HTTPException(403, "Invalid or missing X-Slot-Token")


router = APIRouter(
    prefix="/api/v1/bridge",
    tags=["bridge"],
    dependencies=[Depends(set_slot_context)],
)

# Unit preference of the UI (mm / m / feet); in-memory, host-wide.
_user_unit = "mm"


def _tcp_unreachable(exc: Exception | None = None) -> HTTPException:
    s = RevitSettings.from_env()
    return HTTPException(502, f"Cannot connect to the Revit add-in at {s.host}:{s.port}. Is Revit running?")


async def get_revit_client():
    """The selected slot's relay client, or the local TCP client."""
    slot_id = request_slot_id.get(None)
    if slot_id:
        mgr = get_slot_manager()
        if not mgr.get_connection(slot_id):
            raise HTTPException(502, f"Slot {slot_id} has no connected Revit add-in")
        return WebSocketRevitClient(mgr, slot_id, timeout=RevitSettings.from_env().timeout)
    try:
        return await RevitClientPool.get_client()
    except (ConnectionError, OSError) as exc:
        raise _tcp_unreachable(exc) from None


# -- request models ------------------------------------------------------------

class ExecuteRequest(BaseModel):
    code: str
    parameters: list | None = None


class SolidifyRequest(BaseModel):
    name: str
    code: str
    description: str = ""
    parameters: list[dict] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_query: str = ""


class UpdateToolRequest(BaseModel):
    display_name: str | None = None
    description: str | None = None
    code_template: str | None = None
    parameters: list[dict] | None = None
    tags: list[str] | None = None
    source_query: str | None = None
    preconditions: list[str] | None = None
    applies_when: list[str] | None = None
    not_for: list[str] | None = None


class RunToolRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class QueryRevitRequest(BaseModel):
    command: str
    params: dict = Field(default_factory=dict)


class UnitSettingRequest(BaseModel):
    unit: str


def _tool_summary(tool) -> dict:
    return {
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "parameters": tool.parameters,
        "tags": tool.tags,
        "execution_count": tool.execution_count,
    }


def _tool_detail(tool) -> dict:
    return {
        **_tool_summary(tool),
        "code_template": tool.code_template,
        "source_query": tool.source_query,
        "preconditions": tool.preconditions,
        "applies_when": tool.applies_when,
        "not_for": tool.not_for,
    }


# -- units ---------------------------------------------------------------------

@router.get("/unit")
async def get_unit():
    return {"unit": _user_unit}


@router.post("/unit")
async def set_unit(req: UnitSettingRequest):
    global _user_unit
    if req.unit not in ("mm", "m", "feet"):
        raise HTTPException(400, f"Invalid unit '{req.unit}'. Must be mm, m, or feet.")
    _user_unit = req.unit
    return {"unit": _user_unit, "status": "updated"}


@router.get("/project-units")
async def get_project_units():
    """Ask Revit which length unit the project displays."""
    try:
        client = await get_revit_client()
        units = await RevitQueryExecutor(client).get_project_units()
    except HTTPException as exc:
        return {"error": exc.detail, "current_setting": _user_unit}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "current_setting": _user_unit}
    return {**units, "current_setting": _user_unit}


# -- execution -----------------------------------------------------------------

@router.post("/execute")
async def execute_code(req: ExecuteRequest):
    """Send C# to Revit after the sandbox review."""
    safe, warnings = sandbox.review(req.code)
    if not safe:
        raise HTTPException(400, detail={"error": "blocked", "warnings": warnings})
    client = await get_revit_client()
    try:
        resp = await client.send_code(req.code, req.parameters)
    except (ConnectionError, OSError) as exc:
        raise _tcp_unreachable(exc) from None
    return {"success": resp.success, "result": resp.result, "error": resp.error}


@router.post("/solidify")
async def solidify_tool(req: SolidifyRequest):
    """Save working code as a capability pack; best-effort sync to the add-in."""
    safe, warnings = sandbox.review(req.code)
    if not safe:
        raise HTTPException(400, detail={"error": "blocked", "warnings": warnings})
    store = ToolStore()
    tool = store.solidify(
        name=req.name, code=req.code, description=req.description,
        parameters=req.parameters, tags=req.tags, source_query=req.source_query,
    )
    return {
        "status": "solidified",
        "name": tool.name,
        "display_name": tool.display_name,
        "revit_synced": await _sync_to_revit(tool),
    }


async def _sync_to_revit(tool) -> bool:
    try:
        client = await get_revit_client()
        resp = await client.send_command("manage_solidified_tools", {
            "action": "register",
            "name": tool.name,
            "code_template": tool.code_template,
            "description": tool.description,
            "parameters": tool.parameters,
            "source_query": tool.source_query,
        })
        return bool(resp.success)
    except Exception:  # noqa: BLE001 - Revit may be absent; the YAML is the source of truth
        return False


# -- capability packs ----------------------------------------------------------

@router.get("/tools")
async def list_tools():
    return [_tool_summary(t) for t in ToolStore().list_tools()]


@router.get("/tools/{name}")
async def get_tool(name: str):
    tool = ToolStore().load(name)
    if not tool:
        raise HTTPException(404, f"Tool '{name}' not found")
    return _tool_detail(tool)


@router.put("/tools/{name}")
async def update_tool(name: str, req: UpdateToolRequest):
    updates = req.model_dump(exclude_unset=True)
    if "code_template" in updates:
        safe, warnings = sandbox.review(updates["code_template"] or "")
        if not safe:
            raise HTTPException(422, f"Code review failed: {'; '.join(warnings)}")
    tool = ToolStore().update(name, updates)
    if not tool:
        raise HTTPException(404, f"Tool '{name}' not found")
    return {"status": "updated", **_tool_detail(tool), "revit_synced": await _sync_to_revit(tool)}


@router.delete("/tools/{name}")
async def delete_tool(name: str):
    if ToolStore().delete(name):
        return {"status": "deleted", "name": name}
    raise HTTPException(404, f"Tool '{name}' not found")


@router.get("/tools/{name}/choices")
async def get_tool_choices(name: str):
    """Real values for the pack's dynamic parameters (levels, types, elements)."""
    store = ToolStore()
    if not store.load(name):
        raise HTTPException(404, f"Tool '{name}' not found")
    dynamic = store.get_dynamic_params(name)
    if not dynamic:
        return {}
    client = await get_revit_client()
    try:
        return await asyncio.wait_for(RevitQueryExecutor(client).get_tool_choices(dynamic), timeout=15.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Revit query timed out (15s)") from None
    except (ConnectionError, OSError) as exc:
        raise _tcp_unreachable(exc) from None


@router.post("/tools/{name}/run")
async def run_tool(name: str, req: RunToolRequest):
    """Render the pack with the given parameters, review, execute."""
    store = ToolStore()
    if not store.load(name):
        raise HTTPException(404, f"Tool '{name}' not found")
    valid, errors, _ = store.validate_params(name, req.params)
    if not valid:
        raise HTTPException(422, f"Parameter validation failed: {'; '.join(errors)}")
    code = store.render_code(name, req.params)
    safe, warnings = sandbox.review(code or "")
    if not safe:
        raise HTTPException(400, detail={"error": "blocked", "warnings": warnings})
    client = await get_revit_client()
    try:
        resp = await client.send_code(code)
    except (ConnectionError, OSError) as exc:
        store.record_usage(name, success=False)
        raise _tcp_unreachable(exc) from None
    store.record_usage(name, success=resp.success)
    return {"success": resp.success, "tool": name, "result": resp.result, "error": resp.error}


# -- model queries -------------------------------------------------------------

@router.post("/query-revit")
async def query_revit(req: QueryRevitRequest):
    """Run an add-in command (or one of the known read queries)."""
    client = await get_revit_client()
    executor = RevitQueryExecutor(client)
    try:
        if req.command == "get_available_family_types":
            return {"result": await executor.get_family_types(req.params.get("categoryList", []))}
        if req.command == "get_levels":
            return {"result": await executor.get_levels()}
        if req.command == "get_selected_elements":
            return {"result": await executor.get_selected_elements()}
        resp = await client.send_command(req.command, req.params)
        return {"result": resp.result, "error": resp.error}
    except (ConnectionError, OSError) as exc:
        raise _tcp_unreachable(exc) from None


@router.post("/trigger-selection")
async def trigger_selection():
    """Put Revit into pick mode and return what the designer selected."""
    client = await get_revit_client()
    try:
        return {"elements": await RevitQueryExecutor(client).trigger_selection()}
    except (ConnectionError, OSError) as exc:
        raise _tcp_unreachable(exc) from None


# -- health --------------------------------------------------------------------

@router.get("/revit-health")
async def revit_health():
    """Is a Revit reachable: the selected slot, else the local TCP add-in."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    slot_id = request_slot_id.get(None)
    mgr = get_slot_manager()
    slot_status = mgr.get_status()

    if slot_id:
        conn = mgr.get_connection(slot_id)
        if not conn:
            return {
                "revit_connected": False, "latency_ms": None, "mode": "websocket",
                "detail": f"Slot {slot_id} has no connected Revit add-in",
                "bridge_version": bridge_version, "timestamp": now, "ws_slots": slot_status,
            }
        t0 = time.monotonic()
        ok = await WebSocketRevitClient(mgr, slot_id, timeout=10).ping()
        return {
            "revit_connected": ok,
            "latency_ms": round((time.monotonic() - t0) * 1000) if ok else None,
            "mode": "websocket",
            "detail": f"Slot {slot_id} responded" if ok else f"Slot {slot_id} did not answer",
            "endpoint": f"slot {slot_id}",
            "bridge_version": bridge_version, "timestamp": now, "ws_slots": slot_status,
        }

    t0 = time.monotonic()
    status = await check_connection()
    latency = round((time.monotonic() - t0) * 1000)
    if status["reachable"]:
        return {
            "revit_connected": True, "latency_ms": latency, "mode": "tcp",
            "detail": "Local add-in responded",
            "endpoint": f"{status['host']}:{status['port']}",
            "protocol": "JSON-RPC 2.0 / TCP",
            "bridge_version": bridge_version, "timestamp": now, "ws_slots": slot_status,
        }
    return {
        "revit_connected": False, "latency_ms": None,
        "mode": "waiting_for_revit",
        "detail": f"Local add-in unreachable ({status['error']}); "
                  f"{slot_status['connected']} remote slot(s) connected",
        "endpoint": f"{status['host']}:{status['port']}",
        "bridge_version": bridge_version, "timestamp": now, "ws_slots": slot_status,
    }


@router.get("/service-health")
async def service_health():
    """Relay status only; never probes Revit."""
    slot_status = get_slot_manager().get_status()
    return {
        "status": "ok",
        "remote_relay_ready": True,
        "connected_slots": slot_status["connected"],
        "websocket_endpoint": "/api/v1/bridge/ws/{slot_id}",
        "slots": slot_status,
    }


@router.get("/slots")
async def get_slots():
    return get_slot_manager().get_status()


# -- add-in relay --------------------------------------------------------------

@router.websocket("/ws/{slot_id}")
async def revit_ws_endpoint(ws: WebSocket, slot_id: str):
    """Remote add-in connects here and registers on a slot.

    With tokens configured the first message must be the add-in's auth
    handshake (``{"type": "auth", "slot_id": ..., "token": ...}``).
    """
    mgr = get_slot_manager()
    # Exact literal match: str.isdigit()/int() would also accept "01" or
    # Unicode digits, registering a key no X-Slot-Id or token lookup matches.
    if slot_id not in mgr.slot_ids:
        await ws.close(code=4001, reason=f"Invalid slot_id. Use 1-{mgr.max_slots}")
        return

    await ws.accept()

    # Tokens were loaded and validated at startup (WebSettings.from_env);
    # a misconfigured deployment never reaches this point.
    tokens = get_settings().slot_tokens
    if tokens:
        try:
            first = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:  # noqa: BLE001 - no handshake in time
            first = None
        if not verify_slot_token(tokens, slot_id, parse_handshake_token(first)):
            await ws.close(code=4003, reason="Invalid slot token")
            return
    else:
        _log.warning("no slot tokens configured - WebSocket slot auth skipped (insecure)")

    if not mgr.register(slot_id, ws):
        await ws.close(code=4002, reason="Slot occupied")
        return

    _log.info("Revit add-in connected on slot %s", slot_id)
    try:
        while True:
            mgr.resolve_response(slot_id, await ws.receive_text())
    except WebSocketDisconnect:
        _log.info("Revit add-in left slot %s", slot_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("slot %s websocket error: %s", slot_id, exc)
    finally:
        mgr.unregister(slot_id)
