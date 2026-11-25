"""Short-lived source for remote dry runs. The package ledger supplies their evidence."""
import asyncio
import hashlib
import json
from dataclasses import dataclass

from revit_bridge.evidence.ledger import Ledger
from revit_bridge.paths import data_root

CHANNEL = "remote-mcp"
MAX_DRAFTS = 20
MAX_BYTES = 64 * 1024
TTL_SECONDS = 24 * 3600


def ledger_for(device_id):
    from revit_bridge.mcp_app import scope_directory
    root = data_root() if device_id == "local" else scope_directory(device_id)
    return Ledger(root / "evidence")


def recent_calls(device_id):
    ledger = ledger_for(device_id)
    limit = 20
    while True:
        rows = ledger.recent(limit, scope=device_id)
        calls = []
        for row in rows:
            if row.get("host") == CHANNEL:
                action = "dry_run_code" if row["action"] == "dry_run" else row["action"]
                calls.append({"time": row["ts"], "tool": row.get("tool") or action,
                              "success": row["success"], "evidence_id": row["id"]})
        if len(calls) >= 20 or len(rows) < limit:
            return calls[:20]
        limit *= 2


@dataclass
class Draft:
    code: str
    evidence: dict
    expires: asyncio.TimerHandle


class RemoteTrials:
    """Owned by one app/event loop. Expiry also runs when the device is idle."""

    def __init__(self):
        self.devices: dict[str, dict[str, Draft]] = {}

    def discard(self, device_id, evidence_id):
        drafts = self.devices[device_id]
        drafts.pop(evidence_id).expires.cancel()
        if not drafts:
            del self.devices[device_id]

    def remember(self, device_id, code, record):
        if (not record or record.get("host") != CHANNEL or record.get("scope") != device_id
                or record.get("action") != "dry_run" or record.get("success") is not True
                or (record.get("result_summary") or {}).get("rolled_back") is not True):
            return
        source = code.encode("utf-8")
        if len(source) > MAX_BYTES or hashlib.sha256(source).hexdigest() != record["code_sha256"]:
            return
        eid = record["id"]
        drafts = self.devices.setdefault(device_id, {})
        if eid in drafts:
            return
        if len(drafts) == MAX_DRAFTS:
            self.discard(device_id, next(iter(drafts)))
        timer = asyncio.get_running_loop().call_later(TTL_SECONDS, self.discard, device_id, eid)
        fields = {"time": record["ts"], "evidence_id": eid, "code_sha256": record["code_sha256"], "rolled_back": True}
        drafts[eid] = Draft(code, fields, timer)

    def close(self):
        for drafts in self.devices.values():
            for draft in drafts.values():
                draft.expires.cancel()
        self.devices.clear()


def trial_evidence_id(body):
    """The package returns a JSON text content block in its SDK JSON response."""
    try:
        reply = json.loads(json.loads(body)["result"]["content"][0]["text"])
        if reply["success"] is True and reply["rolled_back"] is True and isinstance(reply["evidence_id"], str):
            return reply["evidence_id"]
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        pass
    return None
