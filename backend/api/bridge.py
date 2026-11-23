"""Revit routes - the v1 contract, every one a thin wrapper over ``revit_bridge``.

Prefix ``/api/v1/bridge``. Two transports to Revit:

- TCP: the add-in listens locally (``REVIT_BRIDGE_HOST:PORT``), used when no
  ``X-Device-Id`` header is sent, and always on a local host;
- WebSocket relay (``DEPLOY_MODE=cloud`` only): a paired add-in connected to
  ``/ws/{device_id}``; the browser selects it with ``X-Device-Id`` +
  ``X-Device-Key`` (see ``backend.api.devices``).

``router`` holds the routes of both modes. ``relay_router`` (the add-in socket and
the device routes) is mounted on a cloud host only; a local host mounts
``cloud_only_router`` instead, which answers those paths 404 ``cloud_mode_only``.
On a local host the device headers are not read and the admin checks pass
without a password.

The flow a page walks is the package's: ``take_snapshot`` -> ``missing_params``
/ ``reconcile`` -> ``validate_spec`` + ``Gate.issue`` (the token stays in the
browser) -> ``run_pack`` / ``run_code`` (sandbox, preconditions, validator,
evidence ledger, all inside the package) -> ``Ledger.recent`` / ``revalidate``.
``ToolStore`` (built-in packs from the wheel plus the user packs in the
package's user capabilities directory) serves the pack routes, read from disk on
every call: a pack another process (the package's MCP server) writes there is
listed on the next request.

The workbench's routes: ``POST /check`` (``checking.check_code``: compile, never
run) and ``POST /dry-run`` (``dry_run.dry_run_code``: run and always roll back,
no token - the package's rule), ``GET /tools/{name}/export`` (the pack file as
stored) and ``POST /tools/import`` (a pack file, validated as the package
validates a pack file).

Built-in packs are read-only: a name whose pack ships in the wheel and has no
user file is never updated, hidden, replaced by a solidified pack or by an
import (409 ``builtin_read_only``). Every tool name that comes from a request is
checked before it reaches the file system (``require_tool_name``).

Status codes: a request the route refuses as such is 400 (``confirmation_required``,
``invalid_category``, ``unknown_kind``, ``blocked``, ``no_validator``), a body that
parsed but is not valid is 422 (``invalid_args``, ``invalid_spec``, ``invalid_snapshot``,
``invalid_pack``, ``invalid_tool_name``), something that does not exist is 404, a change
that would overwrite what is there is 409 (``builtin_read_only``, ``pack_exists``), a Revit that cannot be
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
import os
import tempfile
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.requests import HTTPConnection

from revit_bridge import __version__ as bridge_version
from revit_bridge import checking
from revit_bridge import dry_run as package_dry_run
from revit_bridge import guidance as _guidance
from revit_bridge.capabilities import ToolStore
from revit_bridge.capabilities.schema import IDENTIFIER_RE, validate_pack
from revit_bridge.paths import guidance_dir, guidance_layer_dirs
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
from backend.api.errors import ApiError, admin_responses, responses, revit_unreachable
from backend.config import get_settings
from backend.ratelimit import client_key, confirm_limiter, handshake_failures
from backend.relay import WebSocketRevitClient, get_relay

_log = logging.getLogger("backend.bridge")

PUBLIC_PATHS = {"/api/v1/bridge/service-health", "/api/v1/bridge/slots"}
HOST = "web"                      # what the evidence ledger records as the host
HANDSHAKE_TIMEOUT = 10.0          # seconds an add-in has to send its auth message
# What stops unauthenticated sockets piling up, per client address. Only FAILED
# handshakes count (a wrong token, an unknown or revoked device, no auth message in
# time): an add-in that authenticates is never counted, however often it reconnects -
# also when it is then answered 4002 (a stale socket still holds its device) or 4001.
# A revoked add-in stops reconnecting after its 4003, so 20 failures a minute from one
# address is a client guessing, not an office of add-ins.
HANDSHAKE_FAILURE_LIMIT = 20
# Sockets accepted but not yet authenticated, at once, per address. An add-in sends its
# auth message right after connecting, so it is pending for milliseconds.
PENDING_HANDSHAKE_LIMIT = 64
_pending_handshakes: dict[str, int] = {}
CONFIRM_CHANNEL = "host_ui"       # confirmations come from the page, never from the model
MAX_EVIDENCE = 200

# The scope every confirmation and every ledger line belongs to: the device the
# request drives, or "local" for the add-in on this machine (package default).
LOCAL_SCOPE = "local"
CLOUD_MODE_ONLY = "cloud_mode_only"


async def set_device_context(connection: HTTPConnection) -> None:
    """Which device this request drives. On a cloud host: ``backend.api.devices``
    reads ``X-Device-Id`` and verifies ``X-Device-Key``. A local host has no devices:
    it reads no device header at all and every request drives the local add-in."""
    if not get_settings().cloud:
        devices_api.request_device.set(None)
        return
    await devices_api.set_device_context(connection)


def request_device() -> str | None:
    """The device this request drives; always None on a local host."""
    return devices_api.current_device() if get_settings().cloud else None


def current_scope() -> str:
    """The scope of this request: its device id, or ``"local"`` without one."""
    return request_device() or LOCAL_SCOPE


router = APIRouter(
    prefix="/api/v1/bridge",
    tags=["bridge"],
    dependencies=[Depends(set_device_context)],
)

# Cloud only: the add-in socket and the device routes (backend/api/devices.py),
# with the same prefix, tag, error contract and device dependency as ``router``.
relay_router = APIRouter(
    prefix="/api/v1/bridge",
    tags=["bridge"],
    dependencies=[Depends(set_device_context)],
)

# Local only, in place of relay_router: the same paths answer 404 cloud_mode_only.
cloud_only_router = APIRouter(prefix="/api/v1/bridge", include_in_schema=False)


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
    """The selected device's relay client, or the local TCP client (503 when neither answers).
    A local host only ever uses the local TCP client."""
    device_id = request_device()
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


# -- tool names ----------------------------------------------------------------------

# A tool name becomes a file name: the store reads and writes <directory>/<name>.yaml.
# The package's schema asks a pack name only to be a non-empty string; the host
# accepts the schema's identifier pattern (IDENTIFIER_RE, the shape of a parameter
# name and a {placeholder}: a letter or _, then letters, digits or _ - no dot, no
# path separator), at most TOOL_NAME_MAX characters, and never a Windows device
# name: on Windows con.yaml is the console in every directory, and
# ToolStore().load("con") blocks reading it. Checked before any store call, in the
# routes and in the model's tools.
TOOL_NAME_MAX = 100
INVALID_TOOL_NAME = "invalid_tool_name"
# CON, PRN, AUX, NUL, COM0-9 and LPT0-9 (also with the superscript digits 1-3 Windows
# reads as digits), with or without an extension, in any case.
_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{port}{digit}" for port in ("com", "lpt") for digit in "0123456789\u00b9\u00b2\u00b3"})


def tool_name_problem(name: object) -> str | None:
    """Why ``name`` cannot be a tool name, or None when it can."""
    if not isinstance(name, str) or not name:
        return "a tool name is required"
    if len(name) > TOOL_NAME_MAX:
        return f"a tool name has at most {TOOL_NAME_MAX} characters"
    if not IDENTIFIER_RE.fullmatch(name):
        return "a tool name starts with a letter or _ and has only letters, digits and _"
    if name.split(".", 1)[0].lower() in _DEVICE_NAMES:
        return f"'{name}' is a Windows device name"
    return None


def invalid_tool_name(name: object, problem: str) -> ApiError:
    return ApiError(422, INVALID_TOOL_NAME, f"Not a tool name: {problem}", tool=str(name)[:TOOL_NAME_MAX])


def require_tool_name(name: object) -> str:
    """``name`` when it is a tool name; 422 ``invalid_tool_name`` before any file is touched."""
    problem = tool_name_problem(name)
    if problem is not None:
        raise invalid_tool_name(name, problem)
    return name  # type: ignore[return-value]


# -- built-in packs are read-only ------------------------------------------------------

BUILTIN_READ_ONLY = "builtin_read_only"
PACK_EXISTS = "pack_exists"


def _user_file(store: ToolStore, name: str) -> Path:
    """Where the store keeps the user pack ``name`` (a checked name is its own file stem)."""
    return store.user_dir / f"{name}.yaml"


def ships_builtin(store: ToolStore, name: str) -> bool:
    """A pack of this name ships in the wheel (shown or hidden)."""
    return (store.builtin_dir / f"{name}.yaml").is_file()


def is_builtin(store: ToolStore, name: str) -> bool:
    """``name`` is a built-in pack with no user file of that name: changing it would
    copy or hide the wheel's pack. A user file of a built-in name (one an older host
    made by editing the built-in) is the designer's own."""
    return ships_builtin(store, name) and not _user_file(store, name).exists()


