"""Revit routes - the v1 contract, every one a thin wrapper over ``revit_bridge``.

Prefix ``/api/v1/bridge``. Two transports to Revit:

- TCP: the add-in listens locally (``REVIT_BRIDGE_HOST:PORT``), used when no
  ``X-Slot-Id`` header is sent;
- WebSocket relay: a paired add-in connected to ``/ws/{device_id}``; the
  browser selects it with ``X-Device-Id`` + ``X-Device-Key`` (see
  ``backend.api.devices``).

The flow a page walks is the package's: ``take_snapshot`` -> ``missing_params``
/ ``reconcile`` -> ``validate_spec`` + ``Gate.issue`` (the token stays in the
browser) -> ``run_pack`` / ``run_code`` (sandbox, preconditions, validator,
evidence ledger, all inside the package) -> ``Ledger.recent`` / ``revalidate``.
``ToolStore`` (built-in packs from the wheel plus the user packs under
``REVIT_BRIDGE_DATA_DIR``) serves the pack routes.

Status codes: a request the route refuses as such is 400 (``confirmation_required``,
``invalid_category``, ``unknown_kind``, ``blocked``, ``no_validator``), a body that
parsed but is not valid is 422 (``invalid_args``, ``invalid_spec``, ``invalid_snapshot``,
``invalid_pack``), something that does not exist is 404, a Revit that cannot be
reached *before* the run is 503 (a transport failure *during* the run is the
package's ``success: false`` without consuming the token), and a refusal by the
gate, a failed precondition or a failed validation is 200 with ``success: false``
- the same payloads the MCP tools return. Error bodies are ``{error, message?, ...}``
(see ``backend.api.errors``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import HTTPConnection

from revit_bridge import __version__ as bridge_version
from revit_bridge.capabilities import ToolStore
from revit_bridge.evidence.ledger import Ledger
from revit_bridge.execution import ExecutionResult, revalidate, run_code, run_pack
from revit_bridge.revit import RevitClientPool, RevitSettings, sandbox
from revit_bridge.revit.probe import check_connection
from revit_bridge.snapshot import RevitQueryExecutor
from revit_bridge.snapshot.project import ProjectSnapshot, take_snapshot, validate_categories
from revit_bridge.snapshot.query import run_query
from revit_bridge.spec.gate import Gate, confirmation_required
from revit_bridge.spec.models import TaskSpec
from revit_bridge.spec.rules import missing_params, reconcile, validate_spec

from backend.api import devices as devices_api
from backend.api.errors import ApiError, responses, revit_unreachable
from backend.config import get_settings
from backend.ratelimit import client_key, confirm_limiter
from backend.relay import WebSocketRevitClient, get_relay

_log = logging.getLogger("backend.bridge")

PUBLIC_PATHS = {"/api/v1/bridge/service-health", "/api/v1/bridge/slots"}
HOST = "web"                      # what the evidence ledger records as the host
HANDSHAKE_TIMEOUT = 10.0          # seconds an add-in has to send its auth message
CONFIRM_CHANNEL = "host_ui"       # confirmations come from the page, never from the model
MAX_EVIDENCE = 200

# The scope every confirmation and every ledger line belongs to: the device the
# request drives, or "local" for the add-in on this machine (package default).
LOCAL_SCOPE = "local"

# Which device this request drives, and the check behind it, live in backend.api.devices.
set_device_context = devices_api.set_device_context


def current_scope() -> str:
    """The scope of this request: its device id, or ``"local"`` without one."""
    return devices_api.current_device() or LOCAL_SCOPE


router = APIRouter(
    prefix="/api/v1/bridge",
    tags=["bridge"],
    dependencies=[Depends(set_device_context)],
)


# -- package state of this host ------------------------------------------------

# One gate per process: tokens live in its memory and under
# <evidence_dir>/pending/, so a page can confirm now and run a moment later.
_gate: Gate | None = None
_ledger: Ledger | None = None


def get_gate() -> Gate:
    global _gate
    if _gate is None:
        _gate = Gate()
    return _gate


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger


def reset_bridge_state() -> None:
    """Drop the gate and ledger (tests change the data root between cases)."""
    global _gate, _ledger
    _gate = None
    _ledger = None


async def get_revit_client():
    """The selected device's relay client, or the local TCP client (503 when neither answers)."""
    device_id = devices_api.current_device()
    if device_id:
        relay = get_relay()
        if not relay.get_connection(device_id):
            raise ApiError(503, "revit_unreachable", f"Device {device_id} has no connected Revit add-in")
        return WebSocketRevitClient(relay, device_id, timeout=RevitSettings.from_env().timeout)
    try:
        return await RevitClientPool.get_client()
    except OSError as exc:
        s = RevitSettings.from_env()
        raise revit_unreachable(exc, endpoint=f"{s.host}:{s.port}") from None


