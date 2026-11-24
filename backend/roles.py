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
  ``chat.WORKBENCH_LINE``): the tools of the route (``ROUTE_TOOLS``), the session's
  conversation, and this turn's message with the analyst's routing after it
  (``planner_message``) - not the analyst's prompt, digest or tool list.
- **supervisor** (``host_instructions(role="supervisor", code=False)``): ``query`` only.
  One user message: the confirmed spec, a summary of the execution result and the
  validator's per-check verdicts (``supervisor_message``) - not the planner's
  transcript. It answers ONE JSON object ``{verdict, report, fix?, question?}``
  (``parse_verdict``); the host holds it to the package's verdict (``enforce``).

A role's output that is not that JSON gets one repair call (``REPAIR_INSTRUCTION``);
after that the host falls back (``FALLBACK_ROUTING``: route ``tool`` with the whole
planner; ``fallback_verdict``: the host reports the raw outcome and asks). A task is
sent back to the planner at most ``MAX_RETRIES`` times.

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
TOOL_LINE_MAX = 160             # characters of a tool's one line in the analyst's list
OPEN_TASK_LAST_MAX = 600        # characters of the last message to the designer shown to the analyst
RESULT_KEEP = 2000              # characters of an execution's ``result`` the supervisor sees

# The tools of each role and route, by name, in the order of TOOL_DEFINITIONS.
QUERY_TOOLS = ("get_project_snapshot", "query")
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
    f"and saved: {WORKBENCH_LINK}"
)

REPAIR_INSTRUCTION = (
    "Your reply was not the one JSON object your instructions ask for. Reply again with exactly "
    "that JSON object and nothing else."
)

# What the host adds to a role's user message, each on its own line: the designer did not write it.
FROM_HOST = "(from the host; the designer did not write this)"
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
        """What the planner gets of it: the four fields, ``tool`` only when there is one."""
        out: dict = {"route": self.route}
        if self.tool:
            out["tool"] = self.tool
        out["facts_needed"] = list(self.facts_needed)
        out["note"] = self.note
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
    analyst's routing - one JSON line each."""
    parts = [text]
    if verdict_note is not None:
        parts.append(f"{VERDICT_PREFIX} {json.dumps(verdict_note, ensure_ascii=False)}")
    parts.append(f"{ROUTING_PREFIX} {json.dumps(routing.handoff(), ensure_ascii=False)}")
    return "\n\n".join(parts)


