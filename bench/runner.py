"""Drive a running host over HTTP the way the page does.

By default only ``POST /api/chat`` is called (plus the read-only ``GET /config.json``
and ``GET /api/usage/runs``): when the model's spec is accepted, a fixed execution
result goes back through ``/api/chat`` so the report step is measured too, and
nothing runs in Revit. With ``execute=True`` an accepted ``run_tool`` spec is
confirmed (``POST /api/v1/bridge/spec/confirm``) and run
(``POST /api/v1/bridge/tools/{name}/run``) - that changes the open model - and the
real result goes back instead.

The runner never sends a model setting or key: the host holds its model
configuration.
"""
from __future__ import annotations

import copy
import json
import math
import time
from urllib.parse import quote

import httpx

from .metrics import (
    ROLE_FIELDS, accepted, evaluate, merge_tools, models_by_role, role_usage, run_totals, sum_usage, summarize,
    total,
)
from .sse import iter_events

DEFAULT_EXECUTION = {
    "success": True,
    "error": None,
    "result": {"Status": "Success", "benchmark_fixture": True, "modified_model": False},
    "warnings": ["Synthetic benchmark feedback. No model operation was performed."],
}
ERROR_TEXT = 500                 # characters of a failed request's reason kept in the result
CONFIRMED_BY = "token-bench"


class HostError(Exception):
    """The host answered a request with a non-2xx status; the message carries its reason."""

    def __init__(self, status: int, reason: str, body: object = None):
        super().__init__(f"HTTP {status}: {reason}" if reason else f"HTTP {status}")
        self.status = status
        self.body = body