def builtin_read_only(name: str) -> ApiError:
    return ApiError(409, BUILTIN_READ_ONLY,
                    f"'{name}' is a built-in tool and built-in tools are read-only: "
                    f"export it and import it under another name to make your own", tool=name)


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def pack_origin(store: ToolStore, name: str) -> tuple[str, Path | None]:
    """``("builtin" | "user", file)`` of a listed pack: the file ``load`` reads. A listed
    name that is not a tool name is never looked up (it can only be a user file: the
    wheel's packs are named like their files)."""
    if tool_name_problem(name) is not None:
        return "user", None
    path = store.path_of(name)
    if path is not None and _same_dir(path.parent, store.builtin_dir):
        return "builtin", path
    return "user", path


async def require_admin(x_admin_token: str | None = Header(None)) -> None:
    """Changing a capability pack takes the admin password: its code later runs in a
    designer's Revit once they confirm its card. The same check as every admin route
    (``WebSettings.admin_token_ok``), in this router's error shape, as ``GET /devices``:
    503 ``admin_disabled`` without ``ADMIN_PASSWORD``, 403 ``admin_required`` for a
    missing or wrong token. A device key does not stand in for it.

    A local host passes without a password: only this machine reaches it."""
    settings = get_settings()
    if not settings.cloud:
        return
    if not settings.admin_password:
        raise ApiError(503, "admin_disabled", "Set ADMIN_PASSWORD to change capability packs")
    if not settings.admin_token_ok(x_admin_token):
        raise ApiError(403, "admin_required", "Wrong or missing X-Admin-Token")


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
    """The pack a ``run_tool`` spec names (422 ``invalid_tool_name`` before the store is asked)."""
    if spec.action.kind == "run_tool" and spec.action.tool:
        return store.load(require_tool_name(spec.action.tool))
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


