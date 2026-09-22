"""Revit routes - the v1 contract, every one a thin wrapper over ``revit_bridge``.

Prefix ``/api/v1/bridge``. Two transports to Revit:

- TCP: the add-in listens locally (``REVIT_BRIDGE_HOST:PORT``), used when no
  ``X-Slot-Id`` header is sent;
- WebSocket relay: a remote add-in connected to ``/ws/{slot_id}``; the
  browser selects it with ``X-Slot-Id`` (+ ``X-Slot-Token`` when tokens are
  configured).

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
reached is 503, and a refusal by the gate, a failed precondition or a failed
validation is 200 with ``success: false`` - the same payloads the MCP tools
return. Error bodies are ``{error, message?, ...}`` (see ``backend.api.errors``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import HTTPConnection

from revit_bridge import __version__ as bridge_version
from revit_bridge.auth import parse_handshake_token, verify_slot_token
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

from backend.api.errors import ApiError, responses, revit_unreachable
from backend.config import get_settings
from backend.ratelimit import client_key, confirm_limiter
from backend.relay import WebSocketRevitClient, get_slot_manager

_log = logging.getLogger("backend.bridge")

PUBLIC_PATHS = {"/api/v1/bridge/service-health", "/api/v1/bridge/slots"}
HOST = "web"                      # what the evidence ledger records as the host
CONFIRM_CHANNEL = "host_ui"       # confirmations come from the page, never from the model
MAX_EVIDENCE = 200

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
            raise ApiError(403, "missing_slot", "Missing X-Slot-Id")
        return
    if not settings.slot_tokens:
        _log.warning("no slot tokens configured - X-Slot-Token check skipped (insecure)")
        return
    if not verify_slot_token(settings.slot_tokens, slot_id, connection.headers.get("x-slot-token")):
        raise ApiError(403, "invalid_slot_token", "Invalid or missing X-Slot-Token")


router = APIRouter(
    prefix="/api/v1/bridge",
    tags=["bridge"],
    dependencies=[Depends(set_slot_context)],
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
    """The selected slot's relay client, or the local TCP client (503 when neither answers)."""
    slot_id = request_slot_id.get(None)
    if slot_id:
        mgr = get_slot_manager()
        if not mgr.get_connection(slot_id):
            raise ApiError(503, "revit_unreachable", f"Slot {slot_id} has no connected Revit add-in")
        return WebSocketRevitClient(mgr, slot_id, timeout=RevitSettings.from_env().timeout)
    try:
        return await RevitClientPool.get_client()
    except OSError as exc:
        s = RevitSettings.from_env()
        raise revit_unreachable(exc, endpoint=f"{s.host}:{s.port}") from None


def _unknown_tool(name: str) -> ApiError:
    return ApiError(404, "unknown_tool", f"Tool '{name}' not found", tool=name)


def _parse_snapshot(data: dict | None) -> ProjectSnapshot | None:
    if data is None:
        return None
    try:
        return ProjectSnapshot.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "invalid_snapshot", str(exc)) from None


def _parse_spec(data: dict) -> TaskSpec:
    try:
        return TaskSpec.model_validate(data)
    except ValidationError as exc:
        raise ApiError(422, "invalid_spec", str(exc)) from None


def _pack_for(spec: TaskSpec, store: ToolStore):
    if spec.action.kind == "run_tool" and spec.action.tool:
        return store.load(spec.action.tool)
    return None


async def _snapshot_now(client, categories: list[str] | None = None) -> ProjectSnapshot:
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


def _tool_listing(tool) -> dict:
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
    snapshot = await _snapshot_now(client, cats)
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
    return [_tool_listing(t) for t in ToolStore().list_tools()]


@router.get("/tools/{name}", responses=responses(403, 404, 422))
async def get_tool(name: str):
    tool = ToolStore().load(name)
    if not tool:
        raise _unknown_tool(name)
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
        raise _unknown_tool(name)
    return {"status": "updated", **_tool_detail(tool), "revit_synced": await _sync_to_revit(tool)}


