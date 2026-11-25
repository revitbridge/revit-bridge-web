"""Drive a running host over HTTP the way the page does.

By default only ``POST /api/chat`` is called (plus the read-only bridge health and catalogue, ``GET /config.json``,
``GET /api/usage/runs`` and, for a task that picks model items by name,
``GET /api/v1/bridge/mentions``): when the model's spec is accepted, a fixed execution
result (the task's own ``execution``, else the run's fixture) goes back through
``/api/chat`` so the report step is measured too, and nothing runs in Revit. When that
report's reply proposes a new accepted spec (the host's supervisor sent the task back
to its planner), the fixed result goes back again, as a designer confirming the new
card would send it, at most ``MAX_FEEDBACK_TURNS`` reports per task. With
``execute=True`` an accepted ``run_tool`` spec is confirmed
(``POST /api/v1/bridge/spec/confirm``) and run (``POST /api/v1/bridge/tools/{name}/run``)
- that changes the open model - and the real result goes back instead, once.

The chat mode (``mode``: ``auto``, the host's default, or ``single``, ``roles``,
``roles_analyst``) goes as ``mode`` in every chat request of the run. A local host runs
it; a cloud host runs its default instead (it honours a mode for an admin only, and the
bench sends no admin token). The results record the mode asked for (``chat_mode``), the
host's default (``chat.host``: ``/config.json`` ``features.chatModeDefault``, or
``features.roles`` on an older host), the modes the host said each turn ran in (the
first ``stage`` event's ``mode``: ``chat.modes_seen`` and each task's ``modes``) and the
roles the events named.

``confirm`` (``never`` by default, or ``once``): with ``once``, a tool or ask task whose
turn ends with no accepted spec while the host reports a proposal ready for the
designer's confirmation - read from the turn's tool events, not from the reply's words:
the turn's last ``reconcile`` call succeeded with ``ready: true`` and no ``propose_spec``
was called - gets one more user message, ``CONFIRM_MESSAGE``, and the task is scored on
what follows (``confirm_turns`` counts it; its tokens are in the task's usage). This lets
the model reach ``propose_spec`` in an unattended run; nothing more runs in Revit than
without it (the report step still uses the fixed result unless ``execute``).

A task's ``mentions`` are the designer's picks, sent with its first message (the
session keeps them for its later turns): ``{kind, id}`` as written, or ``{kind,
pick: {name | first: true, category?}}`` looked up first, as the page's picker
does, through ``GET /api/v1/bridge/mentions`` - the item of that kind whose name is
exactly ``name``, or the first one that can be picked (``category``: the OST_*
category of a type or a selected element). A pick that finds nothing skips the task (``skipped: pick not
found (<kind> <name>)``: the open model lacks what the task needs, which says nothing
about the model); more than one item of that name, or a lookup the host refuses,
fails it. Either way nothing is sent.

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
# Reports per task in the default mode: the first, and one per time the host's supervisor
# sends the task back (the host does that at most twice).
MAX_FEEDBACK_TURNS = 3
# How a run fed execution results back (``feedback_policy`` in the results; ``compare`` flags two
# runs that differ). Results written before it was recorded reported once per task.
FEEDBACK_FOLLOWS = f"follows send-backs (at most {MAX_FEEDBACK_TURNS} reports per task)"
FEEDBACK_ONCE = "one report per task"
MENTIONS_PATH = "/api/v1/bridge/mentions"
PICK_LIMIT = 200                 # the most the host lists for one pick
CHAT_MODES = ("auto", "single", "roles", "roles_analyst")
CONFIRM_POLICIES = ("never", "once")
# The designer's answer to a proposal the model waits on (confirm once).
CONFIRM_MESSAGE = "yes"
# Tasks whose turn may end at a proposal ready for confirmation.
CONFIRM_KINDS = ("tool", "ask")


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


class PickNotFound(Exception):
    """A task's pick found nothing in the open model: the task is skipped, not scored."""