def retry_message(verdict: Verdict, retry: int) -> str:
    """The planner's user message when the supervisor sends the task back."""
    note = {"verdict": RETRY, "report": verdict.report, "fix": verdict.fix or "", "retry": retry,
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
        """What the planner learns of it on its next turn."""
        out = {"verdict": self.verdict, "report": self.report}
        if self.question:
            out["question"] = self.question
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
    """``refused: true`` (revit-bridge 0.9.1 and later; 0.9.0 does not send it)."""
    return execution.get("refused") is True


def checks_of(execution: dict) -> list[dict]:
    validation = validation_of(execution) or {}
    checks = validation.get("checks")
    return [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []


def _check_line(check: dict) -> str:
    """One validator check as the host shows it: ``- <name> (passed|failed): <detail>``, the
    detail as the package wrote it (none: no colon)."""
    state = "passed" if check.get("passed") is True else "failed"
    head = f"- {check.get('name', '?')} ({state})"
    detail = check.get("detail")
    return f"{head}: {detail}" if isinstance(detail, str) and detail else head


def _error_text(execution: dict) -> str:
    error = execution.get("error")
    text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    message = execution.get("message")
    if isinstance(message, str) and message and message != text:
        text = f"{text}: {message}"
    return text


FORCED_VALIDATOR = "validator_failed"
FORCED_FAILED = "execution_failed"
FORCED_REFUSED = "refused"
FORCED_RETRIES = "retries_spent"
DEFAULT_QUESTION = "How would you like to go on?"


def enforce(verdict: Verdict, execution: dict, retries: int) -> Verdict:
    """The supervisor's verdict held to the package's: a refused execution is never sent back
    or called done; ``done`` needs Revit's success and a validator that did not fail; a task
    already sent back ``MAX_RETRIES`` times is not sent back again. Each becomes ``ask``, with
    the host's own report built from the result."""
    if is_refused(execution) and verdict.verdict != ASK:
        return Verdict(ASK, f"The execution was refused, nothing ran: {_error_text(execution)}",
                       question=DEFAULT_QUESTION, forced=FORCED_REFUSED)
    if verdict.verdict == DONE and validator_failed(execution):
        failed = [c for c in checks_of(execution) if c.get("passed") is not True] or checks_of(execution)
        lines = "\n".join(_check_line(c) for c in failed) or "- (the result lists no checks)"
        return Verdict(ASK, f"The validator did not pass, so this is not done:\n{lines}",
                       question="What should change?", forced=FORCED_VALIDATOR)
    if verdict.verdict == DONE and execution.get("success") is not True:
        return Verdict(ASK, f"Revit did not report success, so this is not done: {_error_text(execution)}",
                       question=DEFAULT_QUESTION, forced=FORCED_FAILED)
    if verdict.verdict == RETRY and retries >= MAX_RETRIES:
        return Verdict(ASK, f"{verdict.report}\n\nThe task was sent back {MAX_RETRIES} times and still did not "
                            f"succeed; the host does not send it back again.",
                       question=f"What should change? (last suggestion: {verdict.fix})" if verdict.fix
                       else DEFAULT_QUESTION, forced=FORCED_RETRIES)
    return verdict


def fallback_verdict(execution: dict) -> Verdict:
    """The supervisor's reply could not be read: the host reports the raw outcome and asks."""
    lines = [f"success: {json.dumps(execution.get('success'))}"]
    if execution.get("error"):
        lines.append(f"error: {_error_text(execution)}")
    if is_refused(execution):
        lines.append("refused: true")
    validation = validation_of(execution)
    if validation is not None:
        lines.append(f"validator {validation.get('validator', '?')}: "
                     f"{'passed' if validation.get('passed') is True else 'did not pass'}")
        lines += [_check_line(c) for c in checks_of(execution)]
    if execution.get("evidence_id"):
        lines.append(f"evidence: {execution['evidence_id']}")
    return Verdict(ASK, "The execution result, as the host received it (the supervisor's reply could not "
                        "be read):\n" + "\n".join(lines), question=DEFAULT_QUESTION, forced="supervisor_unreadable")


SUMMARY_FIELDS = ("success", "tool", "error", "message", "refused", "evidence_id", "warnings",
                  "preconditions_failed", "hint")


def execution_summary(execution: dict) -> dict:
    """What the supervisor gets of an execution result: the verdict fields as they are, the
    ``result`` cut to ``RESULT_KEEP`` characters of its JSON, the validation without its
    before / after samples (the per-check verdicts stay whole)."""
    summary = {key: execution[key] for key in SUMMARY_FIELDS if key in execution}
    if "result" in execution:
        text = json.dumps(execution["result"], ensure_ascii=False)
        summary["result"] = execution["result"] if len(text) <= RESULT_KEEP else (
            f"{text[:RESULT_KEEP]} [cut: {len(text)} characters in all]")
    validation = validation_of(execution)
    if validation is not None:
        summary["validation"] = {key: validation[key] for key in ("validator", "passed", "checks")
                                 if key in validation}
    return summary


def supervisor_message(spec: dict | None, execution: dict, retries: int) -> str:
    """The supervisor's one user message: the confirmed spec, the execution summary and how
    often this task was sent back already, as one JSON object after a fixed line."""
    payload = {"confirmed_spec": spec, "execution": execution_summary(execution),
               "sent_back_so_far": retries, "max_sent_back": MAX_RETRIES}
    return f"{SUPERVISOR_PREFIX}\n{json.dumps(payload, ensure_ascii=False)}"


# -- the session's task ----------------------------------------------------------------------

@dataclass
class RoleTask:
    """The task a session's next message may continue: the latest routing, how often the
    supervisor sent it back, and the last message the designer got (planner or supervisor)."""
    route: str
    tool: str | None = None
    retries: int = 0
    last: str = ""


def next_task(current: RoleTask | None, routing: Routing) -> RoleTask:
    """The task after a query or tool routing: a new one (no retries yet) when none is open or
    the analyst names another tool; else the open one with the new route."""
    if current is None or (routing.tool and current.tool and routing.tool != current.tool):
        return RoleTask(route=routing.route, tool=routing.tool)
    current.route = routing.route
    current.tool = routing.tool or current.tool
    return current