def unknown_tool(name: str) -> ApiError:
    return ApiError(404, "unknown_tool", f"Tool '{name}' not found", tool=name)


def parse_snapshot(data: dict | None) -> ProjectSnapshot | None:
    if data is None:
        return None
    try:
        return ProjectSnapshot.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "invalid_snapshot", str(exc)) from None


def parse_spec(data: dict) -> TaskSpec:
    try:
        return TaskSpec.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "invalid_spec", str(exc)) from None


def pack_for(spec: TaskSpec, store: ToolStore):
    if spec.action.kind == "run_tool" and spec.action.tool:
        return store.load(spec.action.tool)
    return None


async def snapshot_now(client, categories: list[str] | None = None) -> ProjectSnapshot:
    """A fresh snapshot for a route that was not given one (same mapping as the MCP tool)."""
    try:
        return await take_snapshot(client, categories)
    except OSError as exc:                     # refused, timed out, reset: no add-in
        raise revit_unreachable(exc) from None
    except Exception as exc:                   # a bug in the snapshot itself
        raise ApiError(500, "snapshot_failed", f"{type(exc).__name__}: {exc}") from None


def _execution_payload(result: ExecutionResult) -> dict:
    """The MCP reply shape: a refusal payload as is, otherwise the ExecutionResult fields."""
    if result.refusal is not None:
        return result.refusal
    payload = result.model_dump(mode="json", exclude={"refusal"})
    if payload["hint"] is None:
        del payload["hint"]
    return payload


def _require_token(token: str) -> str:
    """No token is a 400 before anything is looked at; the gate judges the rest."""
    if not isinstance(token, str) or not token.strip():
        refusal = confirmation_required()          # the package's payload, verbatim
        raise ApiError(400, refusal.pop("error"), **refusal)
    return token.strip()


# -- request models ------------------------------------------------------------

class QueryRequest(BaseModel):
    kind: str
    args: dict = Field(default_factory=dict)


class MissingParamsRequest(BaseModel):
    known: dict = Field(default_factory=dict)
    snapshot: dict | None = None
    language: str = "zh"


class ReconcileRequest(BaseModel):
    spec: dict
    snapshot: dict | None = None


class ConfirmRequest(BaseModel):
    spec: dict
    confirmed_by: str = "designer"


class RunToolRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    token: str = ""


class ExecuteRequest(BaseModel):
    code: str
    parameters: list | None = None
    token: str = ""


class SolidifyRequest(BaseModel):
    name: str
    code: str
    description: str = ""
    parameters: list[dict] = Field(default_factory=list)   # v1: {name, type, description, source, required, unit?, ...}
    source_query: str = ""
    validator: dict | None = None


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


def tool_listing(tool) -> dict:
    """One item of ``GET /tools``: the MCP ``list_tools`` shape."""
    return {
        "name": tool.name,
        "description": tool.description,
        "version": tool.version,
        "parameters": [
            {k: p[k] for k in ("name", "type", "source", "required", "unit", "choices_from", "default") if k in p}
            for p in tool.parameters
        ],
        "preconditions": tool.preconditions,
        "validator": (tool.validator or {}).get("kind"),
        "used": tool.execution_count,
    }