class CheckCodeRequest(BaseModel):
    """``checking.check_code(client, code, parameters)``: the snippet and its ``parameters``
    (the snippet's object[]), as the MCP tool takes them."""
    code: str
    parameters: list | None = None


class DryRunRequest(BaseModel):
    """``dry_run.dry_run_code(client, code, parameters)``. The package's ``include`` (paths
    of .cs files on the machine running the host, read and sent to Revit) is not offered:
    a browser has no business naming files on the server."""
    code: str
    parameters: list | None = None


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


def tool_listing(tool, store: ToolStore, *, directory: bool = False) -> dict:
    """One item of ``GET /tools`` and of the model's ``list_tools``: the MCP ``list_tools``
    shape plus ``builtin`` and ``origin`` (``"builtin"``: the wheel's read-only pack;
    ``"user"``: a file in the user capabilities directory). ``directory`` adds the
    absolute directory the pack was read from (a local host's page only)."""
    origin, path = pack_origin(store, tool.name)
    item = {
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
        "builtin": origin == "builtin",
        "origin": origin,
    }
    if directory:
        item["directory"] = str(path.parent if path is not None else store.user_dir)
    return item


def list_packs(*, directory: bool = False) -> list[dict]:
    """Every pack, read from disk now (no cache: a pack another process just wrote is in it)."""
    store = ToolStore()
    return [tool_listing(t, store, directory=directory) for t in store.list_tools()]


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
    """``ToolStore.list_tools`` in the MCP ``list_tools`` shape, plus ``builtin`` and
    ``origin`` (and, on a local host, ``directory``: where the file was read). Read from
    disk on every call."""
    return list_packs(directory=not get_settings().cloud)


