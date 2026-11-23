"""Drive only POST /api/chat, using fabricated execution feedback."""
from __future__ import annotations

import copy
import math
import time

import httpx

from .metrics import accepted, evaluate, merge_tools, sum_usage
from .sse import iter_events

DEFAULT_EXECUTION = {
    "success": True,
    "error": None,
    "result": {"Status": "Success", "benchmark_fixture": True, "modified_model": False},
    "warnings": ["Synthetic benchmark feedback. No model operation was performed."],
}


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


def run_task(client: httpx.Client, base: str, label: str, task: dict, fixture: dict) -> dict:
    started = time.perf_counter()
    events: list[dict] = []
    specs: list[dict] = []
    session_id = None
    turns = 0
    requests = 0
    feedback_turns = 0
    failure = None

    def chat(payload: dict) -> list[dict]:
        nonlocal session_id, requests
        requests += 1
        current = []
        with client.stream("POST", base.rstrip("/") + "/api/chat", json=payload,
                           headers={"X-Run-Label": label}) as response:
            response.raise_for_status()
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
                feedback_turns += 1
                chat({"execution": copy.deepcopy(fixture), "session_id": session_id})
                break
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        failure = f"{type(exc).__name__}: {exc}"

    usage_events = [event for event in events if event["type"] == "usage"]
    tool_events = merge_tools([event for event in events if event["type"] == "tool" and "id" in event])
    text = "".join(event["text"] for event in events if event["type"] == "text")
    verdict = evaluate(task, specs, tool_events, text, turns)
    if failure:
        verdict["correct"] = False
    roles = dict.fromkeys(event["role"] for event in events if isinstance(event.get("role"), str))
    by_role = {
        role: {**sum_usage([event for event in usage_events if event.get("role") == role]),
               "tool_calls": len([event for event in tool_events if event.get("role") == role])}
        for role in roles
    }
    return {
        "id": task["id"], "kind": task["kind"], **verdict, "error": failure,
        "turns": turns, "feedback_turns": feedback_turns, "requests": requests,
        "model_calls": len(usage_events) if usage_events else None,
        "tool_calls": len(tool_events), "usage": sum_usage(usage_events), "by_role": by_role,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "session_id": session_id, "text": text, "events": events, "tools": tool_events,
    }


def run_tasks(client: httpx.Client, base: str, label: str, tasks: list[dict], fixture: dict | None = None) -> dict:
    return {"version": 1, "label": label, "synthetic_execution": True,
            "tasks": [run_task(client, base, label, task, fixture if fixture is not None else DEFAULT_EXECUTION)
                      for task in tasks]}
