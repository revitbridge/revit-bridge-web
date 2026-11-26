"""Execute read-only and UI-only built-ins from the installed wheel."""
from starlette.concurrency import run_in_threadpool

from revit_bridge.capabilities import ToolStore
from revit_bridge.spec.gate import GateError
from revit_bridge.spec.rules import reconcile, validate_spec

from backend.api import bridge
from backend.config import get_settings
from backend.mention_check import mention_errors
from backend.selection import SELECTION_WRITERS

MAX_CARD_IDS = 20


def selection_record(name, params, result):
    result = result if isinstance(result, dict) else {}
    count = result.get("Count")
    ids = result.get("ElementIds", result.get("Ids"))
    if not isinstance(ids, list):
        ids = []
        # Only replace makes the supplied IDs the complete resulting selection.
        if name == "select_elements" and params.get("mode") in (None, "replace"):
            ids = list(dict.fromkeys(s.strip().lstrip("0") for s in str(params["element_ids"]).split(",")))
            if count is not None and count != len(ids):
                ids = []
    if type(count) is not int or count < 0:
        count = len(ids) if ids or name == "clear_selection" else None
    return {"tool": name, "count": count, "ids": [str(i) for i in ids[:MAX_CARD_IDS]]}


def selection_for_card(spec, pack, session):
    record = session.assistant_selections.get(bridge.current_scope())
    if not record or not pack:
        return None
    if pack.name == "delete_selected":
        return record
    params = spec.execution_projection()["params"]
    for p in pack.parameters:
        if (p["name"] in ("element_id", "element_ids") and p.get("default") == ""
                and params.get(p["name"]) in (None, "")):
            return record
    return None


def reserve_direct_run(session):
    if session.direct_reads >= get_settings().direct_run_limit:
        return {"error": "read_limit", "message": "Direct run limit reached for this message. Use the results already returned."}
    session.direct_reads += 1


class BuiltinStore(ToolStore):
    """Keep every package lookup on the selected built-in, including render after validation."""

    def path_of(self, name):
        path = super().path_of(name)
        return path if path is not None and path.parent == self.builtin_dir else None


async def try_read(spec, session, store, *, step=None):
    name = spec.action.tool
    pack = store.load(name)
    if pack is None or not (pack.read_only or pack.ui_only) or bridge.pack_origin(store, name)[0] != "builtin":
        return None
    readers = BuiltinStore(user_dir=store.user_dir, builtin_dir=store.builtin_dir)
    pack = readers.load(name)
    if pack is None or not (pack.read_only or pack.ui_only):
        return None
    if step and step.status != "pending":
        return {"error": "invalid_step", "message": "Omit step_id for a supporting read or revise the plan."}
    if limit := reserve_direct_run(session):
        return limit
    scope = bridge.current_scope()
    errors = validate_spec(spec, pack) + mention_errors(spec, pack, session.picks_for(scope))
    if not errors:
        errors = await bridge.recheck_picks(spec, session.session_id)
    if errors:
        return {"error": "invalid_spec", "errors": [e.model_dump() for e in errors]}
    client = await bridge.get_revit_client()
    report = reconcile(spec, await bridge.snapshot_now(client), pack)
    if not report.ready:
        return {"error": "not_ready", "reconcile": report.model_dump(mode="json")}
    gate = bridge.get_gate()
    confirmer = "host:ui-only" if pack.ui_only else "host:read-only"
    confirmation = await run_in_threadpool(gate.issue, spec, confirmed_by=confirmer,
                                           channel=bridge.CONFIRM_CHANNEL, scope=scope)
    projection = spec.execution_projection()
    try:
        result = await bridge.run_pack_on_device(store=readers, gate=gate, ledger=bridge.get_ledger(), client=client,
                                                 name=name, params=projection["params"], token=confirmation.token,
                                                 host=bridge.HOST, scope=scope)
        payload = bridge._execution_payload(result)
        if pack.ui_only:
            payload["ui_only"] = True
            if pack.name in SELECTION_WRITERS and payload.get("success"):
                session.assistant_selections[scope] = selection_record(pack.name, projection["params"], payload.get("result"))
        return payload
    finally:
        try:
            await run_in_threadpool(gate.consume, confirmation.token, projection, scope)
        except GateError:
            pass  # run_pack consumed it
