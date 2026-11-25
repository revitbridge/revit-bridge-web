"""The tools the model may call from the chat: the package's read-only tools plus
``propose_spec`` and ``ask_designer``, the host's card and form tools.

Same names, arguments and result JSON as the MCP server's tools; the
implementations call the package directly through the helpers the v1 routes use
(no HTTP round trip to ourselves). The model has no ``confirm_spec``,
``run_tool`` or ``execute_code``: confirmation happens in the page
(``POST /spec/confirm``) and execution with the token the page holds; the result
comes back to the model as a message, never as something it ran.

The model in this host never writes code: it has no ``get_guidance`` (the
lessons for writing C#), and ``propose_spec`` refuses an ``execute_code`` spec
without showing a card (``code_not_offered``) - only saved tools run here. Code
is written in the coding frameworks and tried and saved as a tool by a person in
the workbench. ``list_tools`` is read from disk on every call, so a tool saved a
moment ago (here or by the package's MCP server) is offered at once; each item
says whether it is ``builtin`` and its ``origin``. Every tool name the model
gives is checked (``require_tool_name``) before the store is asked.

``propose_spec`` is what makes the page's spec card appear: the server runs
``validate_spec`` and ``reconcile`` on the draft, the page gets an SSE
``spec`` event with ``{spec, card, errors, reconcile}``, the model gets
``{accepted, errors, reconcile}`` - accepted only when there are no errors
and the reconciliation is ``ready``. Besides the package's errors, a parameter
bound to a pick (evidence ``mention:<kind>#<id>``) that the package did not
verify in this session for the device the chat drives, or not bound as that pick
is, is an ``unknown_mention`` error (``backend.mention_check``); the model's
``reconcile`` answers such a draft as ``POST /spec/reconcile`` does
(``{error: "invalid_spec", message, errors}``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from pydantic import ValidationError

from revit_bridge import list_host_functions, view_commands
from revit_bridge.capabilities import ToolStore
from revit_bridge.snapshot import RevitQueryExecutor
from revit_bridge.snapshot.project import take_snapshot, validate_categories
from revit_bridge.snapshot.query import run_query
from revit_bridge.spec.models import TaskSpec
from revit_bridge.spec.rules import missing_params, reconcile, validate_spec

from backend import captures
from backend.api import bridge
from backend.api.bridge import (
    current_scope, get_revit_client, list_packs, pack_for, parse_snapshot, require_tool_name, snapshot_now,
)
from backend.api.direct_reads import reserve_direct_run, selection_for_card, try_read
from backend.api.errors import ApiError
from backend.mention_check import mention_errors
from backend.session import Session
from backend.questions import DesignerForm, SelectionQuestion
from backend.plans import Plan
from backend.workflow_run import WorkflowRun, catalogue


PROPOSE_SPEC = "propose_spec"

# The workbench page, where a person tries code and saves it as a tool. The host's
# line in the system prompt (backend.api.chat.WORKBENCH_LINE) and the code_not_offered
# verdict both name it.
WORKBENCH_PATH = "/capabilities/authoring"
WORKBENCH_LINK = f"[workbench]({WORKBENCH_PATH})"
CODE_NOT_OFFERED = "code_not_offered"
CODE_NOT_OFFERED_MESSAGE = (
    "This host runs only saved tools: it never runs code the model writes, so an execute_code "
    "spec is not shown to the designer. Propose a run_tool spec with a tool from list_tools. If "
    "no saved tool fits, write no code: tell the designer that no tool here does this, describe "
    f"in one sentence the tool they would need, and that a new one can be made in the {WORKBENCH_LINK}."
)

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
                       "selection, view_elements (args.limit<=200), units, counts (args.categories), "
                       "links (every linked model instance: id, name, loaded, document_title, "
                       "origin_mm, rotated), type_categories (the categories that have types, with "
                       "counts). args.link_id - the id of a loaded link from kind links - makes "
                       "levels, family_types, elements, counts and type_categories read that linked "
                       "model: the reply is in the link's own coordinates and carries the link's "
                       "origin and rotation; the element, level and type ids in it belong to the "
                       "linked file and must never be passed to a tool that acts on the open model.",
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
                       "Ask the designer to click a write card. A declared built-in reader returns its result "
                       "here. Omit step_id for a supporting read and continue with its result.",
        "parameters": {"type": "object", "properties": {"spec": {"type": "object"},
                       "step_id": {"type": "string", "description": "Identify the first unfinished step. A reader completes it only when the step explicitly names that reader. Omit for supporting reads."}},
                       "required": ["spec"]},
    }},
    {"type": "function", "function": {
        "name": "ask_designer",
        "description": "End this turn with questions in this reply. Use choices grounded in facts "
                       "already read. Ask all outstanding questions together.",
        "parameters": {"type": "object", "required": ["questions"],
                       "properties": {"questions": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                           "type": "object", "required": ["id", "text", "options"],
                           "properties": {
                               "id": {"type": "string", "minLength": 1, "maxLength": 100},
                               "text": {"type": "string", "minLength": 1, "maxLength": 500},
                               "allow_other": {"type": "boolean"},
                               "multiple": {"type": "boolean"},
                               "options": {"type": "array", "minItems": 2, "maxItems": 6, "items": {
                                   "type": "object", "required": ["value", "label"],
                                   "properties": {
                                       "value": {"type": "string", "minLength": 1, "maxLength": 500},
                                       "label": {"type": "string", "minLength": 1, "maxLength": 500},
                                   },
                               }},
                           },
                       }}}},
    }},
    {"type": "function", "function": {
        "name": "set_plan",
        "description": "For a request requiring two or more executions, set the plan before the first card. "
                       "Continue this turn with the first unfinished step. Form answers and earlier step results "
                       "remain valid throughout the plan. Use created element ids in later specs. Revise unfinished "
                       "steps as requested, omitting unwanted unfinished steps to skip them. Preserve completed steps.",
        "parameters": {"type": "object", "required": ["steps"], "properties": {"steps": {
            "type": "array", "maxItems": 8, "items": {"type": "object", "required": ["id", "title"],
            "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "tool": {"type": "string"}}}}}},
    }},
]

TOOL_DEFINITIONS.append({"type": "function", "function": {
    "name": "ask_selection", "description": "Ask the designer to select elements. Only their click starts observation.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["prompt", "mode"],
                   "properties": {"prompt": {"type": "string"},
                                  "mode": {"type": "string", "enum": ["single", "multiple", "box"]},
                                  "category": {"type": "string", "description": "BuiltInCategory such as OST_Walls"}}},
}})
for descriptor in list_host_functions():
    params = descriptor["parameters"]
    TOOL_DEFINITIONS.append({"type": "function", "function": {
        "name": descriptor["name"], "description": descriptor["description"],
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {p["name"]: {"type": p["type"]} for p in params},
                       "required": [p["name"] for p in params if p["required"]]},
    }})

TOOL_DEFINITIONS.append({"type": "function", "function": {
    "name": "use_workflow",
    "description": "Use a saved workflow from list_tools. Supply only inputs the designer gave. "
                   "The host asks for missing inputs and builds each card from the saved steps.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["name", "inputs"],
                   "properties": {"name": {"type": "string"}, "inputs": {"type": "object"}}},
}})
TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOL_DEFINITIONS)


@dataclass
class ToolOutcome:
    """What one tool call produced: the model's result and, for ``propose_spec``,
    the payload of the ``spec`` event for the page."""
    result: object
    spec_event: dict | None = None
    questions_event: dict | None = None
    plan_event: dict | None = None
    read_tool: str | None = None
    stop_reply: str | None = None
    image_event: dict | None = None


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

async def _ask_designer(args: dict, session: Session) -> ToolOutcome:
    try:
        event = DesignerForm.model_validate(args).event()
    except ValidationError as exc:
        return ToolOutcome({"error": "invalid_questions", "message": str(exc)})
    return ToolOutcome(event, questions_event=event)


async def _ask_selection(args: dict, session: Session) -> ToolOutcome:
    try:
        question = SelectionQuestion.model_validate(args)
    except ValidationError:
        return ToolOutcome({"error": "invalid_selection"})
    event = {"ask_id": uuid4().hex, "selection": question.model_dump(exclude_none=True)}
    return ToolOutcome({"awaiting_designer": True}, questions_event=event)


async def _view(name: str, args: dict, session: Session) -> ToolOutcome:
    capture = name == "capture_view_image"
    allowed = {"width", "view_id"} if capture else {"name"}
    if args.keys() - allowed or (not capture and not isinstance(args.get("name"), str)):
        return ToolOutcome({"error": "invalid_args"})
    plan = session.plan
    step = plan.current if plan and plan.current and plan.current.tool == name else None
    if step and step.status != "pending":
        return ToolOutcome({"error": "invalid_step"})
    if limit := reserve_direct_run(session):
        return ToolOutcome(limit)
    command = view_commands.capture_view_image if capture else view_commands.open_view
    scope = current_scope()
    reply = await command(await get_revit_client(), **args, ledger=bridge.get_ledger(), host=bridge.HOST, scope=scope)
    # The native diagnostic, warnings and view name could echo the PNG. Allow only safe
    # metadata into the model result, tool trace, plan, and interaction log.
    success = reply["error"] is None
    result = {"success": success, "evidence_id": reply["evidence_id"]}
    image = None
    if capture:
        if success:
            result.update(width=reply["width"], height=reply["height"])
            image = {k: reply[k] for k in ("width", "height", "view_name", "png_base64")}
            image["capture_id"] = captures.capture_id(session.session_id, scope, reply["png_base64"])
        else:
            result["error"] = reply["error"] if reply["error"] in ("invalid_args", "unsupported_addin") else "capture_failed"
    else:
        result.update(reply, ui_only=True)
    outcome = _direct_outcome(name, result, session, step, chinese=session.chinese, recorded=result)
    outcome.image_event = image
    return outcome


def _direct_outcome(name, read, session, step, *, chinese, recorded):
    if "success" not in read:
        return ToolOutcome(read)
    outcome = ToolOutcome(read, read_tool=name)
    evidence = read.get("evidence_id")
    if read["success"] and evidence:
        session.read_results[current_scope(), evidence] = recorded
    if step:
        from backend.roles import step_result
        step.status = "done" if read["success"] else "failed"
        step.result = step_result(read)
        step.recorded_result = recorded
        step.evidence_id = evidence
        session.spec = session.plan.proposed = None
        outcome.plan_event = session.plan.event()
        if step.status == "failed":
            session.auto_pause_reason = "previous_failure"
            question = ("读取失败，接下来怎么做？" if chinese else "The read failed. How should we proceed?")
            if read.get("ui_only"):
                question = "Revit 操作失败，接下来怎么做？" if chinese else "The Revit action failed. How should we proceed?"
            outcome.questions_event = DesignerForm(questions=[{
                "id": "next_step", "text": question,
                "options": [{"value": "revise", "label": "修改" if chinese else "Revise"},
                            {"value": "continue", "label": "跳过" if chinese else "Skip"},
                            {"value": "stop", "label": "结束" if chinese else "End task"}],
            }]).event()
            outcome.stop_reply = "任务已结束。" if chinese else "This task has ended."
    return outcome


async def _set_plan(args: dict, session: Session) -> ToolOutcome:
    plan = session.plan if session.plan and session.plan.current else None
    if plan and plan.workflow:
        # Ordinary plan revisions may skip saved steps, but cannot insert or rewrite one.
        saved = [{k: s[k] for k in ("id", "title", "tool")} for s in plan.workflow.definition["steps"]]
        steps = args.get("steps")
        if not isinstance(steps, list) or steps != [s for s in saved if s in steps]:
            return ToolOutcome({"error": "invalid_plan", "message": "A saved workflow's steps cannot be rewritten."})
    if plan is None:
        if session.proposed_in_request:
            return ToolOutcome({"error": "plan_after_card", "message": "Set the plan before proposing its first card."})
        plan = Plan(session.request_text, list(session.request_answers), current_scope())
    try:
        if args.keys() != {"steps"}:
            raise ValueError("Supply the steps array.")
        previously_active = {s.id for s in plan.steps if s.status != "skipped"} if plan.workflow else set()
        plan.revise(args["steps"])
    except ValueError as exc:
        return ToolOutcome({"error": "invalid_plan", "message": str(exc)})
    if any(s.status == "skipped" and s.id in previously_active for s in plan.steps):
        session.auto_pause_reason = "workflow_changed"
    session.plan = plan
    if not plan.proposed:
        session.spec = None
    return ToolOutcome(plan.context(), plan_event=plan.event())


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
    """Read from disk on every call; no directory paths for the model."""
    packs = list_packs()
    for pack in packs:
        if pack["read_only"] and pack["builtin"]:
            pack["description"] += " Propose this reader to get its result directly in this turn."
        if pack.get("ui_only") and pack["builtin"]:
            pack["description"] += " Propose this view or selection tool to run it directly in Revit."
    return ToolOutcome(packs + catalogue())


async def _get_tool_choices(args: dict, session: Session) -> ToolOutcome:
    name = require_tool_name(args.get("name"))         # call_tool reports the refusal
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
    name = require_tool_name(args.get("tool"))         # call_tool reports the refusal
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
    pack = pack_for(draft, ToolStore())          # a bad tool name is refused before Revit is asked
    # The pick check of POST /spec/reconcile, so the model learns of a bad binding before it proposes.
    refused = mention_errors(draft, pack, session.picks_for(current_scope()))
    if refused:
        return ToolOutcome({"error": "invalid_spec", "message": "; ".join(e.message for e in refused),
                            "errors": [e.model_dump() for e in refused]})
    given = args.get("snapshot")
    snapshot = parse_snapshot(given) if given is not None else await snapshot_now(await get_revit_client())
    return ToolOutcome(reconcile(draft, snapshot, pack).model_dump(mode="json"))


# -- the host's tool -----------------------------------------------------------------

def _refused(code: str, message: str) -> ToolOutcome:
    """A verdict for the model only: no card for the page, the session's last spec unchanged."""
    return ToolOutcome({"accepted": False, "errors": [{"code": code, "param": None, "message": message}],
                        "reconcile": None})


