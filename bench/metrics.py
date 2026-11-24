"""Token accounting, outcome checks, repeat statistics and run comparison.

Unknown is never 0: a token count the host reported as null stays null, and any
sum, difference, median, minimum or maximum that contains an unknown is unknown.
"""
from __future__ import annotations

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")
USAGE_FIELDS = (*TOKEN_FIELDS, "ms")
# What a role's line holds: model calls, the four token counts and the model time.
ROLE_FIELDS = ("calls", *USAGE_FIELDS)
# The per-task numbers a repeat summary reports.
TASK_FIELDS = ("calls", *USAGE_FIELDS, "tool_calls", "elapsed_ms")
# Read-only packs: proposing one of them answers a query task correctly (planning ruling). These
# are the built-in packs of revit-bridge 0.9.1 that only read the model (their count_delta
# validator expects no change).
READ_ONLY_PACKS = ("query_levels", "query_model_stats", "count_by_level", "list_types", "elements_in_link")
READ_TOOLS = ("query", "get_project_snapshot")
# compare: input tokens may grow by this factor before a correct task counts as worse.
INPUT_TOLERANCE = 1.05


def is_count(value: object) -> bool:
    return type(value) in (int, float) and value >= 0


def total(values: list) -> int | float | None:
    """The sum, or None when there is nothing to add or any value is unknown."""
    if not values or not all(is_count(value) for value in values):
        return None
    return sum(values)


def sum_usage(events: list[dict]) -> dict:
    """Totals of ``usage`` events. No events at all is unknown, not zero; an explicit 0 stays 0."""
    return {"calls": len(events) if events else None,
            **{field: total([event.get(field) for event in events]) for field in USAGE_FIELDS}}


def role_usage(usage_events: list[dict], tool_events: list[dict]) -> dict:
    """Per role (in the order the roles appeared): calls, tokens and ms from the ``usage``
    events, plus the role's tool calls."""
    roles = dict.fromkeys(event["role"] for event in [*usage_events, *tool_events]
                          if isinstance(event.get("role"), str))
    return {role: {**sum_usage([event for event in usage_events if event.get("role") == role]),
                   "tool_calls": len([event for event in tool_events if event.get("role") == role])}
            for role in roles}


def models_by_role(events: list[dict]) -> dict:
    """The model names each role used, from ``stage`` and ``usage`` events."""
    seen: dict[str, list[str]] = {}
    for event in events:
        if event.get("type") in ("stage", "usage") and isinstance(event.get("role"), str):
            names = seen.setdefault(event["role"], [])
            model = event.get("model")
            if isinstance(model, str) and model not in names:
                names.append(model)
    return seen


def merge_tools(events: list[dict]) -> list[dict]:
    latest = {}
    for event in events:
        # The host numbers tool events per session, but key by request as well: the
        # later event of an id is its final state.
        latest[(event["request"], event["id"])] = event
    return list(latest.values())


def accepted(event: dict) -> bool:
    return (
        event.get("type") == "spec"
        and isinstance(event.get("spec"), dict)
        and event.get("errors") == []
        and isinstance(event.get("reconcile"), dict)
        and event["reconcile"].get("ready") is True
    )