@router.delete("/tools/{name}", responses=responses(403, 404, 422))
async def delete_tool(name: str):
    if ToolStore().delete(name):
        return {"status": "deleted", "name": name}
    raise _unknown_tool(name)


@router.get("/tools/{name}/choices", responses=responses(403, 404, 422, 503, 504))
async def get_tool_choices(name: str):
    """Real values for the pack's dynamic parameters (levels, types, elements)."""
    store = ToolStore()
    if not store.load(name):
        raise _unknown_tool(name)
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
        raise _unknown_tool(name)
    snapshot = _parse_snapshot(req.snapshot)
    if req.snapshot is None:
        try:
            snapshot = await take_snapshot(await get_revit_client())
        except Exception:  # noqa: BLE001 - the questions still go out, without options
            snapshot = None
    return [q.model_dump(mode="json") for q in missing_params(pack, req.known, snapshot, req.language)]


@router.post("/tools/{name}/run", responses=responses(400, 403, 422, 503))
async def run_tool(name: str, req: RunToolRequest):
    """``run_pack``: the confirmed pack under its token - health, render, sandbox, preconditions,
    validator, evidence, all in the package. Without a token: 400 ``confirmation_required``."""
    token = _require_token(req.token)
    client = await get_revit_client()
    result = await run_pack(store=ToolStore(), gate=get_gate(), ledger=get_ledger(), client=client,
                            name=name, params=req.params, token=token, host=HOST)
    return _execution_payload(result)


# -- specs ---------------------------------------------------------------------

@router.post("/spec/reconcile", responses=responses(403, 422, 500, 503))
async def reconcile_spec(req: ReconcileRequest):
    """``reconcile``: the draft TaskSpec against the snapshot (given, or taken now)."""
    draft = _parse_spec(req.spec)
    store = ToolStore()
    snapshot = _parse_snapshot(req.snapshot)
    if snapshot is None:
        snapshot = await _snapshot_now(await get_revit_client())
    return reconcile(draft, snapshot, _pack_for(draft, store)).model_dump(mode="json")


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
    errors = validate_spec(spec, _pack_for(spec, ToolStore()))
    if errors:
        raise ApiError(422, "invalid_spec", errors=[e.model_dump() for e in errors])
    conf = get_gate().issue(spec, confirmed_by=req.confirmed_by.strip() or "designer", channel=CONFIRM_CHANNEL)
    return {"token": conf.token, "spec_hash": conf.spec_hash, "expires_at": conf.expires_at, "card": spec.card()}


# -- execution -----------------------------------------------------------------

@router.post("/execute", responses=responses(400, 403, 422, 503))
async def execute_code(req: ExecuteRequest):
    """``run_code``: the confirmed C# under its token; the sandbox review and the ledger are the package's."""
    token = _require_token(req.token)
    client = await get_revit_client()
    result = await run_code(gate=get_gate(), ledger=get_ledger(), client=client, code=req.code,
                            parameters=req.parameters, token=token, host=HOST)
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
    """``Ledger.recent``: the newest execution records, optionally of one pack."""
    return get_ledger().recent(max(1, min(limit, MAX_EVIDENCE)), tool or None)


@router.post("/evidence/{evidence_id}/validate", responses=responses(400, 403, 404, 422, 503))
async def validate_evidence(evidence_id: str):
    """``revalidate``: the recorded execution's assertion against the model as it is now."""
    client = await get_revit_client()
    try:
        report = await revalidate(store=ToolStore(), ledger=get_ledger(), client=client, evidence_id=evidence_id)
    except OSError as exc:
        raise revit_unreachable(exc) from None
    if report.get("error") == "unknown_evidence":
        raise ApiError(404, **report)
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

@router.get("/revit-health", responses=responses(403))
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