def candidate_pages(client: httpx.Client, base: str, kind: str, pick: dict, what: str):
    """List pages of the picker. A V1 host only supports the first page."""
    limit = 50 if kind == "element" else PICK_LIMIT
    params = {"kind": kind, "limit": limit}
    if kind == "element":
        params["source"] = "selection"
    elif pick.get("category"):
        params["category"] = pick["category"]
    offset = 0
    ceiling = None
    incomplete = False
    while True:
        response = client.get(base + MENTIONS_PATH, params=params)
        check_status(response)
        reply = response.json()
        if not isinstance(reply, dict) or reply.get("error"):
            error = reply.get("error") if isinstance(reply, dict) else "not an object"
            raise ValueError(f"mention pick {what}: the host answered {error}")
        rows = reply.get("items")
        if not isinstance(rows, list):
            raise ValueError(f"mention pick {what}: invalid items")
        if "offset" not in reply and reply.get("truncated") and "name" in pick and "q" not in params:
            # V1 has one page and a substring filter. V2 ranks and interprets aliases,
            # so enumerate its pages with no search terms for an exact-name lookup.
            params["q"] = pick["name"]
            continue
        yield rows
        if "offset" not in reply:
            return
        incomplete |= bool(reply.get("facets_partial") or reply.get("warnings"))
        if not reply.get("truncated"):
            break
        total = reply.get("total")
        if type(total) is not int or reply["offset"] != offset:
            raise ValueError(f"mention pick {what}: invalid pagination")
        ceiling = total if ceiling is None else min(ceiling, total)
        offset += limit
        if not rows or offset >= ceiling:
            # A truncated last page can mean the native inventory itself was cut short.
            incomplete = True
            break
        params["offset"] = offset
    if incomplete:
        raise ValueError(f"mention pick {what}: incomplete listing")


def pick_mentions(client: httpx.Client, base: str, mentions: list[dict]) -> list[dict]:
    """Resolve named or first matching picks. No match skips a task; ambiguous names fail."""
    picked = []
    for mention in mentions:
        kind = mention["kind"]
        if "pick" not in mention:
            picked.append({"kind": kind, "id": mention["id"]})
            continue
        pick = mention["pick"]
        named = pick.get("name", "first")
        if pick.get("category"):
            named += f" in {pick['category']}"
        what = f"@{kind} {named}"
        ids = (item["id"] for page in candidate_pages(client, base, kind, pick, what) for item in page
               if isinstance(item, dict) and item.get("kind") == kind and not item.get("disabled")
               and type(item.get("id")) is int and item["id"] > 0
               and ("name" not in pick or item.get("name") == pick["name"])
               and (kind != "element" or "category" not in pick or item.get("category") == pick["category"]))
        first = next(ids, None)
        if first is None:
            raise PickNotFound(f"pick not found ({kind} {named})")
        if "name" in pick and next(ids, None) is not None:
            raise ValueError(f"mention pick {what}: 2 items have that name (or more)")
        picked.append({"kind": kind, "id": first})
    return picked


def execute_spec(client: httpx.Client, base: str, spec: dict, session_id: str | None = None) -> dict:
    """What the page does after the designer confirms: ``/spec/confirm`` (with the chat
    session, so a parameter bound to a pick of that session is accepted), then the pack's
    ``/run`` with the token and the spec's values. A refused confirmation raises (the page
    shows the errors and runs nothing); a failed run becomes ``{success: false, error,
    message}`` that goes back to the model, as on the page."""
    action = spec.get("action") or {}
    if action.get("kind") != "run_tool" or not isinstance(action.get("tool"), str) or not action["tool"]:
        raise ValueError("Only a run_tool spec can be executed")
    body = {"spec": spec, "confirmed_by": CONFIRMED_BY}
    if session_id:
        body["session_id"] = session_id
    response = client.post(base + "/api/v1/bridge/spec/confirm", json=body)
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


def waits_for_confirmation(events: list[dict]) -> bool:
    """The host's structural sign that a turn stopped at a proposal ready for the designer's
    confirmation: its tool events show no ``propose_spec`` call, and its last ``reconcile`` call
    ended ``ok`` with a result (the event's ``preview``, read only when it is the whole result)
    whose ``ready`` is true. The reply's wording is never read."""
    tools = merge_tools([event for event in events if event.get("type") == "tool" and "id" in event])
    if any(tool.get("name") == "propose_spec" for tool in tools):
        return False
    reconciles = [tool for tool in tools if tool.get("name") == "reconcile"]
    if not reconciles or reconciles[-1].get("status") != "ok":
        return False
    last = reconciles[-1]
    preview, size = last.get("preview"), last.get("size")
    if not isinstance(preview, str) or (isinstance(size, int) and size > len(preview)):
        return False                     # cut: the whole result is not there to read
    try:
        result = json.loads(preview)
    except ValueError:
        return False
    return isinstance(result, dict) and result.get("ready") is True