def equal_value(actual: object, expected: object) -> bool:
    if type(actual) in (int, float) and type(expected) in (int, float):
        return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            equal_value(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            equal_value(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def evaluate(task: dict, specs: list[dict], tools: list[dict], text: str, turns: int) -> dict:
    """Score one task from what the host streamed.

    - tool (and ask with expect.tool): the last accepted spec runs expect.tool and every
      listed parameter equals the expected value (numbers as numbers, no units: the task
      file writes millimetres).
    - query: no accepted spec, a reply, and a successful query or get_project_snapshot
      call; or an accepted spec of a read-only pack (READ_ONLY_PACKS).
    - chat, none: no accepted spec and a reply.
    - ask: also needs a second user message (the model asked before acting).
    """
    expect = task.get("expect", {})
    chosen = specs[-1]["spec"] if specs else None
    action = chosen.get("action", {}) if chosen else {}
    expected_tool = expect.get("tool")
    actual_tool = action.get("tool")
    actual_params = {
        item["name"]: item.get("value")
        for item in (chosen.get("parameters", []) if chosen else [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    checks = {
        name: name in actual_params and equal_value(actual_params[name], value)
        for name, value in expect.get("params", {}).items()
    }
    params_correct = all(checks.values())
    replied = bool(text.strip())
    if expected_tool is not None:
        tool_correct = chosen is not None and action.get("kind") == "run_tool" and actual_tool == expected_tool
        correct = tool_correct and params_correct
    elif task["kind"] == "query" and chosen is not None:
        tool_correct = action.get("kind") == "run_tool" and actual_tool in READ_ONLY_PACKS
        correct = tool_correct and params_correct
    else:
        tool_correct = chosen is None
        correct = tool_correct and params_correct and replied
        if task["kind"] == "query":
            correct = correct and any(
                event.get("status") == "ok" and event.get("name") in READ_TOOLS for event in tools
            )
    if task["kind"] == "ask":
        correct = correct and turns > 1
    return {
        "correct": correct, "tool_correct": tool_correct, "params_correct": params_correct,
        "param_checks": checks, "expected_tool": expected_tool, "actual_tool": actual_tool,
        "actual_params": actual_params,
    }


# -- repeats ---------------------------------------------------------------------------

def stat(values: list) -> dict:
    """Median, minimum and maximum; all three unknown when any value is unknown."""
    if not values or not all(is_count(value) for value in values):
        return {"median": None, "min": None, "max": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    if isinstance(median, float) and median.is_integer() and all(type(v) is int for v in ordered):
        median = int(median)
    return {"median": median, "min": ordered[0], "max": ordered[-1]}


def run_totals(tasks: list[dict]) -> tuple[dict, dict]:
    """One repeat's totals over every ``usage`` event it received (failed tasks included:
    those calls happened), overall and per role. This is what the host's run label holds."""
    usage_events = [event for task in tasks for event in task.get("events", []) if event["type"] == "usage"]
    tool_events = [event for task in tasks for event in task.get("tools", [])]
    return sum_usage(usage_events), role_usage(usage_events, tool_events)


def task_values(result: dict) -> dict | None:
    """The numbers of one task in one repeat for the summary; None (every number unknown)
    when the task failed - a flow that broke off has no comparable cost."""
    if result.get("error"):
        return None
    return {"calls": result["usage"]["calls"], **{field: result["usage"][field] for field in USAGE_FIELDS},
            "tool_calls": result["tool_calls"], "elapsed_ms": result["elapsed_ms"]}


def role_values(result: dict, role: str) -> dict | None:
    """A role's numbers in one repeat. A completed task whose host reported usage but none
    for this role made no call in that role: zero calls, zero tokens. Failed, or no usage
    reported at all: unknown."""
    if result.get("error") or result["usage"]["calls"] is None:
        return None
    found = result["by_role"].get(role)
    if found is None:
        return {**dict.fromkeys(ROLE_FIELDS, 0), "tool_calls": 0}
    return found


def summarize(runs: list[dict]) -> dict:
    """Per task over the repeats: how many ran, were correct, failed; median, min and max of
    each number, overall and per role; and the same statistics of the repeats' totals."""
    order = dict.fromkeys(task["id"] for run in runs for task in run["tasks"])
    tasks = []
    for task_id in order:
        results = [task for run in runs for task in run["tasks"] if task["id"] == task_id]
        first = results[0]
        scored = [result for result in results if not result.get("skipped")]
        entry = {"id": task_id, "kind": first["kind"], "requires": first.get("requires", []),
                 "skipped": first.get("skipped") if not scored else None,
                 "runs": len(scored), "correct": sum(1 for result in scored if result["correct"]),
                 "failed": sum(1 for result in scored if result.get("error")),
                 "errors": [result["error"] for result in scored if result.get("error")]}
        if scored:
            values = [task_values(result) for result in scored]
            entry["metrics"] = {field: stat([None if value is None else value[field] for value in values])
                                for field in TASK_FIELDS}
            roles = dict.fromkeys(role for result in scored for role in result.get("by_role", {}))
            entry["by_role"] = {}
            for role in roles:
                per_run = [role_values(result, role) for result in scored]
                entry["by_role"][role] = {field: stat([None if value is None else value[field] for value in per_run])
                                          for field in (*ROLE_FIELDS, "tool_calls")}
        tasks.append(entry)
    roles = dict.fromkeys(role for run in runs for role in run["by_role"])
    return {
        "tasks": tasks,
        "correct": stat([sum(1 for task in run["tasks"] if task.get("correct")) for run in runs]),
        "scored": stat([sum(1 for task in run["tasks"] if not task.get("skipped")) for run in runs]),
        "totals": {field: stat([run["totals"][field] for run in runs]) for field in ROLE_FIELDS},
        "by_role": {role: {field: stat([run_role_value(run, role, field) for run in runs])
                           for field in ROLE_FIELDS}
                    for role in roles},
    }


def run_role_value(run: dict, role: str, field: str) -> int | float | None:
    """A repeat's total for one role: zero when the host reported usage in that repeat but
    none for the role, unknown when it reported none at all."""
    if role in run["by_role"]:
        return run["by_role"][role][field]
    return 0 if run["totals"]["calls"] is not None else None


# -- compare ---------------------------------------------------------------------------

def difference(before: object, after: object) -> int | float | None:
    return after - before if is_count(before) and is_count(after) else None


def compare_runs(before: dict, after: dict) -> list[dict]:
    """Per task: ``regression`` when fewer repeats are correct than before (or a task that
    was correct is missing), or when the task is as often correct as before and its median
    input tokens grew more than 5%; ``uncomparable`` when that token check meets an
    unknown median (either side, or both), or a task that ran before is skipped now. Both
    wholly wrong: more tokens are not a regression."""
    old = {task["id"]: task for task in before["summary"]["tasks"]}
    new = {task["id"]: task for task in after["summary"]["tasks"]}
    rows = []
    for task_id in dict.fromkeys([*old, *new]):
        left, right = old.get(task_id), new.get(task_id)
        reasons: list[str] = []
        status = "ok"
        ran_before = bool(left and not left["skipped"] and left["runs"])
        ran_after = bool(right and not right["skipped"] and right["runs"])
        if not ran_before and not ran_after:
            status = "skipped"
        elif not ran_before:
            status = "new"
        elif right is None:
            if left["correct"]:
                status, reasons = "regression", ["task missing"]
        elif not ran_after:
            status, reasons = "uncomparable", ["skipped after: " + str(right["skipped"])]
        else:
            rate_before = left["correct"] / left["runs"]
            rate_after = right["correct"] / right["runs"]
            if rate_after < rate_before:
                status = "regression"
                reasons.append(f"correctness regressed ({left['correct']}/{left['runs']} -> "
                               f"{right['correct']}/{right['runs']})")
            elif rate_after == rate_before and rate_before > 0:
                a = left["metrics"]["input"]["median"]
                b = right["metrics"]["input"]["median"]
                if not is_count(a) or not is_count(b):
                    side = "both" if not is_count(a) and not is_count(b) else ("before" if not is_count(a) else "after")
                    status = "uncomparable"
                    reasons.append(f"input tokens unknown ({side})")
                elif b > a * INPUT_TOLERANCE:
                    status = "regression"
                    reasons.append("input tokens increased by more than 5%")
        tokens = {}
        for field in (*TOKEN_FIELDS, "calls"):
            a = left["metrics"][field]["median"] if ran_before else None
            b = right["metrics"][field]["median"] if ran_after else None
            tokens[field] = {"before": a, "after": b, "delta": difference(a, b)}
        rows.append({"id": task_id,
                     "before": {key: left[key] for key in ("runs", "correct", "failed", "skipped")} if left else None,
                     "after": {key: right[key] for key in ("runs", "correct", "failed", "skipped")} if right else None,
                     "status": status, "reasons": reasons, "tokens": tokens})
    return rows


def compare_exit_code(rows: list[dict]) -> int:
    """1: some task got worse; 3: nothing got worse but some task could not be compared; 0: neither."""
    if any(row["status"] == "regression" for row in rows):
        return 1
    if any(row["status"] == "uncomparable" for row in rows):
        return 3
    return 0