async def _propose_spec(args: dict, session: Session, *, host_workflow=False) -> ToolOutcome:
    """validate_spec + reconcile; the page sees the card, the model the verdict. An
    ``execute_code`` spec and a ``run_tool`` spec whose tool is not a tool name are
    refused before anything else, with no card."""
    plan = session.plan
    step = plan.current if plan else None
    if step and plan.workflow and not host_workflow:
        return _refused("workflow_step", "The host builds this card from the saved workflow.")
    given = args.get("spec")
    try:
        spec = TaskSpec.model_validate(given)
    except ValidationError as exc:
        # Nothing the page could render, nothing worth keeping: the verdict goes to
        # the model only and the session's last spec stays what it was.
        return _refused("invalid_spec", str(exc))
    if spec.action.kind == "execute_code":
        return _refused(CODE_NOT_OFFERED, CODE_NOT_OFFERED_MESSAGE)
    if spec.action.tool:
        try:
            require_tool_name(spec.action.tool)
        except ApiError as exc:
            return _refused(exc.payload["error"], exc.payload["message"])
    if "step_id" in args and (not step or args["step_id"] != step.id):
        return _refused("invalid_step", "The proposal must identify the current unfinished step.")
    if step and (plan.confirmed or plan.reporting):
        return _refused("invalid_step", f"Step {step.id} is awaiting its execution report.")
    store = ToolStore()
    if spec.action.tool and not host_workflow:
        read_step = step if "step_id" in args and step.tool == spec.action.tool else None
        read = await try_read(spec, session, store, step=read_step)
        if read is not None:
            return _direct_outcome(spec.action.tool, read, session, read_step,
                                   chinese=spec.language == "zh", recorded=read.get("result"))
    if step and "step_id" not in args:
        return _refused("invalid_step", f"Propose only the current unconfirmed plan step {step.id}.")
    if step:
        # The existing workflow envelope travels unchanged from the card to /spec/confirm.
        # Even two identical actions in different steps have distinct confirmation identities.
        workflow = spec.workflow.model_dump() if spec.workflow else {"name": "", "stage": ""}
        workflow["name"] = f"{workflow['name']} [plan {plan.id}]".strip()
        workflow["stage"] = f"{workflow['stage']} [step {step.id}; card {uuid4().hex}]".strip()
        spec = TaskSpec.model_validate({**spec.model_dump(), "workflow": workflow})
    session.proposed_in_request = True
    normalized = spec.model_dump(mode="json")
    if not step:
        session.spec = normalized
    pack = pack_for(spec, store)
    # A parameter bound to a pick (mention:<kind>#<id>) must be one verified in this session,
    # bound as that pick is: unknown_mention otherwise (backend.mention_check).
    picks = session.picks_for(current_scope())
    errors = [e.model_dump() for e in validate_spec(spec, pack) + mention_errors(spec, pack, picks)]
    event = {"spec": normalized, "card": spec.card(), "errors": errors, "reconcile": None}
    if selection := selection_for_card(spec, pack, session):
        event["assistant_selection"] = selection
    try:
        report = reconcile(spec, await snapshot_now(await get_revit_client()), pack_for(spec, store)).model_dump(mode="json")
    except ApiError as exc:
        # No Revit to reconcile against: not accepted; the page sees the card and why
        # (reconcile stays null, the package's payload rides in reconcile_error).
        event["reconcile_error"] = exc.payload
        if step:
            if session.plan is not plan or plan.current is not step or plan.confirmed or plan.reporting:
                return _refused("invalid_step", "The current plan step changed while checking this card.")
            step.status = "pending"
            session.spec = plan.proposed = None
        return ToolOutcome({"accepted": False, "errors": errors, "reconcile": exc.payload}, spec_event=event,
                           plan_event=plan.event() if step else None)
    event["reconcile"] = report
    accepted = not errors and bool(report["ready"])
    if step and (session.plan is not plan or plan.current is not step or plan.confirmed or plan.reporting):
        return _refused("invalid_step", "The current plan step changed while checking this card.")
    checkable = (not report["conflicts"] and not report["questions"]
                 and all(e["code"] == "unconfirmed_interpretation" for e in errors))
    if checkable and step:
        step.status = "proposed"
        session.spec = plan.proposed = normalized
        plan.interpretations = [*normalized["interpretations"], *report["interpretations_required"]]
    elif step:
        step.status = "pending"
        session.spec = plan.proposed = None
    return ToolOutcome({"accepted": accepted, "errors": errors, "reconcile": report}, spec_event=event,
                       plan_event=plan.event() if step else None)


async def workflow_card(session, interpretations=None):
    plan = session.plan
    spec = plan.workflow.card(plan, session.chinese)
    if interpretations:
        spec["interpretations"] = interpretations
    return await _propose_spec({"spec": spec, "step_id": plan.current.id}, session, host_workflow=True)


async def _use_workflow(args, session):
    if session.proposed_in_request or (session.plan and session.plan.current):
        return ToolOutcome({"error": "plan_after_card", "message": "Finish the current task first."})
    session.pending_workflow = WorkflowRun.start(args, session, current_scope())
    return ToolOutcome({"workflow": session.pending_workflow.name})


_HANDLERS = {
    "use_workflow": _use_workflow,
    "set_plan": _set_plan,
    "get_project_snapshot": _get_project_snapshot,
    "query": _query,
    "list_tools": _list_tools,
    "get_tool_choices": _get_tool_choices,
    "missing_params": _missing_params,
    "reconcile": _reconcile,
    PROPOSE_SPEC: _propose_spec,
    "ask_designer": _ask_designer,
    "ask_selection": _ask_selection,
    "open_view": partial(_view, "open_view"),
    "capture_view_image": partial(_view, "capture_view_image"),
}