def turn_modes(events: list[dict]) -> list[str]:
    """The chat modes the host said its turns ran in (each turn's first ``stage`` event), each once."""
    return list(dict.fromkeys(event["mode"] for event in events
                              if event.get("type") == "stage" and isinstance(event.get("mode"), str)))


def run_task(client: httpx.Client, base: str, label: str, task: dict, fixture: dict,
             execute: bool = False, mode: str = "auto", confirm: str = "never") -> dict:
    base = base.rstrip("/")
    started = time.perf_counter()
    events: list[dict] = []
    specs: list[dict] = []
    session_id = None
    turns = 0
    requests = 0
    feedback_turns = 0
    confirm_turns = 0
    failure = None
    http_status = None
    execution = None
    mentions = None

    def chat(payload: dict) -> list[dict]:
        nonlocal session_id, requests
        requests += 1
        payload = {**payload, "mode": mode}
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
        mentions = pick_mentions(client, base, task.get("mentions") or [])
        messages = [task["message"], *task.get("answers", [])]
        for message in messages[:task.get("expect", {}).get("max_turns", 3)]:
            turns += 1
            payload = {"message": message, "session_id": session_id, "bridge": True}
            if mentions and turns == 1:            # picked in the task's message; the session keeps them
                payload["mentions"] = mentions
            current = chat(payload)
            specs.extend(event for event in current if accepted(event))
            if (not specs and confirm == "once" and confirm_turns == 0 and task["kind"] in CONFIRM_KINDS
                    and waits_for_confirmation(current)):
                # The model stopped at a ready proposal and waits for a yes: say it once, then
                # score the task on what follows.
                confirm_turns += 1
                current = chat({"message": CONFIRM_MESSAGE, "session_id": session_id, "bridge": True})
                specs.extend(event for event in current if accepted(event))
                if not specs:
                    break
            if specs:
                if execute:
                    execution = execute_spec(client, base, specs[-1]["spec"], session_id)
                    feedback_turns += 1
                    chat({"execution": execution, "session_id": session_id})
                    break
                fixed = task["execution"] if isinstance(task.get("execution"), dict) else fixture
                while feedback_turns < MAX_FEEDBACK_TURNS:
                    execution = copy.deepcopy(fixed)
                    feedback_turns += 1
                    again = [event for event in chat({"execution": execution, "session_id": session_id})
                             if accepted(event)]
                    if not again:
                        break
                    specs.extend(again)          # sent back: the new card is confirmed in turn
                break
    except HostError as exc:
        failure, http_status = str(exc), exc.status
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        failure = f"{type(exc).__name__}: {exc}"[:ERROR_TEXT]

    usage_events = [event for event in events if event["type"] == "usage"]
    tool_events = merge_tools([event for event in events if event["type"] == "tool" and "id" in event])
    text = "".join(event["text"] for event in events if event["type"] == "text")
    verdict = evaluate(task, specs, tool_events, text, turns, mentions=mentions)
    if failure:
        verdict["correct"] = False
    result = {
        "id": task["id"], "kind": task["kind"], "requires": task.get("requires", []), "skipped": None,
        **verdict, "error": failure, "http_status": http_status,
        "turns": turns, "feedback_turns": feedback_turns, "confirm_turns": confirm_turns, "requests": requests,
        "modes": turn_modes(events),
        "tool_calls": len(tool_events), "usage": sum_usage(usage_events),
        "by_role": role_usage(usage_events, tool_events), "models": models_by_role(events),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "session_id": session_id, "text": text, "events": events, "tools": tool_events,
    }
    if task.get("mentions"):
        result["mentions"] = mentions        # as sent; None when a pick failed
    if execute:
        result["execution"] = execution
    return result


