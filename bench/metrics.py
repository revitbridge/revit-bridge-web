"""Token accounting and explicit benchmark outcome checks."""
from __future__ import annotations

TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")


def sum_usage(events: list[dict]) -> dict:
    """Missing telemetry is unknown; an explicit zero remains zero."""
    totals = {}
    for field in (*TOKEN_FIELDS, "ms"):
        values = [event.get(field) for event in events]
        totals[field] = (
            sum(values)
            if values and all(type(value) in (int, float) and value >= 0 for value in values)
            else None
        )
    return {**totals, "model_calls": len(events) if events else None}


def merge_tools(events: list[dict]) -> list[dict]:
    latest = {}
    for event in events:
        # IDs may repeat across separate HTTP turns, but not within one turn.
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
    expect = task.get("expect", {})
    chosen = specs[-1]["spec"] if specs else None
    action = chosen.get("action", {}) if chosen else {}
    expected_tool = expect.get("tool")
    actual_tool = action.get("tool")
    tool_correct = (
        chosen is not None and action.get("kind") == "run_tool" and actual_tool == expected_tool
        if expected_tool is not None else chosen is None
    )
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
    correct = tool_correct and params_correct
    if expected_tool is None:
        correct = correct and bool(text.strip())
    if task["kind"] == "query":
        correct = correct and any(
            event.get("status") == "ok" and event.get("name") in ("query", "snapshot", "get_snapshot")
            for event in tools
        )
    if task["kind"] == "ask":
        correct = correct and turns > 1
    return {
        "correct": correct, "tool_correct": tool_correct, "params_correct": params_correct,
        "param_checks": checks, "expected_tool": expected_tool, "actual_tool": actual_tool,
        "actual_params": actual_params,
    }


def compare_runs(before: dict, after: dict) -> list[dict]:
    old = {item["id"]: item for item in before["tasks"]}
    new = {item["id"]: item for item in after["tasks"]}
    rows = []
    for task_id in dict.fromkeys([*old, *new]):
        left, right = old.get(task_id), new.get(task_id)
        reasons = []
        if left and left["correct"] and (right is None or not right["correct"]):
            reasons.append("correctness regressed" if right else "task missing")
        if left and right and left["correct"] and right["correct"]:
            a, b = left["usage"]["input"], right["usage"]["input"]
            if a is not None and b is not None and b > a * 1.05:
                reasons.append("input tokens increased by more than 5%")
        tokens = {}
        for field in TOKEN_FIELDS:
            a = left["usage"][field] if left else None
            b = right["usage"][field] if right else None
            tokens[field] = {"before": a, "after": b, "delta": b - a if a is not None and b is not None else None}
        rows.append({"id": task_id, "before": left["correct"] if left else None,
                     "after": right["correct"] if right else None, "tokens": tokens,
                     "regression": bool(reasons), "reasons": reasons})
    return rows
