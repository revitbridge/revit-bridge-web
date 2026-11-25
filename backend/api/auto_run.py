"""Request-scoped automatic execution policy. Only a browser request selects this path."""
import asyncio
import json
import re
from decimal import Decimal, InvalidOperation

from revit_bridge.spec.models import TaskSpec

from backend.api import bridge
from backend.api.direct_reads import BuiltinStore
from backend.api.errors import ApiError
from backend.mention_check import evidence_keys
from backend.questions import DesignerForm
from backend.plans import same_json


def _value_pattern(value):
    text = str(value)
    if isinstance(value, bool):
        text = text.lower()
    if not text:
        return None
    return re.compile(r"(?<![a-zA-Z0-9_.+-])" + re.escape(text) + r"(?![a-zA-Z0-9_.])", re.I)


_NUMBER = re.compile(r"(?<![a-zA-Z0-9_.+-])[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?![\d_]|\.\d)")
_UNIT = re.compile(r"\s*(毫米|厘米|公分|公尺|米|英尺|英寸|度|[a-zA-Z]+|[°%'\"])", re.I)
_UNIT_NAMES = {"毫米": "mm", "厘米": "cm", "公分": "cm", "米": "m", "公尺": "m",
               "英寸": "in", "英尺": "ft", "度": "deg", "°": "deg", "'": "ft", '"': "in"}
_FIELD_NAMES = {"height": ("墙高", "高度"), "width": ("宽度",), "length": ("长度",),
                "sill_height": ("窗台高度", "窗台高")}