def run_once(client: httpx.Client, base: str, label: str, tasks: list[dict], fixture: dict | None = None, *,
             skip_requires=(), include_placeholder_ids: bool = False, execute: bool = False, mode: str = "auto",
             confirm: str = "never") -> dict:
    """One pass over the task set under one run label."""
    results = []
    for task in tasks:
        reason = skip_reason(task, skip_requires, include_placeholder_ids)
        if reason:
            results.append(skipped_result(task, reason))
            continue
        try:
            results.append(run_task(client, base, label, task,
                                    fixture if fixture is not None else DEFAULT_EXECUTION, execute,
                                    mode=mode, confirm=confirm))
        except PickNotFound as exc:          # raised before any chat: nothing was measured
            results.append(skipped_result(task, str(exc)))
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


def host_catalogue(client: httpx.Client, base: str) -> dict:
    """Measure the target host, including its installed user packs. Older hosts may omit either value."""
    def read(path):
        try:
            response = client.get(base.rstrip("/") + "/api/v1/bridge/" + path)
            check_status(response)
            return response.json()
        except (HostError, httpx.HTTPError, ValueError):
            return None

    health = read("revit-health")
    tools = read("tools")
    version = health.get("bridge_version") if isinstance(health, dict) else None
    return {"revit_bridge_version": version if isinstance(version, str) else None,
            "tool_count": len(tools) if isinstance(tools, list) else None}


def run_labels(label: str, repeat: int) -> list[str]:
    return [f"{label}-{index}" for index in range(1, repeat + 1)]


def run_bench(client: httpx.Client, base: str, label: str, tasks: list[dict], fixture: dict | None = None, *,
              repeat: int = 1, skip_requires=(), include_placeholder_ids: bool = False,
              execute: bool = False, mode: str = "auto", confirm: str = "never") -> dict:
    """The task set ``repeat`` times under ``<label>-1`` .. ``<label>-<repeat>``, each repeat
    cross-checked against ``/api/usage/runs``, and the summary over the repeats; every chat
    request carries ``mode``, and ``confirm`` is the confirmation policy (see the module).
    Refuses (ValueError, before any chat) a label the host already has calls for: its line
    would mix two runs; and a mode or policy that is not one of the known."""
    if mode not in CHAT_MODES:
        raise ValueError(f"mode must be one of {', '.join(CHAT_MODES)}")
    if confirm not in CONFIRM_POLICIES:
        raise ValueError(f"confirm must be one of {', '.join(CONFIRM_POLICIES)}")
    labels = run_labels(label, repeat)
    before, unavailable = host_runs(client, base)
    if before is not None:
        taken = [name for name in labels if name in before]
        if taken:
            raise ValueError("The host already has calls under " + ", ".join(taken) + "; choose another --label")
    runs = []
    for name in labels:
        run = run_once(client, base, name, tasks, fixture, skip_requires=skip_requires,
                       include_placeholder_ids=include_placeholder_ids, execute=execute, mode=mode, confirm=confirm)
        after, unavailable = host_runs(client, base)
        run["host_usage"] = cross_check(run, after, unavailable)
        runs.append(run)
    features = host_features(client, base)
    return {
        "version": 2, "label": label, "base_url": base, "repeat": repeat,
        "mode": "execute" if execute else "synthetic",
        "skip_requires": sorted(set(skip_requires)), "include_placeholder_ids": include_placeholder_ids,
        "host": {"features": features, **host_catalogue(client, base)},
        "chat_mode": mode,
        "chat": chat_mode(features, runs),
        "feedback_policy": FEEDBACK_ONCE if execute else FEEDBACK_FOLLOWS,
        "confirm_policy": confirm,
        "runs": runs, "summary": summarize(runs),
    }


def chat_mode(features: dict | None, runs: list[dict]) -> dict:
    """How the host runs its chat by default, as it says (``features.chatModeDefault``); a host
    before the modes says only ``features.roles``: ``false`` is ``single``, ``true`` is None (not
    reported - there it meant the analyst too unless ROLES_ANALYST=off, which it does not say),
    and so is a host that says neither. Also the modes its turns ran in (``modes_seen``) and the
    roles the events named."""
    features = features or {}
    default = features.get("chatModeDefault")
    if default not in CHAT_MODES[1:]:
        default = "single" if features.get("roles") is False else None
    tasks = [task for run in runs for task in run["tasks"]]
    seen = dict.fromkeys(role for task in tasks for role in task.get("by_role", {}))
    modes = dict.fromkeys(m for task in tasks for m in task.get("modes", []))
    return {"host": default, "modes_seen": list(modes), "roles_seen": list(seen)}