def error_reason(response: httpx.Response) -> tuple[str, object]:
    """``error``, ``message`` and ``detail`` of an error body (the host's ``{error, message}``
    and FastAPI's ``{detail}``), or its text when it is not such JSON; at most ERROR_TEXT
    characters. The response must have been read."""
    text = response.text
    try:
        body = json.loads(text)
    except ValueError:
        return text.strip()[:ERROR_TEXT], None
    if not isinstance(body, dict):
        return text.strip()[:ERROR_TEXT], body
    parts = []
    for key in ("error", "message", "detail"):
        value = body.get(key)
        if value is None or value == "":
            continue
        parts.append(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    return (" - ".join(parts) or text.strip())[:ERROR_TEXT], body


def check_status(response: httpx.Response) -> None:
    if not response.is_success:
        response.read()
        reason, body = error_reason(response)
        raise HostError(response.status_code, reason, body)


def validate_event(event: dict) -> None:
    kind = event["type"]
    if kind in ("stage", "usage", "tool"):
        if event.get("role") not in ("analyst", "planner", "supervisor"):
            raise ValueError(f"Invalid role in {kind} event")
    if kind == "usage":
        for field in ("input", "output", "cache_read", "cache_write", "ms"):
            value = event.get(field)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError(f"Invalid {field} in usage event")
    if kind == "tool":
        if not isinstance(event.get("id"), str) or not event["id"]:
            raise ValueError("Invalid tool event id")
        if event.get("status") not in ("running", "ok", "error"):
            raise ValueError("Invalid tool event status")
    if kind == "spec" and accepted(event):
        value = event["spec"]
        if not isinstance(value.get("action"), dict) or not isinstance(value.get("parameters", []), list):
            raise ValueError("Invalid accepted spec")


def skip_reason(task: dict, skip_requires=(), include_placeholder_ids: bool = False) -> str | None:
    needed = [name for name in task.get("requires", []) if name in skip_requires]
    if needed:
        return "requires " + ", ".join(needed) + " (--skip-requires)"
    if task.get("placeholder_ids") and not include_placeholder_ids:
        return "placeholder ids: fill in real ids from the test model (--include-placeholder-ids runs it)"
    return None


def skipped_result(task: dict, reason: str) -> dict:
    return {"id": task["id"], "kind": task["kind"], "requires": task.get("requires", []), "skipped": reason,
            "correct": None, "error": None}


def execute_spec(client: httpx.Client, base: str, spec: dict) -> dict:
    """What the page does after the designer confirms: ``/spec/confirm``, then the pack's
    ``/run`` with the token and the spec's values. A refused confirmation raises (the page
    shows the errors and runs nothing); a failed run becomes ``{success: false, error,
    message}`` that goes back to the model, as on the page."""
    action = spec.get("action") or {}
    if action.get("kind") != "run_tool" or not isinstance(action.get("tool"), str) or not action["tool"]:
        raise ValueError("Only a run_tool spec can be executed")
    response = client.post(base + "/api/v1/bridge/spec/confirm", json={"spec": spec, "confirmed_by": CONFIRMED_BY})
    check_status(response)
    token = response.json()["token"]
    params = {item["name"]: item.get("value") for item in spec.get("parameters", [])
              if isinstance(item, dict) and isinstance(item.get("name"), str)}
    response = client.post(base + "/api/v1/bridge/tools/" + quote(action["tool"], safe="") + "/run",
                           json={"params": params, "token": token})
    if response.is_success:
        return response.json()
    reason, body = error_reason(response)
    body = body if isinstance(body, dict) else {}
    return {"success": False, "error": body.get("error") or "request_failed",
            "message": body.get("message") or f"HTTP {response.status_code}: {reason}"}


def run_task(client: httpx.Client, base: str, label: str, task: dict, fixture: dict,
             execute: bool = False) -> dict:
    base = base.rstrip("/")
    started = time.perf_counter()
    events: list[dict] = []
    specs: list[dict] = []
    session_id = None
    turns = 0
    requests = 0
    feedback_turns = 0
    failure = None
    http_status = None
    execution = None

    def chat(payload: dict) -> list[dict]:
        nonlocal session_id, requests
        requests += 1
        current = []
        with client.stream("POST", base + "/api/chat", json=payload, headers={"X-Run-Label": label}) as response:
            check_status(response)
            sid = response.headers.get("X-Session-Id")
            if not sid:
                raise ValueError("Host omitted X-Session-Id")
            if session_id is not None and sid != session_id:
                raise ValueError("Host changed the session during a task")
            session_id = sid
            for event in iter_events(response.iter_lines()):
                validate_event(event)
                event = {**event, "request": requests}
                events.append(event)
                current.append(event)
                if event["type"] == "error":
                    raise ValueError(str(event.get("detail") or "Host reported an error"))
            if not current or current[-1]["type"] != "done":
                raise ValueError("SSE stream ended before done")
        return current

    try:
        messages = [task["message"], *task.get("answers", [])]
        for message in messages[:task.get("expect", {}).get("max_turns", 3)]:
            turns += 1
            payload = {"message": message, "session_id": session_id, "bridge": True}
            if task.get("mentions"):
                payload["mentions"] = task["mentions"]
            current = chat(payload)
            specs.extend(event for event in current if accepted(event))
            if specs:
                execution = execute_spec(client, base, specs[-1]["spec"]) if execute else copy.deepcopy(fixture)
                feedback_turns += 1
                chat({"execution": execution, "session_id": session_id})
                break
    except HostError as exc:
        failure, http_status = str(exc), exc.status
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        failure = f"{type(exc).__name__}: {exc}"[:ERROR_TEXT]

    usage_events = [event for event in events if event["type"] == "usage"]
    tool_events = merge_tools([event for event in events if event["type"] == "tool" and "id" in event])
    text = "".join(event["text"] for event in events if event["type"] == "text")
    verdict = evaluate(task, specs, tool_events, text, turns)
    if failure:
        verdict["correct"] = False
    result = {
        "id": task["id"], "kind": task["kind"], "requires": task.get("requires", []), "skipped": None,
        **verdict, "error": failure, "http_status": http_status,
        "turns": turns, "feedback_turns": feedback_turns, "requests": requests,
        "tool_calls": len(tool_events), "usage": sum_usage(usage_events),
        "by_role": role_usage(usage_events, tool_events), "models": models_by_role(events),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "session_id": session_id, "text": text, "events": events, "tools": tool_events,
    }
    if execute:
        result["execution"] = execution
    return result


def run_once(client: httpx.Client, base: str, label: str, tasks: list[dict], fixture: dict | None = None, *,
             skip_requires=(), include_placeholder_ids: bool = False, execute: bool = False) -> dict:
    """One pass over the task set under one run label."""
    results = []
    for task in tasks:
        reason = skip_reason(task, skip_requires, include_placeholder_ids)
        results.append(skipped_result(task, reason) if reason else
                       run_task(client, base, label, task, fixture if fixture is not None else DEFAULT_EXECUTION,
                                execute))
    totals, by_role = run_totals(results)
    return {"label": label, "tasks": results, "totals": totals, "by_role": by_role}


def host_runs(client: httpx.Client, base: str) -> tuple[dict | None, str | None]:
    """``GET /api/usage/runs`` by label, or None and why it is not available."""
    try:
        response = client.get(base.rstrip("/") + "/api/usage/runs")
        check_status(response)
        items = response.json()["items"]
        return {item["run_label"]: item for item in items if isinstance(item, dict)}, None
    except HostError as exc:
        return None, str(exc)
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        return None, f"{type(exc).__name__}: {exc}"[:ERROR_TEXT]


def host_roles(row: dict) -> dict | None:
    """The host's ``by_role`` parts (one per role and model) summed per role, unknown
    staying unknown; None when the host does not report ``by_role``."""
    parts = row.get("by_role")
    if not isinstance(parts, list):
        return None
    grouped: dict[str, list[dict]] = {}
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("role"), str):
            grouped.setdefault(part["role"], []).append(part)
    return {role: {field: total([part.get(field) for part in items]) for field in ROLE_FIELDS}
            for role, items in grouped.items()}