def _numeric_matches(text, value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return
    for match in _NUMBER.finditer(text):
        try:
            if Decimal(match[0]) == number:
                yield match
        except InvalidOperation:
            continue


def _text_supports(binding, text, quote, parameter_type, unit, *, answer_label=False):
    """Match the cited value in its original context, including the pack's units."""
    if not quote:
        return False
    numeric = parameter_type in ("double", "number", "float", "int", "integer") or type(binding.value) in (float, int)
    if numeric:
        matches = _numeric_matches(text, binding.value)
    else:
        pattern = _value_pattern(binding.value)
        if pattern is None:
            return False
        matches = pattern.finditer(text)
    citations = list(re.finditer(re.escape(quote), text, re.I))
    for match in matches:
        if not any(c.start() <= match.start() and match.end() <= c.end() for c in citations):
            continue
        prefix = text[:match.start()]
        if numeric:
            suffix = text[match.end():]
            stated = _UNIT.match(suffix)
            if stated:
                symbol = stated[1].casefold()
                if _UNIT_NAMES.get(symbol, symbol) != (unit or "").casefold():
                    continue
                suffix = suffix[stated.end():]
            if suffix and not re.match(r"[\s,，。;；.!?、)\]]", suffix):
                continue
            if not prefix.strip() and not suffix.strip() and (answer_label or stated):
                return True
            names = [r"(?<!\w)" + re.escape(binding.name).replace("_", "[ _]")]
            names.extend(_FIELD_NAMES.get(binding.name, ()))
            if re.search(r"(?:" + "|".join(names) + r")\s*(?:[=:：]|is\b|of\b|为)?\s*['\"]?$", prefix, re.I):
                return True
        else:
            assignment = re.search(r"([a-zA-Z_]\w*)\s*[=:：]\s*['\"]?$", prefix, re.I)
            if assignment is None or assignment[1].casefold() == binding.name.casefold():
                return True
    return False


def _supported(binding, session, words, answers, parameter_type=None, unit=None):
    keys = evidence_keys(binding.evidence)
    if keys:
        picks = session.picks_for(bridge.current_scope())
        return all(key in picks for key in keys)  # propose_spec already checked the values as well
    for entry in answers:
        items = entry.get("answers")
        if isinstance(items, list):
            for item in items:
                label = item.get("label", "")
                if (item.get("id") == binding.name and same_json(item.get("value"), binding.value)
                        and _text_supports(binding, label, label, parameter_type, unit, answer_label=True)):
                    return True
        elif isinstance(items, str):
            words += "\n" + items
        if isinstance(entry.get("answer"), str):
            words += "\n" + entry["answer"]
    return any(_text_supports(binding, line, binding.evidence.strip(), parameter_type, unit)
               for line in words.splitlines())


def _result_contains(result, binding, parameter):
    if same_json(result, binding.value):
        return True
    # The package accepts a decimal ID string on cards for integer element parameters.
    if (parameter.get("type") in ("int", "integer") and (binding.name == "id" or binding.name.endswith("_id"))
            and type(result) is int
            and isinstance(binding.value, str) and binding.value == str(result)):
        return True
    if isinstance(result, dict):
        children = result.values()
    elif isinstance(result, list):
        children = result
    else:
        return False
    return any(_result_contains(child, binding, parameter) for child in children)


class AutoRun:
    def __init__(self, session, request, settings, chinese, user_message, form_reply):
        self.session = session
        self.request = request
        self.settings = settings
        self.chinese = chinese
        self.user_message = user_message or ""
        self.approved = form_reply
        self.paused = False
        self.receipt = None


    def inspect(self, event):
        spec = TaskSpec.model_validate(event["spec"])
        name = spec.action.tool
        session = self.session
        plan = session.plan
        store = BuiltinStore()
        pack = store.load(name) if name else None
        if (not pack or name != pack.name
                or name.casefold() in {item.casefold() for item in self.settings.auto_run_exclude}):
            return "excluded_tool", None
        if self.paused or (plan and plan.current and plan.auto_paused):
            return "previous_failure", None
        if session.auto_count >= self.settings.auto_run_max_steps:
            return "step_limit", None
        report = event.get("reconcile") or {}
        if (not report or report.get("questions") or report.get("conflicts")
            or any(e["code"] != "unconfirmed_interpretation" for e in event["errors"])):
            return "not_ready", None
        assumptions = {(i.get("param"), i["text"]) for i in [
            *event["spec"].get("interpretations", []), *report.get("interpretations_required", [])]}
        key = (bridge.current_scope(), plan.id if plan and plan.current else None,
               plan.current.id if plan and plan.current else None, json.dumps(spec.execution_projection(), sort_keys=True))
        assumptions.update(session.pending_interpretations.get(key, set()))
        if assumptions:
            session.pending_interpretations[key] = assumptions
            identity = (key, assumptions)
            if self.approved != identity:
                text = "\n".join(sorted(text for _, text in assumptions))
                prompt = ("以下假设是否正确？\n" if self.chinese else "Are these assumptions correct?\n") + text
                if len(prompt) > 500:
                    prompt = "请核对卡片列出的全部假设。" if self.chinese else "Review all assumptions listed on the card."
                form = DesignerForm(questions=[{"id": "assumptions", "text": prompt, "options": [
                    {"label": "正确" if self.chinese else "Correct", "value": "accept"},
                    {"label": "需要修改" if self.chinese else "Revise", "value": "revise"}]}]).event()
                session.auto_assumptions = (form["ask_id"], identity)
                return "unconfirmed_interpretation", form
            if not report.get("ready") or event["errors"]:
                return "apply_approval", [{"param": param, "text": text, "confirmed": True}
                                          for param, text in sorted(assumptions, key=lambda pair: (pair[0] or "", pair[1]))]
        if not report.get("ready") or event["errors"]:
            return "not_ready", None
        words = (plan.original_request if plan and plan.current else session.request_text) + "\n" + self.user_message
        answers = plan.answers if plan and plan.current else session.request_answers
        declared = {p["name"]: p for p in pack.parameters}
        results = [result for scope, result in session.read_results if scope == bridge.current_scope()]
        if plan and plan.current:
            for earlier in plan.steps:
                if earlier is plan.current:
                    break
                if earlier.status == "done" and earlier.recorded_result is not None:
                    results.append(earlier.recorded_result)
        for binding in spec.parameters:
            parameter = declared.get(binding.name, {})
            if "default" in parameter and same_json(binding.value, parameter["default"]):
                continue
            if _supported(binding, session, words, answers, parameter.get("type"), parameter.get("unit")):
                continue
            if not any(_result_contains(result, binding, parameter) for result in results):
                return "unverified_evidence", None
        return None, None

    async def execute(self, spec):
        self.session.auto_count += 1

        try:
            bridge.confirm_rate_limit(self.request)
            store = BuiltinStore()
            confirmation = await bridge.issue_spec(bridge.ConfirmRequest(spec=spec, session_id=self.session.session_id,
                                                                         confirmed_by="designer:auto-run"), store)
            projection = TaskSpec.model_validate(spec).execution_projection()
            req = bridge.RunToolRequest(params=projection["params"], token=confirmation["token"])
            return await bridge.execute_pack(projection["tool"], req, store)
        except ApiError as exc:
            return {"success": False, **exc.payload}
        except Exception:
            return {"success": False, "error": "execution_failed", "message": "Check the model before retrying."}

    def start(self, spec):
        task = asyncio.create_task(self.execute(spec))
        self.session.auto_worker = task
        plan = self.session.plan
        single_step = not (plan and plan.current)
        scope = bridge.current_scope()
        def finished(worker):
            self.session.auto_worker = None
            if single_step and not worker.cancelled():
                self.session.pending_auto_receipt = (scope, worker.result())
            elif plan and plan.confirmed and plan.receipt is not None:
                plan.auto_receipt_pending = True
            if not worker.cancelled() and worker.result().get("success") is not True:
                self.paused = True
                if self.session.plan:
                    self.session.plan.auto_paused = True
        task.add_done_callback(finished)
        return task
