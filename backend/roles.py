"""The chat's three roles: who sees what, which tools each may call, and the hand-offs.

The host loop of ``backend.api.chat`` runs one user message in up to two role
segments and one execution report in up to two:

- **analyst** (``host_instructions(role="analyst", code=False)`` plus one host line,
  ``ANALYST_WORKBENCH_LINE``): no tools. Its one user message is the tool list (a name
  and one line each, ``tool_lines``), the digest of the open model (the package's
  ``snapshot_digest``), the open task when there is one (``open_task_block``) and the
  designer's message with the selections block. It answers ONE JSON object
  ``{route, tool?, facts_needed, note}`` (``parse_routing``). ``chat`` and ``none``: the
  host shows ``note`` (``none`` with the workbench link, added by the host when the note
  lacks it) and the turn ends.
- **planner** (``host_instructions(role="planner", code=False)`` plus
  ``chat.WORKBENCH_LINE``): the tools of the route (``ROUTE_TOOLS``; all planner tools on every
  route with ``ROLES_PLANNER_TOOLS=all``), the session's conversation, and this turn's
  message with the analyst's routing after it (``planner_message``) - not the analyst's
  prompt, digest or tool list. With ``ROLES_ANALYST=off`` every message comes to it with
  route tool (``FALLBACK_ROUTING``).
- **supervisor** (``host_instructions(role="supervisor", code=False)``): ``query`` only.
  One user message: the spec (confirmed, or the last proposal labelled as proposed), a
  summary of the execution result and the validator's per-check verdicts
  (``supervisor_message``) - not the planner's transcript. It answers ONE JSON object
  ``{verdict, report, fix?, question?}`` (``parse_verdict``); the host holds it to the
  package's verdict (``enforce``, on the raw result).

A role's output that is not that JSON gets one repair call (``REPAIR_INSTRUCTION``);
after that the host falls back (``FALLBACK_ROUTING``: route ``tool`` with the whole
planner; ``fallback_verdict``: the host reports the raw outcome and asks). A task is
sent back to the planner at most ``MAX_RETRIES`` times, never when the run succeeded and
never when it was a refusal by nature (``refusal_kind``).

Every piece of an execution result a model or the session gets is cut to a cap
(``TEXT_MAX``, ``ITEM_MAX``, ``LIST_MAX``, ``CHECKS_MAX``, ``RESULT_KEEP``), and the
supervisor's whole message to ``MESSAGE_KEEP``: the route takes results of up to 4 MiB.

Nothing here knows Revit: the texts, the digest, the selections block and the
validator's verdict all come from the package; this module only moves them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from backend.api.model_tools import TOOL_DEFINITIONS, WORKBENCH_LINK, WORKBENCH_PATH

ANALYST, PLANNER, SUPERVISOR = "analyst", "planner", "supervisor"
ROLES = (ANALYST, PLANNER, SUPERVISOR)

CHAT, QUERY, TOOL, NONE = "chat", "query", "tool", "none"
ROUTES = (CHAT, QUERY, TOOL, NONE)
ANSWERED_BY_ANALYST = (CHAT, NONE)          # the host shows the analyst's note; no planner
DONE, RETRY, ASK = "done", "retry", "ask"
VERDICTS = (DONE, RETRY, ASK)

MAX_RETRIES = 2                 # a task goes back to the planner at most this often
SUPERVISOR_TOOL_ROUNDS = 2      # query rounds of the supervisor before it must answer
DIGEST_MAX_CHARS = 1200         # the package's default for the analyst's digest
DIGEST_RETRY_SECONDS = 60.0     # a digest that could not be read is not tried again sooner
TOOL_LINE_MAX = 160             # characters of a tool's one line in the analyst's list
OPEN_TASK_LAST_MAX = 600        # characters of the last message to the designer shown to the analyst

# What one report may carry, so a 4 MB execution result (the route takes up to 4 MiB) cannot
# reach a model or the session whole. The supervisor's one message is at most MESSAGE_KEEP
# characters in all - the one loop's EXECUTION_KEEP - and each piece of it is cut first:
RESULT_KEEP = 2000              # the JSON text of an execution's ``result``
TEXT_MAX = 1000                 # any other text: error, message, hint; a role's report, fix, question, note
NAME_MAX = 200                  # a tool, validator, check or evidence name
LIST_MAX = 20                   # items of a list (warnings, preconditions_failed, facts_needed)
ITEM_MAX = 300                  # characters of one list item, and of one check's detail
CHECKS_MAX = 30                 # validator checks the supervisor sees (the failed ones first)
CHECKS_SHOWN = 20               # validator checks a report of the host lists
MESSAGE_KEEP = 16_384           # the supervisor's whole message (= chat.EXECUTION_KEEP)


def clip(value, limit: int = TEXT_MAX) -> str:
    """``value`` as text (JSON when it is not text), at most ``limit`` characters and a note of
    how many more there were."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (+{len(text) - limit} characters)"