def _tool_detail(tool) -> dict:
    return {
        "name": tool.name,
        "display_name": tool.display_name,
        "description": tool.description,
        "version": tool.version,
        "parameters": tool.parameters,
        "tags": tool.tags,
        "execution_count": tool.execution_count,
        "code_template": tool.code_template,
        "source_query": tool.source_query,
        "preconditions": tool.preconditions,
        "applies_when": tool.applies_when,
        "not_for": tool.not_for,
        "validator": tool.validator,
    }


# ``ToolStore.solidify`` / ``update`` validate the pack (undeclared ``{placeholder}``,
# malformed parameter, unknown validator) and raise ValueError listing the problems.
_INVALID_PACK_PREFIX = "invalid capability pack: "


def _invalid_pack(exc: ValueError) -> ApiError:
    text = str(exc)
    if text.startswith(_INVALID_PACK_PREFIX):
        text = text[len(_INVALID_PACK_PREFIX):]
    return ApiError(422, "invalid_pack", problems=text.split("; "))


# -- the model, read-only --------------------------------------------------------

@router.get("/snapshot", responses=responses(400, 403, 422, 500, 503))
async def get_snapshot(categories: list[str] | None = Query(default=None)):
    """``take_snapshot``: the open model as a ``ProjectSnapshot`` (units, levels, grids,
    selection, the family types of ``categories`` - repeat the parameter or separate
    names with commas; absent: walls, columns, framing, floors, doors, windows;
    ``categories=`` empty: no family types)."""
    names = None
    if categories is not None:
        names = [c.strip() for value in categories for c in value.split(",") if c.strip()]
    try:
        cats = validate_categories(names)
    except ValueError as exc:
        raise ApiError(400, "invalid_category", str(exc)) from None
    client = await get_revit_client()
    snapshot = await snapshot_now(client, cats)
    return snapshot.model_dump(mode="json")


@router.post("/query", responses=responses(400, 403, 422, 500, 503))
async def query_model(req: QueryRequest):
    """``run_query``: one read-only query by kind (levels, grids, family_types, elements,
    selection, view_elements, units, counts). The package's answer is returned as is;
    an unknown kind is 400, bad args 422, Revit reporting an error is 200 with ``error``."""
    try:
        client = await get_revit_client()
        answer = await run_query(RevitQueryExecutor(client), req.kind, req.args)
    except ApiError as exc:
        exc.payload.setdefault("kind", req.kind)
        raise
    except OSError as exc:
        raise revit_unreachable(exc, kind=req.kind) from None
    except Exception as exc:  # noqa: BLE001 - a bug in the query, never a bad request
        raise ApiError(500, "query_failed", f"{type(exc).__name__}: {exc}", kind=req.kind) from None
    if answer.get("error") == "unknown_kind":
        raise ApiError(400, **answer)
    if answer.get("error") == "invalid_args":
        raise ApiError(422, **answer)
    return answer


# -- capability packs ----------------------------------------------------------

@router.get("/tools", responses=responses(403))
async def list_tools():
    """``ToolStore.list_tools`` in the MCP ``list_tools`` shape."""
    return [tool_listing(t) for t in ToolStore().list_tools()]


@router.get("/tools/{name}", responses=responses(403, 404, 422))
async def get_tool(name: str):
    tool = ToolStore().load(name)
    if not tool:
        raise unknown_tool(name)
    return _tool_detail(tool)


@router.put("/tools/{name}", responses=responses(400, 403, 404, 422))
async def update_tool(name: str, req: UpdateToolRequest):
    """Change editable fields; the package validates the result as a v1 pack (422 with problems)."""
    updates = req.model_dump(exclude_unset=True)
    if "code_template" in updates:
        safe, warnings = sandbox.review(updates["code_template"] or "")
        if not safe:
            raise ApiError(400, "blocked", "Code review failed", warnings=warnings)
    try:
        tool = ToolStore().update(name, updates)
    except ValueError as exc:
        raise _invalid_pack(exc) from None
    if not tool:
        raise unknown_tool(name)
    return {"status": "updated", **_tool_detail(tool), "revit_synced": await _sync_to_revit(tool)}