def cross_check(run: dict, runs_by_label: dict | None, unavailable: str | None) -> dict:
    """The repeat's totals from the ``usage`` events against the host's line for its label,
    and per role against its ``by_role`` when the host gives one. Unknown equals only
    unknown. The bench's own numbers never come from here."""
    if runs_by_label is None:
        return {"available": False, "reason": unavailable}
    row = runs_by_label.get(run["label"])
    if row is None:
        return {"available": True, "host": None, "host_by_role": None, "match": run["totals"]["calls"] is None,
                "mismatches": [] if run["totals"]["calls"] is None else ["label missing on the host"]}
    host = {field: row.get(field) for field in ROLE_FIELDS}
    mismatches = [field for field in ROLE_FIELDS if host[field] != run["totals"][field]]
    by_role = host_roles(row)
    if by_role is not None:
        for role in dict.fromkeys([*run["by_role"], *by_role]):
            ours, theirs = run["by_role"].get(role), by_role.get(role)
            if ours is None or theirs is None:
                mismatches.append(f"{role} (only on the {'host' if ours is None else 'bench'})")
                continue
            mismatches += [f"{role}.{field}" for field in ROLE_FIELDS if ours[field] != theirs[field]]
    return {"available": True, "host": host, "host_by_role": by_role, "match": not mismatches,
            "mismatches": mismatches}


def host_features(client: httpx.Client, base: str) -> dict | None:
    """``features`` of ``/config.json`` (mode, and the model names once the host lists them)."""
    try:
        response = client.get(base.rstrip("/") + "/config.json")
        check_status(response)
        features = response.json().get("features")
        return features if isinstance(features, dict) else None
    except (HostError, httpx.HTTPError, ValueError, AttributeError):
        return None


def run_labels(label: str, repeat: int) -> list[str]:
    return [f"{label}-{index}" for index in range(1, repeat + 1)]


def run_bench(client: httpx.Client, base: str, label: str, tasks: list[dict], fixture: dict | None = None, *,
              repeat: int = 1, skip_requires=(), include_placeholder_ids: bool = False,
              execute: bool = False) -> dict:
    """The task set ``repeat`` times under ``<label>-1`` .. ``<label>-<repeat>``, each repeat
    cross-checked against ``/api/usage/runs``, and the summary over the repeats. Refuses
    (ValueError, before any chat) a label the host already has calls for: its line would mix
    two runs."""
    labels = run_labels(label, repeat)
    before, unavailable = host_runs(client, base)
    if before is not None:
        taken = [name for name in labels if name in before]
        if taken:
            raise ValueError("The host already has calls under " + ", ".join(taken) + "; choose another --label")
    runs = []
    for name in labels:
        run = run_once(client, base, name, tasks, fixture, skip_requires=skip_requires,
                       include_placeholder_ids=include_placeholder_ids, execute=execute)
        after, unavailable = host_runs(client, base)
        run["host_usage"] = cross_check(run, after, unavailable)
        runs.append(run)
    return {
        "version": 2, "label": label, "base_url": base, "repeat": repeat,
        "mode": "execute" if execute else "synthetic",
        "skip_requires": sorted(set(skip_requires)), "include_placeholder_ids": include_placeholder_ids,
        "host": {"features": host_features(client, base)},
        "runs": runs, "summary": summarize(runs),
    }