def clip_list(values, limit: int = LIST_MAX, each: int = ITEM_MAX) -> list[str]:
    """The first ``limit`` items, each clipped to ``each``, then ``(+N more)``."""
    if not isinstance(values, list):
        return [clip(values, each)]
    items = [clip(v, each) for v in values[:limit]]
    if len(values) > limit:
        items.append(f"(+{len(values) - limit} more)")
    return items

# The tools of each role and route, by name, in the order of TOOL_DEFINITIONS.
QUERY_TOOLS = ("get_project_snapshot", "query", "ask_designer")
ALL_TOOLS = tuple(t["function"]["name"] for t in TOOL_DEFINITIONS)
ROUTE_TOOLS = {QUERY: QUERY_TOOLS, TOOL: ALL_TOOLS}
SUPERVISOR_TOOLS = ("query",)


def definitions(names: tuple[str, ...]) -> list[dict]:
    """The function definitions of ``names``, as ``TOOL_DEFINITIONS`` has them (same objects,
    same order): a role and route always send the same bytes."""
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in names]


ROUTE_DEFINITIONS = {route: definitions(names) for route, names in ROUTE_TOOLS.items()}
SUPERVISOR_DEFINITIONS = definitions(SUPERVISOR_TOOLS)

# The analyst's one host line (after the package's text, a paragraph of its own: the text
# ends with its never-list): where a tool that does not exist yet is made.
ANALYST_WORKBENCH_LINE = (
    "With route none, end `note` with exactly this Markdown link to where a new tool is tried "
    f"and saved: {WORKBENCH_LINK}."
)

REPAIR_INSTRUCTION = (
    "Your reply was not the one JSON object your instructions ask for. Reply again with exactly "
    "that JSON object and nothing else."
)

# What the host adds to a role's user message, each on its own line: the designer did not write it.
FROM_HOST = "(from the host; the designer did not write this)"
FORM_MARKER = "(checked by the host against the choices on the form)"
ANSWERS_PREFIX = f"Designer's form answers {FORM_MARKER}, with other: true for the designer's own words:"
SUPERVISOR_PREFIX = f"Execution to report {FROM_HOST}:"
ROUTING_PREFIX = f"Analyst's routing {FROM_HOST}:"
VERDICT_PREFIX = f"Supervisor's verdict on the last execution {FROM_HOST}:"
RETRY_PREFIX = f"Sent back by the supervisor {FROM_HOST}:"


class RoleOutputError(ValueError):
    """A role's reply is not the JSON object its text asks for; the message says why (for the log)."""


# -- the analyst -----------------------------------------------------------------------------

@dataclass
class Routing:
    route: str
    tool: str | None = None
    facts_needed: list[str] = field(default_factory=list)
    note: str = ""

    def handoff(self) -> dict:
        """What the planner gets of it: the four fields, ``tool`` only when there is one, each
        cut to its cap (``TEXT_MAX``, ``LIST_MAX`` items of ``ITEM_MAX``)."""
        out: dict = {"route": self.route}
        if self.tool:
            out["tool"] = clip(self.tool, NAME_MAX)
        out["facts_needed"] = clip_list(list(self.facts_needed))
        out["note"] = clip(self.note)
        return out


# The analyst could not be read: the whole planner, as without the split (the safe default).
FALLBACK_ROUTING = Routing(route=TOOL, tool=None, facts_needed=[], note="")