@router.delete("/tools/{name}", responses=responses(403, 404, 422))
async def delete_tool(name: str):
    if ToolStore().delete(name):
        return {"status": "deleted", "name": name}
    raise unknown_tool(name)


@router.get("/tools/{name}/choices", responses=responses(403, 404, 422, 503, 504))
async def get_tool_choices(name: str):
    """Real values for the pack's dynamic parameters (levels, types, elements)."""
    store = ToolStore()
    if not store.load(name):
        raise unknown_tool(name)
    dynamic = store.get_dynamic_params(name)
    if not dynamic:
        return {}
    client = await get_revit_client()
    try:
        return await asyncio.wait_for(RevitQueryExecutor(client).get_tool_choices(dynamic), timeout=15.0)
    except asyncio.TimeoutError:
        raise ApiError(504, "revit_timeout", "Revit query timed out (15s)") from None
    except OSError as exc:
        raise revit_unreachable(exc) from None


@router.post("/tools/{name}/missing-params", responses=responses(403, 404, 422))
async def tool_missing_params(name: str, req: MissingParamsRequest):
    """``missing_params``: the questions still open for the pack given ``known`` values,
    with the real options from ``snapshot`` (or one taken now, best effort)."""
    pack = ToolStore().load(name)
    if pack is None:
        raise unknown_tool(name)
    snapshot = parse_snapshot(req.snapshot)
    if req.snapshot is None:
        try:
            snapshot = await take_snapshot(await get_revit_client())
        except Exception:  # noqa: BLE001 - the questions still go out, without options
            snapshot = None
    return [q.model_dump(mode="json") for q in missing_params(pack, req.known, snapshot, req.language)]


@router.post("/tools/{name}/run", responses=responses(400, 403, 404, 422, 503))
async def run_tool(name: str, req: RunToolRequest):
    """``run_pack``: the confirmed pack under its token - health, render, sandbox, preconditions,
    validator, evidence, all in the package. An unknown pack is 404 and a missing token
    400 ``confirmation_required`` before anything else is looked at."""
    store = ToolStore()
    if store.load(name) is None:
        raise unknown_tool(name)
    token = _require_token(req.token)
    client = await get_revit_client()
    result = await run_pack(store=store, gate=get_gate(), ledger=get_ledger(), client=client,
                            name=name, params=req.params, token=token, host=HOST, scope=current_scope())
    return _execution_payload(result)


# -- specs ---------------------------------------------------------------------

@router.post("/spec/reconcile", responses=responses(403, 422, 500, 503))
async def reconcile_spec(req: ReconcileRequest):
    """``reconcile``: the draft TaskSpec against the snapshot (given, or taken now)."""
    draft = parse_spec(req.spec)
    store = ToolStore()
    snapshot = parse_snapshot(req.snapshot)
    if snapshot is None:
        snapshot = await snapshot_now(await get_revit_client())
    return reconcile(draft, snapshot, pack_for(draft, store)).model_dump(mode="json")


def confirm_rate_limit(request: Request) -> None:
    """Each confirmation writes a pending file to the data volume: CHAT_RATE_LIMIT per minute per address."""
    if not confirm_limiter.allow(client_key(request), get_settings().chat_rate_limit):
        raise ApiError(429, "rate_limited", "Too many confirmations from this address; try again in a minute")


@router.post("/spec/confirm", dependencies=[Depends(confirm_rate_limit)], responses=responses(403, 422, 429))
async def confirm_spec(req: ConfirmRequest):
    """``validate_spec`` + ``Gate.issue(channel="host_ui")``: the designer confirmed the card.
    Returns ``{token, spec_hash, expires_at, card}``; an invalid spec is 422 ``{errors}``.
    The token belongs to the browser session and never enters the model context."""
    try:
        spec = TaskSpec.model_validate(req.spec)
    except ValidationError as exc:
        raise ApiError(422, "invalid_spec", errors=[{"code": "invalid_spec", "param": None, "message": str(exc)}]) from None
    errors = validate_spec(spec, pack_for(spec, ToolStore()))
    if errors:
        raise ApiError(422, "invalid_spec", errors=[e.model_dump() for e in errors])
    # issue() writes pending/<id>.json and globs the directory: not on the loop that drives the relay
    conf = await run_in_threadpool(get_gate().issue, spec, confirmed_by=req.confirmed_by.strip() or "designer",
                                   channel=CONFIRM_CHANNEL, scope=current_scope())
    return {"token": conf.token, "spec_hash": conf.spec_hash, "expires_at": conf.expires_at, "card": spec.card()}