@router.get("/tools/{name}", responses=responses(403, 404, 422))
async def get_tool(name: str):
    tool = ToolStore().load(require_tool_name(name))
    if not tool:
        raise unknown_tool(name)
    return _tool_detail(tool)


@router.put("/tools/{name}", dependencies=[Depends(require_admin)],
            responses=admin_responses(400, 403, 404, 409, 422, 503))
async def update_tool(name: str, req: UpdateToolRequest):
    """Change editable fields of a user pack (admin password required on a cloud host); the
    package validates the result as a v1 pack (422 with problems). A built-in pack is
    read-only: 409 ``builtin_read_only``."""
    require_tool_name(name)
    store = ToolStore()
    if is_builtin(store, name):
        raise builtin_read_only(name)
    updates = req.model_dump(exclude_unset=True)
    if "code_template" in updates:
        safe, warnings = sandbox.review(updates["code_template"] or "")
        if not safe:
            raise ApiError(400, "blocked", "Code review failed", warnings=warnings)
    try:
        tool = store.update(name, updates)
    except ValueError as exc:
        raise _invalid_pack(exc) from None
    if not tool:
        raise unknown_tool(name)
    return {"status": "updated", **_tool_detail(tool), "revit_synced": await _sync_to_revit(tool)}


@router.delete("/tools/{name}", dependencies=[Depends(require_admin)],
               responses=admin_responses(403, 404, 409, 422, 503))
async def delete_tool(name: str):
    """Delete a user pack (admin password required on a cloud host). A built-in pack is
    read-only: 409 ``builtin_read_only``, never hidden. Deleting a user file that carries a
    built-in's name brings the built-in back."""
    require_tool_name(name)
    store = ToolStore()
    if is_builtin(store, name):
        raise builtin_read_only(name)
    if not store.delete(name):
        raise unknown_tool(name)
    if ships_builtin(store, name):
        store.enable(name)            # delete() hid the wheel's pack behind a marker: show it again
    return {"status": "deleted", "name": name}


@router.get("/tools/{name}/export", response_class=Response,
            responses={**responses(403, 404, 422),
                       200: {"description": "The pack file as stored (YAML text)",
                             "content": {"text/yaml": {"schema": {"type": "string"}}}}})
