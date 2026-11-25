"""A designer's multi-step task, kept independently of the rolling model history."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import uuid4


def same_json(left, right):
    """Compare JSON values as the browser returns them. JSON booleans and numbers are distinct."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, dict):
        return isinstance(right, dict) and left.keys() == right.keys() and all(same_json(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return isinstance(right, list) and len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


@dataclass
class Step:
    id: str
    title: str
    tool: str | None = None
    status: str = "pending"
    evidence_id: str | None = None
    result: dict | None = None
    retries: int = 0
    # The actual result for provenance, separate from the clipped model context.
    recorded_result: object = field(default=None, repr=False)

    def event(self):
        item = {"id": self.id, "title": self.title, "status": self.status}
        if self.evidence_id:
            item["evidence_id"] = self.evidence_id
        return item


@dataclass
class Plan:
    original_request: str
    answers: list[dict]
    scope: str
    id: str = field(default_factory=lambda: uuid4().hex)
    steps: list[Step] = field(default_factory=list)
    proposed: dict | None = None
    interpretations: list[dict] = field(default_factory=list)
    confirmed: bool = False
    reporting: bool = False
    token_digest: str | None = None
    receipt: dict | None = None
    expires_at: float = 0
    running: bool = False
    closed: bool = False
    auto_receipt_pending: bool = False

    @staticmethod
    def fingerprint(token: str):
        return hashlib.sha256(token.encode()).hexdigest()

    def matches_card(self, spec):
        if self.proposed is None:
            return False
        if any(not same_json(spec.get(k), v) for k, v in self.proposed.items() if k != "interpretations"):
            return False

        def key(item):
            return item.get("param"), item["text"]

        original = [key(item) for item in self.proposed["interpretations"]]
        extra = {key(item) for item in self.interpretations} - set(original)
        chosen = spec.get("interpretations", [])
        return (len(chosen) == len(original) + len(extra) and all(i["confirmed"] for i in chosen)
                and [key(i) for i in chosen[:len(original)]] == original
                and {key(i) for i in chosen[len(original):]} == extra)

    def matches_report(self, report):
        if self.receipt is None:
            return False
        evidence = self.receipt.get("evidence_id")
        if evidence:
            return report.get("evidence_id") == evidence
        # A preflight failure has no Revit evidence. Its error code and message come
        # from this token's server-side attempt; the model receives that saved result.
        return (not report.get("evidence_id") and report.get("success") is False
                and report.get("error") == self.receipt.get("error")
                and report.get("message") == self.receipt.get("message"))

    @property
    def current(self):
        if self.closed:
            return None
        return next((step for step in self.steps if step.status not in ("done", "skipped")), None)

    def event(self):
        return {"plan_id": self.id, "steps": [s.event() for s in self.steps]}

    def context(self):
        return {"plan_id": self.id, "original_request": self.original_request,
                "current_step": self.current.id if self.current else None, "form_answers": self.answers,
                "steps": [{**s.event(), "tool": s.tool, "result": s.result} for s in self.steps]}

    def revise(self, raw):
        if not isinstance(raw, list) or len(raw) > 8:
            raise ValueError("A plan needs 2 to 8 steps.")
        if self.confirmed or self.reporting:
            raise ValueError("Wait for the confirmed step's execution report before revising the plan.")
        previous = {s.id: s for s in self.steps}
        steps = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict) or item.keys() - {"id", "title", "tool"}:
                raise ValueError("Each step needs id and title, with an optional tool.")
            for key, limit in (("id", 100), ("title", 500), ("tool", 100)):
                if key == "tool" and key not in item:
                    continue
                if not isinstance(item.get(key), str) or not 0 < len(item[key].strip()) <= limit:
                    raise ValueError(f"Give the step a nonempty {key} of at most {limit} characters.")
            ident, title, tool = item["id"].strip(), item["title"].strip(), item.get("tool")
            if ident in seen:
                raise ValueError("Step ids must be unique.")
            seen.add(ident)
            old = previous.get(ident)
            if old and old.status == "done":
                if title != old.title or tool != old.tool:
                    raise ValueError("A completed step cannot be changed or moved.")
                steps.append(old)
            elif old and old.title == title and old.tool == tool:
                steps.append(old)
            else:
                steps.append(Step(ident, title, tool, "pending",
                                  old.evidence_id if old else None, old.result if old else None,
                                  old.retries if old else 0))
        if any(s.status == "done" and s.id not in seen for s in self.steps):
            raise ValueError("A completed step cannot be removed.")
        omitted = [s for s in self.steps if s.id not in seen]
        if len(steps) + len(omitted) < 2:
            raise ValueError("A plan needs 2 to 8 steps.")
        if len(steps) + len(omitted) > 8:
            raise ValueError("A plan can retain at most 8 steps, including skipped steps.")
        positions = {s.id: i for i, s in enumerate(self.steps)}
        for old in omitted:
            where = next((i for i, s in enumerate(steps)
                          if s.id in previous and positions[s.id] > positions[old.id]), len(steps))
            steps.insert(where, Step(old.id, old.title, old.tool, "skipped", old.evidence_id, old.result, old.retries))
        if any(old.status == "done" and steps[i] is not old for i, old in enumerate(self.steps)):
            raise ValueError("Completed steps must remain in their original positions.")
        previous_current = self.current
        self.steps = steps
        self.closed = self.current is None
        if self.current is not previous_current:
            self.proposed = None
            if previous_current and previous_current.status == "proposed":
                previous_current.status = "pending"

    def stop(self):
        for step in self.steps:
            if step.status in ("pending", "proposed"):
                step.status = "skipped"
        self.closed = True
        self.proposed = None
        self.confirmed = False
