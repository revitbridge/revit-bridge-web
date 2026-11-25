"""Bind a saved workflow to one designer request and its host-recorded plan results."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from revit_bridge.capabilities import ToolStore
from revit_bridge.spec.models import ParamBinding
from revit_bridge.workflows import resolve_step_params

from backend.api.workflows import invalid, list_workflows, saved_workflow
from backend.plans import Plan, Step, same_json
from backend.questions import DesignerForm
from backend.workflow_validation import binding_errors


def catalogue():
    return [{"kind": "workflow", "origin": "user", "name": w["name"], "summary": w["summary"],
             "description": w["description"][:600],
             "inputs": [{**i, "description": i["description"][:120]}
                        for i in w["inputs"][:12]]} for w in list_workflows()]


def _invocation_match(text, definition):
    names = "|".join(re.escape(definition[k]) for k in ("name", "display_name"))
    return re.fullmatch(r"(?:(?:please\s+)?(?:run|use|execute)\s+(?:the\s+)?(?:workflow\s+)?|"
                        r"(?:请)?(?:运行|执行|使用)\s*(?:工作流)?\s*)"
                        rf"(?:{names})(?:\s+workflow)?(?:\s+with\s+(?P<arguments>.+?))?[.!。！]?", text.strip(), re.I)


def invocation(text, definition, supplied):
    """Return the explicitly named inputs, or None when the Run form is needed."""
    match = _invocation_match(text, definition)
    if not match or any(w["name"] != definition["name"] and _invocation_match(text, w) for w in list_workflows()):
        return None
    named = set()
    if not match["arguments"]:
        return named
    for assignment in match["arguments"].split(","):
        name, equals, literal = assignment.strip().partition("=")
        value = supplied.get(name.strip())
        if not equals or type(value) not in (str, bool, int, float):
            return None
        shown = json.dumps(value, ensure_ascii=False)
        if literal.strip() != shown and not (isinstance(value, str) and literal.strip() == value and value.isalnum()):
            return None
        named.add(name.strip())
    return named


@dataclass
class WorkflowRun:
    definition: dict
    scope: str
    invoked: bool
    designer_message: str
    inputs: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    form_id: str | None = None
    questions: dict = field(default_factory=dict)
    chat_input: str | None = None

    @property
    def name(self):
        return self.definition["name"]

    @classmethod
    def start(cls, args, session, scope):
        from backend.api.auto_run import _supported
        if args.keys() - {"name", "inputs"} or not isinstance(args.get("inputs", {}), dict):
            raise invalid(["Supply a saved workflow name and an object of designer inputs."])
        definition = saved_workflow(args.get("name"))
        supplied = args.get("inputs", {})
        if supplied.keys() - {i["name"] for i in definition["inputs"]}:
            raise invalid(["inputs: unknown workflow input"])
        message = session.designer_message
        named = invocation(message, definition, supplied)
        run = cls(definition, scope, named is not None, message)
        for item in definition["inputs"]:
            name = item["name"]
            if name not in supplied or (item["ask"] and (named is None or name not in named)):
                continue
            value = supplied[name]
            if type(value) not in (int, float, bool, str):
                continue
            # Verify against the actual request or its verified form, before retaining
            # any model-supplied value (also in click-to-confirm mode).
            quote = str(value).lower() if isinstance(value, bool) else str(value)
            binding = ParamBinding(name=name, value=value, evidence=quote, source="designer")
            params = run.uses(name) or [item]
            words = message if item["ask"] else session.request_text + "\n" + message
            if all(_supported(binding, session, words, [], p["type"], p.get("unit")) for p in params):
                run.inputs[name] = value
                run.evidence[name] = ("answer", quote)
                continue
            if item["ask"]:
                continue
            for entry in reversed(session.request_answers):
                answers = entry.get("answers")
                if not isinstance(answers, list) or not entry.get("ask_id"):
                    continue
                if all(_supported(binding, session, "", [entry], p["type"], p.get("unit")) for p in params):
                    run.inputs[name] = value
                    run.evidence[name] = ("answer", entry["ask_id"] + "#" + name)
                    break
        return run

    def uses(self, name):
        store = ToolStore()
        return [p for step in self.definition["steps"] if {"input": name} in step["params"].values()
                for p in self.pack(store, step).parameters if step["params"].get(p["name"]) == {"input": name}]

    @staticmethod
    def pack(store, step):
        pack = store.load(step["tool"])
        if pack is None:
            raise invalid([f"step {step['id']}: unknown tool {step['tool']}"])
        return pack

    def fresh(self, *, check_bindings=False):
        if not same_json(saved_workflow(self.name), self.definition):
            raise invalid(["The saved workflow changed. Start a new run to use the new definition."])
        if check_bindings and (errors := binding_errors(self.definition, ToolStore())):
            raise invalid(errors)

    def next_form(self, zh, *, automatic):
        self.questions = {}
        confirm_invocation = automatic and not self.invoked
        if confirm_invocation:
            questions = [{"id": "invoke", "text": (f"运行 {self.name}？" if zh else f"Run {self.name}?"),
                          "options": [{"value": "run", "label": "运行" if zh else "Run workflow"},
                                      {"value": "cancel", "label": "取消" if zh else "Cancel"}]}]
        else:
            used = {s["input"] for step in self.definition["steps"] for s in step["params"].values()
                    if isinstance(s, dict) and "input" in s}
            missing = [i for i in self.definition["inputs"] if (i["ask"] or i["name"] in used) and i["name"] not in self.inputs]
            if not missing:
                return None
            # One question allows a typed chat reply to be bound unambiguously too.
            item = missing[0]
            self.questions["input"] = item
            options = ([{"value": b, "label": str(b).lower()} for b in (True, False)]
                       if item["type"] in ("bool", "boolean") else [
                           {"value": "reply", "label": "在对话框中填写" if zh else "Type in chat"},
                           {"value": "cancel", "label": "取消工作流" if zh else "Cancel workflow"}])
            questions = [{"id": "input", "text": f"{item['name']} - {item['description']}"[:500],
                          "allow_other": True, "options": options}]
        event = DesignerForm(questions=questions).event()
        if confirm_invocation:
            event["workflow_preview"] = {"summary": self.definition["summary"], "steps": [
                {"title": s["title"], "tool": s["tool"]} for s in self.definition["steps"]]}
        self.form_id = event["ask_id"]
        return event

    def answer(self, verified, text):
        """Only called for this run's own form (or its explicitly requested text reply)."""
        answer = verified[0] if verified else None
        if "input" not in self.questions:
            self.invoked = bool(answer and answer["value"] == "run")
            return self.invoked
        item = self.questions["input"]
        if answer and not answer.get("other") and answer["value"] == "cancel":
            return False
        if answer and not answer.get("other") and answer["value"] == "reply":
            self.chat_input = item["name"]
            return True
        self.chat_input = None
        value = answer["value"] if answer else text.strip()
        label = answer["label"] if answer else text.strip()
        if isinstance(value, str) and item["type"] != "string":
            try:
                value = json.loads(value)
            except ValueError:
                pass
        name = item["name"]
        self.inputs[name] = value
        self.evidence[name] = ("answer", self.form_id + "#" + name)
        # Keep the checked label as well as the typed scalar. The ordinary answer
        # verifier will still check that the visible text proves this value and unit.
        self.labels[name] = label
        return True

    def plan(self, session):
        store = ToolStore()
        errors = []
        for step in self.definition["steps"]:
            tool = self.pack(store, step)
            # Check the actual answers before the first card. An invalid answer
            # reopens that input's form for correction.
            for key, source in step["params"].items():
                if not isinstance(source, dict) or "input" not in source:
                    continue
                try:
                    resolve_step_params({**step, "params": {key: source}}, self.inputs, {}, tool)
                except ValueError as exc:
                    errors.append(str(exc))
                    self.inputs.pop(source["input"], None)
        if errors:
            raise invalid(errors)
        return Plan(session.request_text, list(session.request_answers), self.scope, workflow=self,
                    steps=[Step(s["id"], s["title"], s["tool"]) for s in self.definition["steps"]])

    def step(self, ident):
        return next(s for s in self.definition["steps"] if s["id"] == ident)

    def fixed_ref(self, step, param):
        return f"workflow:{self.name}@{self.definition['version']}#{step}.{param}"

    def fixed_matches(self, plan, tool, binding):
        if not self.invoked or not plan.current or plan.current.tool != tool:
            return False
        step = self.step(plan.current.id)
        value = step["params"].get(binding.name)
        return (value is not None and not isinstance(value, dict) and same_json(value, binding.value)
                and binding.evidence == self.fixed_ref(step["id"], binding.name))

    def card(self, plan, zh):
        self.fresh()
        step = self.step(plan.current.id)
        tool = self.pack(ToolStore(), step)
        earlier = {s.id: s for s in plan.steps if s.status == "done" and s.evidence_id}
        values = resolve_step_params(step, self.inputs, {k: s.recorded_result for k, s in earlier.items()}, tool)
        return self.spec(step, tool, values, {k: s.evidence_id for k, s in earlier.items()}, zh)

    def spec(self, step, tool, values, receipts, zh):
        parameters = []
        for p in tool.parameters:
            key = p["name"]
            if key not in step["params"]:
                continue
            source = step["params"][key]
            if not isinstance(source, dict):
                origin, evidence = "answer", self.fixed_ref(step["id"], key)
            elif "input" in source:
                origin, evidence = self.evidence[source["input"]]
            else:
                origin = "tool"
                ref = receipts.get(source["from"])
                evidence = f"result:{ref}#{source['path']}" if ref else f"pending result from {source['from']}"
            parameters.append({"name": key, "value": values.get(key), "unit": p.get("unit"),
                               "source": origin, "evidence": evidence})
        return {"task": step["title"], "action": {"kind": "run_tool", "tool": tool.name}, "parameters": parameters,
                "workflow": {"name": self.name, "stage": step["id"]}, "language": "zh" if zh else "en"}
