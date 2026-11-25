"""Execute only readers declared by the installed package and supplied by the wheel."""
from starlette.concurrency import run_in_threadpool

from revit_bridge.capabilities import ToolStore
from revit_bridge.execution import run_pack
from revit_bridge.spec.gate import GateError
from revit_bridge.spec.rules import reconcile, validate_spec

from backend.api import bridge
from backend.mention_check import mention_errors

MAX_READS = 10


class BuiltinStore(ToolStore):
    """Keep every package lookup on the selected built-in, including render after validation."""

    def path_of(self, name):
        path = super().path_of(name)
        return path if path is not None and path.parent == self.builtin_dir else None


async def try_read(spec, session, store, *, step=None):
    name = spec.action.tool
    pack = store.load(name)
    if getattr(pack, "read_only", False) is not True or bridge.pack_origin(store, name)[0] != "builtin":
        return None
    readers = BuiltinStore(user_dir=store.user_dir, builtin_dir=store.builtin_dir)
    pack = readers.load(name)
    if getattr(pack, "read_only", False) is not True:
        return None
    if step and step.status != "pending":
        return {"error": "invalid_step", "message": "Omit step_id for a supporting read or revise the plan."}
    if session.direct_reads >= MAX_READS:
        return {"error": "read_limit", "message": "Read limit reached for this message. Use the results already returned."}
    session.direct_reads += 1
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
    confirmation = await run_in_threadpool(gate.issue, spec, confirmed_by="host:read-only",
                                           channel=bridge.CONFIRM_CHANNEL, scope=scope)
    projection = spec.execution_projection()
    try:
        result = await run_pack(store=readers, gate=gate, ledger=bridge.get_ledger(), client=client, name=name,
                                params=projection["params"], token=confirmation.token, host=bridge.HOST, scope=scope)
        return bridge._execution_payload(result)
    finally:
        try:
            await run_in_threadpool(gate.consume, confirmation.token, projection, scope)
        except GateError:
            pass  # run_pack consumed it