# -- execution -----------------------------------------------------------------

@router.post("/execute", responses=responses(400, 403, 422, 503))
async def execute_code(req: ExecuteRequest):
    """``run_code``: the confirmed C# under its token; the sandbox review and the ledger are the package's."""
    token = _require_token(req.token)
    client = await get_revit_client()
    result = await run_code(gate=get_gate(), ledger=get_ledger(), client=client, code=req.code,
                            parameters=req.parameters, token=token, host=HOST, scope=current_scope())
    return _execution_payload(result)


@router.post("/solidify", responses=responses(400, 403, 422))
async def solidify_tool(req: SolidifyRequest):
    """``ToolStore.solidify``: save working code as a v1 pack; best-effort sync to the add-in."""
    safe, warnings = sandbox.review(req.code)
    if not safe:
        raise ApiError(400, "blocked", "Code review failed", warnings=warnings)
    try:
        tool = ToolStore().solidify(
            name=req.name, code=req.code, description=req.description, parameters=req.parameters,
            source_query=req.source_query, validator=req.validator,
        )
    except ValueError as exc:
        raise _invalid_pack(exc) from None
    return {
        "status": "solidified",
        "name": tool.name,
        "display_name": tool.display_name,
        "version": tool.version,
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


# -- evidence ------------------------------------------------------------------

@router.get("/evidence", responses=responses(403, 422))
async def list_evidence(limit: int = 20, tool: str | None = None):
    """``Ledger.recent``: the newest execution records of this device, optionally of one pack.

    A browser sees only what ran on the device it drives (or, without a device
    header, on the local add-in); the filter is silent."""
    # reads the monthly JSONL files: off the event loop
    return await run_in_threadpool(get_ledger().recent, max(1, min(limit, MAX_EVIDENCE)), tool or None,
                                   current_scope())


@router.post("/evidence/{evidence_id}/validate", responses=responses(400, 403, 404, 422, 503))
async def validate_evidence(evidence_id: str):
    """``revalidate``: the recorded execution's assertion against the model as it is now.
    An unknown record - or one of another device - is 404 before any Revit is needed."""
    ledger = get_ledger()
    record = await run_in_threadpool(ledger.get, evidence_id)
    if record is None or (record.get("scope") or LOCAL_SCOPE) != current_scope():
        # Another device's record reads as absent: its existence is not this browser's business.
        raise ApiError(404, "unknown_evidence", evidence_id=evidence_id)
    client = await get_revit_client()
    try:
        report = await revalidate(store=ToolStore(), ledger=ledger, client=client, evidence_id=evidence_id,
                                  scope=current_scope())
    except OSError as exc:
        raise revit_unreachable(exc) from None
    if report.get("error") in ("unknown_evidence", "scope_mismatch"):
        raise ApiError(404, "unknown_evidence", evidence_id=evidence_id)
    if report.get("error"):
        raise ApiError(400, **report)
    return report


# -- selection -----------------------------------------------------------------

@router.post("/trigger-selection", responses=responses(403, 503))
async def trigger_selection():
    """Put Revit into pick mode and return what the designer selected."""
    client = await get_revit_client()
    try:
        return {"elements": await RevitQueryExecutor(client).trigger_selection()}
    except OSError as exc:
        raise revit_unreachable(exc) from None


# -- health --------------------------------------------------------------------

@router.get("/revit-health", responses=responses(403, 422))
async def revit_health():
    """Is a Revit reachable: the selected device, else the local TCP add-in."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    device_id = devices_api.current_device()
    relay = get_relay()
    relay_status = relay.get_status()

    if device_id:
        conn = relay.get_connection(device_id)
        if not conn:
            return {
                "revit_connected": False, "latency_ms": None, "mode": "websocket",
                "detail": f"Device {device_id} has no connected Revit add-in",
                "bridge_version": bridge_version, "timestamp": now, "devices": relay_status,
            }
        t0 = time.monotonic()
        ok = await WebSocketRevitClient(relay, device_id, timeout=10).ping()
        return {
            "revit_connected": ok,
            "latency_ms": round((time.monotonic() - t0) * 1000) if ok else None,
            "mode": "websocket",
            "detail": f"Device {device_id} responded" if ok else f"Device {device_id} did not answer",
            "endpoint": f"device {device_id}",
            "bridge_version": bridge_version, "timestamp": now, "devices": relay_status,
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
            "bridge_version": bridge_version, "timestamp": now, "devices": relay_status,
        }
    return {
        "revit_connected": False, "latency_ms": None,
        "mode": "waiting_for_revit",
        "detail": f"Local add-in unreachable ({status['error']}); "
                  f"{relay_status['connected']} paired device(s) connected",
        "endpoint": f"{status['host']}:{status['port']}",
        "bridge_version": bridge_version, "timestamp": now, "devices": relay_status,
    }


@router.get("/service-health")
async def service_health():
    """Relay status only; never probes Revit."""
    relay_status = get_relay().get_status()
    return {
        "status": "ok",
        "remote_relay_ready": True,
        "connected_devices": relay_status["connected"],
        "websocket_endpoint": "/api/v1/bridge/ws/{device_id}",
        "devices": relay_status,
    }


@router.get("/slots")
async def get_slots():
    """How many devices may be connected and how many are: no ids, this is public."""
    return get_relay().get_status()


# -- add-in relay --------------------------------------------------------------

@router.websocket("/ws/{device_id}")
async def revit_ws_endpoint(ws: WebSocket, device_id: str):
    """A paired add-in connects here and authenticates with its device token.

    The first message must be ``{"type": "auth", "device_id": ..., "token": ...}``
    within ``HANDSHAKE_TIMEOUT`` seconds and must verify against the device
    store, else the socket closes with 4003 - an unknown device, a revoked one
    and a wrong token are indistinguishable from outside. 4002 means that
    device is already connected, 4001 that the relay is full.
    """
    relay = get_relay()
    await ws.accept()

    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=HANDSHAKE_TIMEOUT)
    except Exception:  # noqa: BLE001 - no handshake in time
        first = None
    if not await _handshake_ok(first, device_id):
        await ws.close(code=4003, reason="Invalid device token")
        return

    refused = relay.register(device_id, ws)
    if refused == "occupied":
        await ws.close(code=4002, reason="This device is already connected")
        return
    if refused is not None:
        await ws.close(code=4001, reason=f"Too many connected devices (max {relay.max_devices})")
        return
    await run_in_threadpool(devices_api.get_device_store().touch, device_id)

    _log.info("Revit add-in connected for device %s", device_id)
    try:
        while True:
            relay.resolve_response(device_id, await ws.receive_text())
    except WebSocketDisconnect:
        _log.info("Revit add-in of device %s left", device_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("device %s websocket error: %s", device_id, exc)
    finally:
        relay.unregister(device_id)


async def _handshake_ok(message: str | None, device_id: str) -> bool:
    """``{"type": "auth", "device_id": ..., "token": ...}`` for this device, verified by the store."""
    if not message:
        return False
    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("type") != "auth":
        return False
    if payload.get("device_id") != device_id:
        return False
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        return False
    store = devices_api.get_device_store()
    return await run_in_threadpool(store.verify_device, device_id, token) is not None


# -- devices -------------------------------------------------------------------

# Pairing, the device list and revocation (backend/api/devices.py) hang on this
# router, so they share its error contract - but the redeem route needs no device
# header: the add-in has only its pairing code.
devices_api.register(router)