def extract_object(text: str) -> dict:
    """The JSON object of a role's reply: the reply itself, or the first object in it (a model
    may wrap it in a Markdown fence or add a word); RoleOutputError when there is none."""
    stripped = (text or "").strip()
    if not stripped:
        raise RoleOutputError("empty reply")
    try:
        value = json.loads(stripped)
    except ValueError:
        start = stripped.find("{")
        if start < 0:
            raise RoleOutputError("no JSON object in the reply") from None
        try:
            value, _ = json.JSONDecoder().raw_decode(stripped[start:])
        except ValueError:
            raise RoleOutputError("the reply's JSON does not parse") from None
    if not isinstance(value, dict):
        raise RoleOutputError("the reply is not a JSON object")
    return value


def _text(value, name: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise RoleOutputError(f"{name} is missing")
        return None
    if not isinstance(value, str):
        raise RoleOutputError(f"{name} is not text")
    value = value.strip()
    if required and not value:
        raise RoleOutputError(f"{name} is empty")
    return value or None


def parse_routing(text: str, tool_names) -> Routing:
    """The analyst's ``{route, tool?, facts_needed, note}``. Refused: no object, a route that is
    not one of ``ROUTES``, ``facts_needed`` that is not a list of text, a ``note`` that is not
    text (or empty with chat / none, where it is the reply), a ``tool`` with route tool that is
    not a name from the list. ``tool`` with another route is dropped; ``facts_needed`` left out
    is empty."""
    data = extract_object(text)
    route = data.get("route")
    if route not in ROUTES:
        raise RoleOutputError("route is not chat, query, tool or none")
    facts = data.get("facts_needed", [])
    if facts is None:
        facts = []
    if not isinstance(facts, list) or not all(isinstance(f, str) for f in facts):
        raise RoleOutputError("facts_needed is not a list of text")
    note = _text(data.get("note"), "note", required=route in ANSWERED_BY_ANALYST) or ""
    tool = None
    if route == TOOL:
        tool = _text(data.get("tool"), "tool", required=False)
        if tool is not None and tool not in set(tool_names):
            raise RoleOutputError("tool is not a name from the list")
    return Routing(route=route, tool=tool, facts_needed=[f.strip() for f in facts if f.strip()], note=note)


def analyst_reply(routing: Routing) -> str:
    """What the designer sees of a chat or none routing: the note; with none, the workbench
    link when the note does not carry it."""
    if routing.route == NONE and WORKBENCH_PATH not in routing.note:
        return f"{routing.note}\n\n{WORKBENCH_LINK}"
    return routing.note


def tool_lines(packs: list[dict]) -> str:
    """The analyst's tool list: one line per tool, ``- <name>: <first line of its description>``
    (at most ``TOOL_LINE_MAX`` characters), sorted by name - the same bytes while the tools are
    the same (nothing that changes on use, such as the run count)."""
    lines = []
    for pack in sorted((p for p in packs if isinstance(p, dict) and isinstance(p.get("name"), str)),
                       key=lambda p: p["name"]):
        description = pack.get("description") if isinstance(pack.get("description"), str) else ""
        first = next((line.strip() for line in description.splitlines() if line.strip()), "")
        if len(first) > TOOL_LINE_MAX:
            first = first[:TOOL_LINE_MAX - 3].rstrip() + "..."
        lines.append(f"- {pack['name']}: {first}" if first else f"- {pack['name']}")
    return "\n".join(lines) if lines else "(no tools)"


def open_task_block(task: RoleTask | None) -> str:
    """The task the analyst's message may continue (an answer to a question the planner or
    the supervisor asked): its route and tool and the last message the designer got."""
    if task is None:
        return ""
    head = f"Open task {FROM_HOST}: route {task.route}" + (f", tool {task.tool}" if task.tool else "")
    if not task.last:
        return head + "."
    last = task.last if len(task.last) <= OPEN_TASK_LAST_MAX else task.last[:OPEN_TASK_LAST_MAX - 3] + "..."
    return f"{head}. The last message to the designer:\n{last}"


def analyst_message(tools: str, digest: str, task: RoleTask | None, text: str) -> str:
    """The analyst's one user message: what changes least first (tools, digest), then the open
    task and the designer's message (with the selections block)."""
    parts = [f"Tools:\n{tools}", f"Digest of the open model:\n{digest}"]
    block = open_task_block(task)
    if block:
        parts.append(block)
    parts.append(f"Designer's message:\n{text}")
    return "\n\n".join(parts)


# -- the planner -----------------------------------------------------------------------------

def planner_message(text: str, routing: Routing, verdict_note: dict | None = None) -> str:
    """The planner's user message of a turn: the designer's message (with the selections
    block), the last execution's verdict when the planner has not seen it yet, then the
    analyst's routing - one JSON line each, every field cut to its cap."""
    parts = [text]
    if verdict_note is not None:
        parts.append(f"{VERDICT_PREFIX} {json.dumps(verdict_note, ensure_ascii=False)}")
    parts.append(f"{ROUTING_PREFIX} {json.dumps(routing.handoff(), ensure_ascii=False)}")
    return "\n\n".join(parts)


def retry_message(verdict: Verdict, retry: int) -> str:
    """The planner's user message when the supervisor sends the task back (report and fix cut
    to ``TEXT_MAX``)."""
    note = {"verdict": RETRY, "report": clip(verdict.report), "fix": clip(verdict.fix or ""), "retry": retry,
            "retries_left": MAX_RETRIES - retry}
    return f"{RETRY_PREFIX} {json.dumps(note, ensure_ascii=False)}"


# -- the supervisor --------------------------------------------------------------------------

@dataclass
class Verdict:
    verdict: str
    report: str
    fix: str | None = None
    question: str | None = None
    forced: str | None = None          # why the host changed the supervisor's verdict, if it did

    def reply(self) -> str:
        """What the designer sees: the report, and the question with ask."""
        if self.verdict == ASK and self.question:
            return f"{self.report}\n\n{self.question}"
        return self.report

    def note(self) -> dict:
        """What the planner learns of it on its next turn (``pending_verdict``): each text cut
        to ``TEXT_MAX``."""
        out = {"verdict": self.verdict, "report": clip(self.report)}
        if self.question:
            out["question"] = clip(self.question)
        return out


def parse_verdict(text: str) -> Verdict:
    """The supervisor's ``{verdict, report, fix?, question?}``. Refused: no object, a verdict
    that is not done / retry / ask, a ``report`` that is not non-empty text, retry without a
    ``fix``. ``fix`` is kept only with retry and ``question`` only with ask."""
    data = extract_object(text)
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise RoleOutputError("verdict is not done, retry or ask")
    report = _text(data.get("report"), "report", required=True)
    fix = _text(data.get("fix"), "fix", required=verdict == RETRY) if verdict == RETRY else None
    question = _text(data.get("question"), "question", required=False) if verdict == ASK else None
    return Verdict(verdict=verdict, report=report, fix=fix, question=question)


def validation_of(execution: dict) -> dict | None:
    value = execution.get("validation")
    return value if isinstance(value, dict) else None


def validator_failed(execution: dict) -> bool:
    """The package's verdict: ``error: validation_failed``, or a validation that did not pass."""
    validation = validation_of(execution)
    return execution.get("error") == "validation_failed" or (
        validation is not None and validation.get("passed") is False)


def is_refused(execution: dict) -> bool:
    """``refused: true``: the pack refused the input in Revit and nothing was kept (revit-bridge
    0.9.1 and later; 0.9.0 does not send it)."""
    return execution.get("refused") is True


# The package's error codes of a run it stopped before anything changed in the model
# (``revit_bridge.execution``): a precondition failed, the validator could not take its first
# sample, the sandbox blocked the code.
STOPPED_BEFORE_RUN = {
    "preconditions_failed": ("a precondition failed", "前置条件未满足"),
    "validator_before_failed": ("the validator could not take its first sample", "校验器未能读取执行前的数据"),
    "blocked": ("the code was blocked", "代码被阻止"),
}


def refusal_kind(execution: dict, *, chinese: bool = False) -> str | None:
    """Why a failed run is a refusal by nature - not a value the planner could change - read from
    the package's fields, never from its wording; None when it is not one:
    ``refused: true`` (0.9.1: the pack refused the input in Revit); ``preconditions_failed``
    listing any; ``error`` one of the codes of ``STOPPED_BEFORE_RUN``; or a failed run with no
    ``evidence_id`` - nothing reached the ledger: the package refused it before dispatch (its
    parameter check, an unknown or unhealthy tool) or Revit could not be reached."""
    if is_refused(execution):
        return "Revit 工具拒绝了输入，修改未保留" if chinese else "the tool refused the input in Revit; nothing was kept"
    if execution.get("success") is True:
        return None
    failed = execution.get("preconditions_failed")
    if isinstance(failed, list) and failed:
        return STOPPED_BEFORE_RUN["preconditions_failed"][int(chinese)]
    error = execution.get("error")
    if isinstance(error, str) and error in STOPPED_BEFORE_RUN:
        return STOPPED_BEFORE_RUN[error][int(chinese)]
    if not execution.get("evidence_id") and validation_of(execution) is None:
        return "没有执行记录" if chinese else "nothing was run or recorded"
    return None


def checks_of(execution: dict) -> list[dict]:
    validation = validation_of(execution) or {}
    checks = validation.get("checks")
    return [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []


def failed_first(checks: list[dict]) -> list[dict]:
    """The checks that did not pass, then the rest, each group in its own order."""
    return [c for c in checks if c.get("passed") is not True] + [c for c in checks if c.get("passed") is True]


def _check_line(check: dict, chinese: bool = False) -> str:
    """One validator check as the host shows it: ``- <name> (passed|failed): <detail>``, the
    detail as the package wrote it (cut to ``ITEM_MAX``; none: no colon)."""
    state = "passed" if check.get("passed") is True else "failed"
    if chinese:
        state = "通过" if check.get("passed") is True else "未通过"
    head = f"- {clip(check.get('name', '?'), NAME_MAX)} ({state})"
    detail = check.get("detail")
    return f"{head}: {clip(detail, ITEM_MAX)}" if isinstance(detail, str) and detail else head


def _check_lines(checks: list[dict], chinese: bool = False) -> list[str]:
    """At most ``CHECKS_SHOWN`` check lines, then ``(+N more checks)``."""
    lines = [_check_line(c, chinese) for c in checks[:CHECKS_SHOWN]]
    if len(checks) > CHECKS_SHOWN:
        more = len(checks) - CHECKS_SHOWN
        lines.append(f"（另有 {more} 项检查）" if chinese else f"(+{more} more checks)")
    return lines


def _error_text(execution: dict) -> str:
    """``error`` and ``message`` on one line, cut to ``TEXT_MAX``."""
    error = execution.get("error")
    text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    message = execution.get("message")
    if isinstance(message, str) and message and message != text:
        text = f"{text}: {message}"
    return clip(text)


FORCED_VALIDATOR = "validator_failed"
FORCED_FAILED = "execution_failed"
FORCED_REFUSED = "refused"
FORCED_SUCCEEDED = "succeeded"
FORCED_RETRIES = "retries_spent"
DEFAULT_QUESTION = "How would you like to go on?"


def enforce(verdict: Verdict, execution: dict, retries: int, *, chinese: bool = False) -> Verdict:
    """The supervisor's verdict held to the package's, read from the raw execution result.
    A refusal by nature (``refusal_kind``) is never sent back or called done; ``done`` needs
    Revit's success and a validator that did not fail; a run that succeeded with a validator
    that did not fail is not sent back (the designer decides); a task already sent back
    ``MAX_RETRIES`` times is not sent back again. Each becomes ``ask``, with the host's own
    report built from the result - every piece of it cut to its cap."""
    succeeded = execution.get("success") is True
    failed_validator = validator_failed(execution)
    kind = refusal_kind(execution, chinese=chinese)
    default_question = "接下来要怎么做？" if chinese else DEFAULT_QUESTION
    if verdict.verdict == DONE and failed_validator:
        checks = checks_of(execution)
        failed = [c for c in checks if c.get("passed") is not True] or checks
        lines = "\n".join(_check_lines(failed, chinese)) or ("未列出检查项。" if chinese else "- (the result lists no checks)")
        return Verdict(ASK, ("校验未通过，任务尚未完成。\n" if chinese else "The validator did not pass, so this is not done:\n") + lines,
                       question="需要调整什么？" if chinese else "What should change?", forced=FORCED_VALIDATOR)
    if kind is not None and verdict.verdict != ASK:
        report = f"本次执行未重试。{kind}。{_error_text(execution)}" if chinese else f"This run is not sent back: {kind}. {_error_text(execution)}"
        return Verdict(ASK, report,
                       question=default_question, forced=FORCED_REFUSED)
    if verdict.verdict == DONE and not succeeded:
        report = "Revit 未报告执行成功，任务尚未完成。" if chinese else "Revit did not report success, so this is not done: "
        return Verdict(ASK, report + _error_text(execution),
                       question=default_question, forced=FORCED_FAILED)
    if verdict.verdict == RETRY and succeeded and not failed_validator:
        note = ("Revit 已报告成功，校验未发现失败。请决定是否需要调整。" if chinese else
                "Revit reported success and the validator did not fail, so the host does not send this run back; you decide whether anything should change.")
        return Verdict(ASK, f"{clip(verdict.report)}\n\n{note}",
                       question=(f"需要调整吗？监督员建议：{clip(verdict.fix)}" if chinese else
                                 f"Should anything change? (the supervisor suggested: {clip(verdict.fix)})")
                       if verdict.fix else default_question, forced=FORCED_SUCCEEDED)
    if verdict.verdict == RETRY and retries >= MAX_RETRIES:
        how = ("，仍未成功" if chinese else " and still did not succeed") if not succeeded else ""
        note = (f"任务已退回 {MAX_RETRIES} 次{how}，请决定下一步。" if chinese else
                f"The task was sent back {MAX_RETRIES} times{how}; the host does not send it back again.")
        return Verdict(ASK, f"{clip(verdict.report)}\n\n{note}",
                       question=(f"需要调整什么？上次建议：{clip(verdict.fix)}" if chinese else
                                 f"What should change? (last suggestion: {clip(verdict.fix)})") if verdict.fix
                       else default_question, forced=FORCED_RETRIES)
    return verdict


def fallback_verdict(execution: dict, *, chinese: bool = False) -> Verdict:
    """The supervisor's reply could not be read: the host reports the raw outcome and asks."""
    success = json.dumps(execution.get("success"))
    lines = [f"执行成功：{success}" if chinese else f"success: {success}"]
    if execution.get("error"):
        lines.append(("错误：" if chinese else "error: ") + _error_text(execution))
    if is_refused(execution):
        lines.append("工具已拒绝执行" if chinese else "refused: true")
    validation = validation_of(execution)
    if validation is not None:
        name = clip(validation.get("validator", "?"), NAME_MAX)
        passed = validation.get("passed") is True
        lines.append(f"校验器 {name}：{'通过' if passed else '未通过'}" if chinese else
                     f"validator {name}: {'passed' if passed else 'did not pass'}")
        lines += _check_lines(checks_of(execution), chinese)
    if execution.get("evidence_id"):
        lines.append(("证据：" if chinese else "evidence: ") + clip(execution['evidence_id'], NAME_MAX))
    heading = ("未能读取监督员回复。以下为收到的执行结果。\n" if chinese else
               "The execution result, as the host received it (the supervisor's reply could not be read):\n")
    return Verdict(ASK, heading + "\n".join(lines), question="接下来要怎么做？" if chinese else DEFAULT_QUESTION,
                   forced="supervisor_unreadable")


def execution_summary(execution: dict) -> dict:
    """What the supervisor gets of an execution result, each field cut to its cap: ``success``
    and ``refused`` as they are; ``tool`` and ``evidence_id`` (``NAME_MAX``); ``error``,
    ``message``, ``hint`` (``TEXT_MAX``); ``warnings`` and ``preconditions_failed`` (``LIST_MAX``
    items of ``ITEM_MAX``); ``result`` as it is up to ``RESULT_KEEP`` characters of JSON, else its
    head as text; the validation without its before / after samples, at most ``CHECKS_MAX``
    checks (the failed ones first), each name and detail cut, and ``checks_more`` for the rest."""
    summary: dict = {}
    for key in ("success", "refused"):
        if key in execution:
            value = execution[key]
            summary[key] = value if isinstance(value, bool) or value is None else clip(value, NAME_MAX)
    for key, limit in (("tool", NAME_MAX), ("error", TEXT_MAX), ("message", TEXT_MAX),
                       ("evidence_id", NAME_MAX), ("hint", TEXT_MAX)):
        if key in execution:
            summary[key] = None if execution[key] is None else clip(execution[key], limit)
    for key in ("warnings", "preconditions_failed"):
        if key in execution:
            summary[key] = clip_list(execution[key])
    if "result" in execution:
        text = json.dumps(execution["result"], ensure_ascii=False)
        summary["result"] = execution["result"] if len(text) <= RESULT_KEEP else clip(text, RESULT_KEEP)
    validation = validation_of(execution)
    if validation is not None:
        shown: dict = {}
        if "validator" in validation:
            shown["validator"] = clip(validation["validator"], NAME_MAX)
        if "passed" in validation:
            shown["passed"] = validation["passed"] if isinstance(validation["passed"], bool) else None
        checks = failed_first(checks_of(execution))
        shown["checks"] = [{"name": clip(c.get("name", "?"), NAME_MAX), "passed": c.get("passed") is True,
                            "detail": clip(c.get("detail", ""), ITEM_MAX)} for c in checks[:CHECKS_MAX]]
        if len(checks) > CHECKS_MAX:
            shown["checks_more"] = len(checks) - CHECKS_MAX
        summary["validation"] = shown
    return summary


def step_result(execution: dict) -> dict:
    """Keep numeric element identifiers beside a clipped result, independent of JSON field order."""
    summary = execution_summary(execution)
    raw = execution.get("result")
    if isinstance(raw, dict) and isinstance(summary.get("result"), str):
        ids = {}
        remaining = 256
        omitted = 0
        for key, value in raw.items():
            if not key.casefold().endswith(("id", "ids")):
                continue
            items = value if isinstance(value, list) else [value]
            numeric = [v for v in items if type(v) is int or (isinstance(v, str) and len(v) <= 20 and v.isdecimal())]
            keep = numeric[:remaining]
            if keep:
                ids[key] = keep if isinstance(value, list) else keep[0]
                remaining -= len(keep)
            omitted += len(numeric) - len(keep)
        if ids:
            summary["element_ids"] = ids
        if omitted:
            summary["more_element_ids"] = omitted
    return summary


def supervisor_message(spec: dict | None, execution: dict, retries: int, *, confirmed: bool = True) -> str:
    """The supervisor's one user message: the spec (``confirmed_spec`` when the page confirmed it
    in this session, else ``proposed_spec``, the last one the planner proposed), the execution
    summary and how often this task was sent back already, as one JSON object after a fixed
    line - at most ``MESSAGE_KEEP`` characters: a spec that would not fit goes as the head of
    its JSON text."""
    key = "confirmed_spec" if confirmed else "proposed_spec"
    payload = {key: spec, "execution": execution_summary(execution),
               "sent_back_so_far": retries, "max_sent_back": MAX_RETRIES}
    text = json.dumps(payload, ensure_ascii=False)
    if len(SUPERVISOR_PREFIX) + 1 + len(text) > MESSAGE_KEEP and spec is not None:
        without = len(json.dumps({**payload, key: ""}, ensure_ascii=False))
        room = max(0, MESSAGE_KEEP - len(SUPERVISOR_PREFIX) - 1 - without - 64)
        payload[key] = clip(json.dumps(spec, ensure_ascii=False), room)
        text = json.dumps(payload, ensure_ascii=False)
    message = f"{SUPERVISOR_PREFIX}\n{text}"
    return message if len(message) <= MESSAGE_KEEP else clip(message, MESSAGE_KEEP - 40)


# -- the session's task ----------------------------------------------------------------------

@dataclass
class RoleTask:
    """The task a session's next message may continue: the tool a ``tool`` routing named, how
    often the supervisor sent it back since the designer last wrote, and the last message the
    designer got from it (planner or supervisor)."""
    route: str
    tool: str | None = None
    retries: int = 0
    last: str = ""


def next_task(current: RoleTask | None, routing: Routing) -> RoleTask | None:
    """The open task after a routing. Only a ``tool`` routing opens or continues one: with the
    open task's tool (or none named) it continues it, and the designer's message is a new
    attempt (no send-backs counted); naming another tool closes it and opens a new one.
    ``query``, ``chat`` and ``none`` leave the open task as it is and open none. (``done``
    closes it: ``backend.api.chat``.)"""
    if routing.route != TOOL:
        return current
    if current is None or (routing.tool and current.tool and routing.tool != current.tool):
        return RoleTask(route=TOOL, tool=routing.tool)
    current.tool = routing.tool or current.tool
    current.retries = 0
    return current