async def export_tool(name: str):
    """The pack's file as it is on disk - the user file, or the wheel's for a built-in pack -
    as ``text/yaml``. ``POST /tools/import`` takes it back (under another name for a built-in)."""
    require_tool_name(name)
    store = ToolStore()
    path = store.path_of(name) if store.load(name) is not None else None
    if path is None:
        raise unknown_tool(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:                    # deleted between the two reads
        raise unknown_tool(name) from None
    return Response(text, media_type="text/yaml; charset=utf-8")


MAX_IMPORT_BYTES = 256 * 1024          # a pack file; the largest built-in is a few KB


@router.post("/tools/import", dependencies=[Depends(require_admin)],
             responses=admin_responses(400, 403, 409, 422, 503),
             openapi_extra={"requestBody": {"required": True, "description": "A capability pack file (YAML text)",
                                            "content": {"text/yaml": {"schema": {"type": "string"}}}}})
async def import_tool(request: Request, overwrite: bool = False):
    """Store a pack file sent as YAML text in the user capabilities directory, as it is.

    Validated as the package validates a pack file (``validate_pack``: the v1 layout, 422
    ``invalid_pack`` with the problems), its name checked (422 ``invalid_tool_name``), its
    code reviewed by the sandbox (400 ``blocked``). A built-in pack's name is refused (409
    ``builtin_read_only``), and so is the name of a user pack that exists unless
    ``overwrite=true`` (409 ``pack_exists``). Admin password required on a cloud host."""
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise ApiError(422, "invalid_args", f"the pack file has {len(raw)} bytes, at most {MAX_IMPORT_BYTES}")
    try:
        text = raw.decode("utf-8-sig")
        data = yaml.safe_load(text)
    except UnicodeDecodeError:
        raise ApiError(422, "invalid_pack", problems=["the file is not UTF-8 text"]) from None
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ApiError(422, "invalid_pack", problems=[f"not YAML: {first}"]) from None
    problems = validate_pack(data)
    if problems:
        raise ApiError(422, "invalid_pack", problems=problems)
    name = require_tool_name(data["name"])
    safe, warnings = sandbox.review(data["code_template"])
    if not safe:
        raise ApiError(400, "blocked", "Code review failed", warnings=warnings)
    store = ToolStore()
    if ships_builtin(store, name):
        raise builtin_read_only(name)
    target = _user_file(store, name)
    replaced = target.exists()
    if replaced and not overwrite:
        raise ApiError(409, PACK_EXISTS, f"A tool named '{name}' exists: import with overwrite=true to replace it",
                       tool=name)
    await run_in_threadpool(_write_pack_file, store.user_dir, target, text)
    tool = store.load(name)
    if tool is None:                   # validated above; only a file changed meanwhile gets here
        raise unknown_tool(name)
    return {"status": "imported", "name": tool.name, "version": tool.version, "replaced": replaced,
            "revit_synced": await _sync_to_revit(tool)}


def _write_pack_file(directory: Path, target: Path, text: str) -> None:
    """Write the file whole or not at all: another process (the MCP server) may read it now."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, prefix=f".{target.stem}.", suffix=".import")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


@router.get("/tools/{name}/choices", responses=responses(403, 404, 422, 503, 504))
async def get_tool_choices(name: str):
    """Real values for the pack's dynamic parameters (levels, types, elements)."""
    require_tool_name(name)
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
    pack = ToolStore().load(require_tool_name(name))
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
    require_tool_name(name)
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
    pack = pack_for(draft, ToolStore())          # a bad tool name is refused before Revit is asked
    snapshot = parse_snapshot(req.snapshot)
    if snapshot is None:
        snapshot = await snapshot_now(await get_revit_client())
    return reconcile(draft, snapshot, pack).model_dump(mode="json")


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


# -- the workbench: compile, dry run (no token) ----------------------------------------

def guidance_library() -> _guidance.Library:
    """The guidance layers as the MCP server loads them for ``check_code``: the framework
    lessons in the wheel, company and project from REVIT_BRIDGE_GUIDANCE_COMPANY /
    _PROJECT, personal under the package's data root. Validated on first use and kept per
    set of directories (a lesson added later is read after a restart, as with the MCP
    server). Raises ``GuidanceError`` when the framework layer is invalid - ``check_code``
    turns that into a ``guidance_unavailable`` warning."""
    layers = guidance_layer_dirs()
    return _load_guidance(guidance_dir(), layers["company"], layers["project"], layers["personal"])


@lru_cache(maxsize=8)
def _load_guidance(framework: Path, company: Path | None, project: Path | None,
                   personal: Path | None) -> _guidance.Library:
    return _guidance.load_layers(framework, company=company, project=project, personal=personal)


@router.post("/check", responses=responses(403, 422, 503))
async def check_code(req: CheckCodeRequest):
    """``checking.check_code``: compile the snippet in Revit without running it - no token,
    nothing changes. The package's reply as it is (``{schema_version, checked, compiled,
    blocked, capability, reason, diagnostics, truncated, fingerprint, hints, guidance,
    environment, warnings}``), with the guidance lessons the MCP tool adds. As the MCP tool
    does, code the sandbox refuses is answered before Revit is needed (``blocked: true``);
    no Revit to ask is 503 ``revit_unreachable``, as on every route."""
    refused = checking.blocked_reply(req.code)
    if refused is not None:
        return refused
    client = await get_revit_client()
    return await checking.check_code(client, req.code, req.parameters, library=guidance_library)


@router.post("/dry-run", responses=responses(403, 422, 503))
async def dry_run_code(req: DryRunRequest):
    """``dry_run.dry_run_code``: run the snippet in the open model inside a transaction group
    the add-in always rolls back - nothing is kept, the undo stack is untouched, so no
    confirmation and no token (the package's rule: the gate takes no part in a dry run).
    The package's reply as it is (``{success, rolled_back, result, error, diagnostics,
    changes, failures, fingerprint, evidence_id, hint, warnings}``); each request that
    reaches the add-in leaves a ``dry_run`` line in the evidence ledger of this device's
    scope. Code the sandbox refuses is answered before Revit is needed (``error:
    "blocked"``); no Revit to ask is 503 ``revit_unreachable``."""
    refused, _ = package_dry_run.prepare(req.code)
    if refused is not None:
        return refused
    client = await get_revit_client()
    return await package_dry_run.dry_run_code(client, req.code, req.parameters, ledger=get_ledger(),
                                              host=HOST, scope=current_scope())


# -- execution -----------------------------------------------------------------

@router.post("/execute", responses=responses(400, 403, 422, 503))
async def execute_code(req: ExecuteRequest):
    """``run_code``: the confirmed C# under its token; the sandbox review and the ledger are the package's."""
    token = _require_token(req.token)
    client = await get_revit_client()
    result = await run_code(gate=get_gate(), ledger=get_ledger(), client=client, code=req.code,
                            parameters=req.parameters, token=token, host=HOST, scope=current_scope())
    return _execution_payload(result)


@router.post("/solidify", dependencies=[Depends(require_admin)],
             responses=admin_responses(400, 403, 409, 422, 503))
async def solidify_tool(req: SolidifyRequest):
    """``ToolStore.solidify``: save working code as a v1 pack in the user capabilities
    directory (admin password required on a cloud host; a user pack of the same name is
    replaced); best-effort sync to the add-in. A built-in pack's name is refused: 409
    ``builtin_read_only``."""
    require_tool_name(req.name)
    store = ToolStore()
    if ships_builtin(store, req.name):
        raise builtin_read_only(req.name)
    safe, warnings = sandbox.review(req.code)
    if not safe:
        raise ApiError(400, "blocked", "Code review failed", warnings=warnings)
    try:
        tool = store.solidify(
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
    device_id = request_device()
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
    # A local host has no paired devices to mention.
    paired = f"; {relay_status['connected']} paired device(s) connected" if get_settings().cloud else ""
    return {
        "revit_connected": False, "latency_ms": None,
        "mode": "waiting_for_revit",
        "detail": f"Local add-in unreachable ({status['error']}){paired}",
        "endpoint": f"{status['host']}:{status['port']}",
        "bridge_version": bridge_version, "timestamp": now, "devices": relay_status,
    }


@router.get("/service-health")
async def service_health():
    """Relay status only; never probes Revit."""
    relay_status = get_relay().get_status()
    cloud = get_settings().cloud           # a local host has no relay: not ready, no endpoint, 0 devices
    return {
        "status": "ok",
        "remote_relay_ready": cloud,
        "connected_devices": relay_status["connected"],
        "websocket_endpoint": "/api/v1/bridge/ws/{device_id}" if cloud else None,
        "devices": relay_status,
    }


@router.get("/slots")
async def get_slots():
    """How many devices may be connected and how many are: no ids, this is public."""
    return get_relay().get_status()


# -- add-in relay (cloud only) -------------------------------------------------

@relay_router.websocket("/ws/{device_id}")
async def revit_ws_endpoint(ws: WebSocket, device_id: str):
    """A paired add-in connects here and authenticates with its device token.

    The first message must be ``{"type": "auth", "device_id": ..., "token": ...}``
    within ``HANDSHAKE_TIMEOUT`` seconds and must verify against the device
    store, else the socket closes with 4003 - an unknown device, a revoked one
    and a wrong token are indistinguishable from outside. 4002 means that
    device is already connected, 4001 that the relay is full.

    An address with ``HANDSHAKE_FAILURE_LIMIT`` failed handshakes in the last minute,
    or with ``PENDING_HANDSHAKE_LIMIT`` sockets still unauthenticated, is refused
    before the upgrade (a real server answers HTTP 403): no close code at all, so
    4001/4002/4003 keep their meaning and the add-in retries as after any failed
    connection. Add-ins that authenticate never count against either. The token is
    only read after the upgrade, so a valid add-in that shares its address with a
    client that keeps failing is refused with it until those failures are a minute old.
    """
    relay = get_relay()
    address = client_key(ws)
    if (handshake_failures.count(address) >= HANDSHAKE_FAILURE_LIMIT
            or _pending_handshakes.get(address, 0) >= PENDING_HANDSHAKE_LIMIT):
        _log.warning("add-in handshakes from %s refused before accepting: too many failed or pending", address)
        await ws.close(code=1008, reason="Too many failed connection attempts from this address")
        return
    _pending_handshakes[address] = _pending_handshakes.get(address, 0) + 1
    try:
        await ws.accept()
        try:
            first = await asyncio.wait_for(ws.receive_text(), timeout=HANDSHAKE_TIMEOUT)
        except Exception:  # noqa: BLE001 - no handshake in time
            first = None
        authenticated = await _handshake_ok(first, device_id)
    finally:
        _pending_handshakes[address] -= 1
        if not _pending_handshakes[address]:
            del _pending_handshakes[address]
    if not authenticated:
        handshake_failures.record(address)
        await ws.close(code=4003, reason="Invalid device token")
        return

    refused = relay.register(device_id, ws)
    if refused == "occupied":
        await ws.close(code=4002, reason="This device is already connected")
        return
    if refused is not None:
        await ws.close(code=4001, reason=f"Too many connected devices (max {relay.max_devices})")
        return

    _log.info("Revit add-in connected for device %s", device_id)
    try:
        # Registered: from here on every way out unregisters (finally). ``touch`` only
        # records last_seen, so a device store that cannot be read right now (a
        # revit-bridge that raises OSError rather than overwrite devices.json) costs
        # the add-in nothing: it stays connected and the next connection records it.
        try:
            await run_in_threadpool(devices_api.get_device_store().touch, device_id)
        except OSError as exc:
            _log.warning("device %s connected, last_seen not recorded: device store unreadable (%s)",
                         device_id, exc)
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


# -- devices (cloud only) ------------------------------------------------------

# Pairing, the device list and revocation (backend/api/devices.py) hang on the relay
# router, which shares this router's error contract - but the redeem route needs no
# device header: the add-in has only its pairing code.
devices_api.register(relay_router)


# -- a local host: the relay's paths, answered --------------------------------------

def cloud_mode_only() -> ApiError:
    return ApiError(404, CLOUD_MODE_ONLY,
                    "Pairing, devices and the add-in relay exist only on a host started with "
                    "DEPLOY_MODE=cloud; this host is local and talks to the add-in on this machine")


_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@cloud_only_router.api_route("/devices", methods=_ALL_METHODS)
@cloud_only_router.api_route("/devices/{rest:path}", methods=_ALL_METHODS)
@cloud_only_router.api_route("/ws/{rest:path}", methods=_ALL_METHODS)
async def relay_route_on_a_local_host(rest: str = ""):
    raise cloud_mode_only()


@cloud_only_router.websocket("/ws/{device_id}")
async def relay_socket_on_a_local_host(ws: WebSocket, device_id: str):
    """An add-in that tries to connect gets the same 404 as a plain request, before the upgrade."""
    error = cloud_mode_only()
    try:
        await ws.send_denial_response(JSONResponse(status_code=error.status, content=error.payload))
    except RuntimeError:                       # a server without the denial extension: HTTP 403
        await ws.close(code=1008, reason=CLOUD_MODE_ONLY)
