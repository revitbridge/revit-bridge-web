"""The tools the model may call from the chat: the package's read-only tools plus
``propose_spec``, the host's one addition.

Same names, arguments and result JSON as the MCP server's tools, so the skill
text applies unchanged; the implementations call the package directly through
the helpers the v1 routes use (no HTTP round trip to ourselves). The model has
no ``confirm_spec``, ``run_tool`` or ``execute_code``: confirmation happens in
the page (``POST /spec/confirm``) and execution with the token the page holds;
the result comes back to the model as a message, never as something it ran.

``propose_spec`` is what makes the page's spec card appear: the server runs
``validate_spec`` and ``reconcile`` on the draft, the page gets an SSE
``spec`` event with ``{spec, card, errors, reconcile}``, the model gets
``{accepted, errors, reconcile}`` - accepted only when there are no errors
and the reconciliation is ``ready``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from revit_bridge.capabilities import ToolStore
from revit_bridge.snapshot import RevitQueryExecutor
from revit_bridge.snapshot.project import take_snapshot, validate_categories
from revit_bridge.snapshot.query import run_query
from revit_bridge.spec.models import TaskSpec
from revit_bridge.spec.rules import missing_params, reconcile, validate_spec

from backend.api.bridge import (
    get_revit_client, pack_for, parse_snapshot, snapshot_now, tool_listing,
)
from backend.api.errors import ApiError
from backend.session import Session

PROPOSE_SPEC = "propose_spec"

# OpenAI-compatible function definitions; the descriptions are the MCP tools' own.
TOOL_DEFINITIONS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_project_snapshot",
        "description": "Read-only picture of the open Revit model: document, units, active view, "
                       "levels, grids, selection, links, phases and the family types of the given "
                       "categories (default: walls, structural columns/framing, floors, doors, "
                       "windows). Partial failures are listed in warnings; fingerprint identifies "
                       "the model state for reconcile.",
        "parameters": {"type": "object", "properties": {
            "categories": {"type": "array", "items": {"type": "string"},
                           "description": "OST_* category names whose family types to list"},
        }},
    }},
    {"type": "function", "function": {
        "name": "query",
        "description": "Read-only model query, no confirmation needed. kinds: levels, grids, "
                       "family_types (args.categories), elements (args.category, args.limit<=200), "
                       "selection, view_elements (args.limit<=200), units, counts (args.categories).",
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string"},
            "args": {"type": "object", "description": "arguments of the kind, see the description"},
        }, "required": ["kind"]},
    }},
    {"type": "function", "function": {
        "name": "list_tools",
        "description": "The capability packs of this host, as a list of {name, description, version, "
                       "parameters: [{name, type, source, required, unit?}], preconditions, "
                       "validator, used}. Check here before drafting a spec.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_tool_choices",
        "description": "Query Revit for the dynamic parameter choices of a capability pack: "
                       "{param_name: [{label, value}, ...]} for parameters that need selection "
                       "(levels, family types, elements).",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "missing_params",
        "description": "The questions still open for a capability pack: one per required parameter "
                       "not in known ({name: value}), with the real options (levels, family types) "
                       "from the snapshot (pass the one from get_project_snapshot, or omit it and "
                       "the host takes one). Returns a list of {id, param, text, why, options, "
                       "allow_other}.",
        "parameters": {"type": "object", "properties": {
            "tool": {"type": "string"},
            "known": {"type": "object"},
            "snapshot": {"type": "object"},
            "language": {"type": "string", "description": "zh (default) or en"},
        }, "required": ["tool"]},
    }},
    {"type": "function", "function": {
        "name": "reconcile",
        "description": "Check a draft TaskSpec against the model: values that do not exist (levels, "
                       "family types), missing parameters as questions, readings the designer must "
                       "confirm (units, range words), a stale snapshot. Pass the snapshot from "
                       "get_project_snapshot, or omit it and the host takes one. Returns "
                       "{conflicts, questions, interpretations_required, ready}.",
        "parameters": {"type": "object", "properties": {
            "spec": {"type": "object"},
            "snapshot": {"type": "object"},
        }, "required": ["spec"]},
    }},
    {"type": "function", "function": {
        "name": PROPOSE_SPEC,
        "description": "Propose the TaskSpec to the designer: the host validates and reconciles it, "
                       "shows the spec card in its interface and answers {accepted, errors, "
                       "reconcile}. accepted is true only with no errors and reconcile.ready; "
                       "otherwise fix the spec or ask the designer, then propose again. "
                       "Confirmation and execution happen in the host's interface, not here.",
        "parameters": {"type": "object", "properties": {"spec": {"type": "object"}}, "required": ["spec"]},
    }},
]

TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOL_DEFINITIONS)


@dataclass
class ToolOutcome:
    """What one tool call produced: the model's result and, for ``propose_spec``,
    the payload of the ``spec`` event for the page."""
    result: object
    spec_event: dict | None = None


def parse_arguments(raw: str) -> dict | None:
    """The call's arguments as a dict; None when the text is not a JSON object."""
    if not raw or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def call_tool(name: str, arguments: dict, session: Session) -> ToolOutcome:
    """Run one tool for the model; every failure is a result, never an exception."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return ToolOutcome({"error": "unknown_tool", "tool": name, "known": sorted(TOOL_NAMES)})
    try:
        return await handler(arguments, session)
    except ApiError as exc:                    # the route helpers' errors, as the MCP tools report them
        return ToolOutcome(exc.payload)


# -- the package's read-only tools ---------------------------------------------------

async def _get_project_snapshot(args: dict, session: Session) -> ToolOutcome:
    try:
        cats = validate_categories(args.get("categories"))
    except ValueError as exc:
        return ToolOutcome({"error": "invalid_category", "message": str(exc)})
    snapshot = await snapshot_now(await get_revit_client(), cats)
    session.snapshot_fingerprint = snapshot.fingerprint
    return ToolOutcome(snapshot.model_dump(mode="json"))


async def _query(args: dict, session: Session) -> ToolOutcome:
    kind = str(args.get("kind", ""))
    query_args = args.get("args") or {}
    try:
        client = await get_revit_client()
        return ToolOutcome(await run_query(RevitQueryExecutor(client), kind, query_args))
    except ApiError as exc:
        return ToolOutcome({**exc.payload, "kind": kind})
    except OSError as exc:
        return ToolOutcome({"error": "revit_unreachable", "kind": kind, "message": str(exc) or type(exc).__name__})
    except Exception as exc:  # noqa: BLE001 - a bug in the query is reported, not raised
        return ToolOutcome({"error": "query_failed", "kind": kind, "message": f"{type(exc).__name__}: {exc}"})


async def _list_tools(args: dict, session: Session) -> ToolOutcome:
    return ToolOutcome([tool_listing(t) for t in ToolStore().list_tools()])


async def _get_tool_choices(args: dict, session: Session) -> ToolOutcome:
    name = str(args.get("name", ""))
    store = ToolStore()
    if store.load(name) is None:
        return ToolOutcome({"error": "unknown_tool", "tool": name})
    dynamic = store.get_dynamic_params(name)
    if not dynamic:
        return ToolOutcome({"message": f"Tool '{name}' has no dynamic parameters"})
    try:
        client = await get_revit_client()
        return ToolOutcome(await RevitQueryExecutor(client).get_tool_choices(dynamic))
    except ApiError as exc:
        return ToolOutcome({"success": False, **exc.payload})
    except Exception as exc:  # noqa: BLE001 - same shape as the MCP tool
        return ToolOutcome({"success": False, "error": str(exc) or type(exc).__name__})


async def _missing_params(args: dict, session: Session) -> ToolOutcome:
    name = str(args.get("tool", ""))
    pack = ToolStore().load(name)
    if pack is None:
        return ToolOutcome({"error": "unknown_tool", "tool": name})
    known = args.get("known") or {}
    if not isinstance(known, dict):
        return ToolOutcome({"error": "invalid_args", "message": "known must be an object {name: value}"})
    given = args.get("snapshot")
    if given is not None:
        snapshot = parse_snapshot(given)
    else:
        try:
            snapshot = await take_snapshot(await get_revit_client())
        except Exception:  # noqa: BLE001 - best effort: questions still go out, without options
            snapshot = None
    language = str(args.get("language") or "zh")
    return ToolOutcome([q.model_dump(mode="json") for q in missing_params(pack, known, snapshot, language)])


async def _reconcile(args: dict, session: Session) -> ToolOutcome:
    try:
        draft = TaskSpec.model_validate(args.get("spec"))
    except ValidationError as exc:
        return ToolOutcome({"error": "invalid_spec", "message": str(exc)})
    given = args.get("snapshot")
    snapshot = parse_snapshot(given) if given is not None else await snapshot_now(await get_revit_client())
    return ToolOutcome(reconcile(draft, snapshot, pack_for(draft, ToolStore())).model_dump(mode="json"))


# -- the host's tool -----------------------------------------------------------------

async def _propose_spec(args: dict, session: Session) -> ToolOutcome:
    """validate_spec + reconcile; the page sees the card, the model the verdict."""
    given = args.get("spec")
    session.spec = given if isinstance(given, dict) else None
    try:
        spec = TaskSpec.model_validate(given)
    except ValidationError as exc:
        # Nothing the page could render: the verdict goes to the model only.
        errors = [{"code": "invalid_spec", "param": None, "message": str(exc)}]
        return ToolOutcome({"accepted": False, "errors": errors, "reconcile": None})
    store = ToolStore()
    errors = [e.model_dump() for e in validate_spec(spec, pack_for(spec, store))]
    event = {"spec": spec.model_dump(mode="json"), "card": spec.card(), "errors": errors, "reconcile": None}
    try:
        report = reconcile(spec, await snapshot_now(await get_revit_client()), pack_for(spec, store)).model_dump(mode="json")
    except ApiError as exc:
        # No Revit to reconcile against: not accepted; the page sees the card and why
        # (reconcile stays null, the package's payload rides in reconcile_error).
        event["reconcile_error"] = exc.payload
        return ToolOutcome({"accepted": False, "errors": errors, "reconcile": exc.payload}, spec_event=event)
    event["reconcile"] = report
    return ToolOutcome({"accepted": not errors and bool(report["ready"]), "errors": errors, "reconcile": report},
                       spec_event=event)


_HANDLERS = {
    "get_project_snapshot": _get_project_snapshot,
    "query": _query,
    "list_tools": _list_tools,
    "get_tool_choices": _get_tool_choices,
    "missing_params": _missing_params,
    "reconcile": _reconcile,
    PROPOSE_SPEC: _propose_spec,
}
