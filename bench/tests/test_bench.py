"""The bench against a fake host (httpx.MockTransport): parsing, scoring, skips, failures,
null handling, repeats, compare and the command line."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import bench.__main__ as cli
from bench.__main__ import load_tasks, main
from bench.metrics import (
    accepted, compare_exit_code, compare_runs, equal_value, stat, sum_usage, summarize,
)
from bench.runner import DEFAULT_EXECUTION, FEEDBACK_FOLLOWS, run_bench, run_once
from bench.sse import iter_events, parse_frame

BASE = "http://127.0.0.1:7860"


def frame(event, value):
    return (f"event: {event}\n" if event else "") + "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"


def usage(role="planner", **changes):
    return {"role": role, "model": "fixture-model", "round": 1, "input": 100,
            "output": 20, "cache_read": 40, "cache_write": 0, "ms": 10, **changes}


def spec(tool="create_wall", **changes):
    return {"spec": {"action": {"kind": "run_tool", "tool": tool},
                     "parameters": [{"name": "height", "value": 3000.0}]},
            "errors": [], "reconcile": {"ready": True}, **changes}


def task(task_id="wall", **changes):
    return {"id": task_id, "kind": "tool", "message": "Make a wall",
            "expect": {"tool": "create_wall", "params": {"height": 3000}, "max_turns": 3}, **changes}


class Fragments(httpx.SyncByteStream):
    def __init__(self, text):
        self.data = text.encode("utf-8")

    def __iter__(self):
        # Split UTF-8 code points, field names and line boundaries deliberately.
        for index in range(0, len(self.data), 2):
            yield self.data[index:index + 2]


class FakeHost:
    """``/api/chat`` answers the scripted replies in order; ``/api/usage/runs`` answers
    ``runs`` (a list of rows, or an int status); confirm and run answer as configured."""

    def __init__(self, replies, runs=None, run_reply=None, candidates=None):
        self.replies = list(replies)
        self.requests = []
        self.other = []
        self.sessions = 0
        self.runs = runs if runs is not None else []
        self.run_reply = run_reply or httpx.Response(200, json={"success": True, "evidence_id": "ev-1"})
        self.candidates = candidates or {}         # kind -> items, or an httpx.Response
        self.lookups = []

    def handle(self, request):
        if request.url.path == "/api/v1/bridge/mentions":
            assert request.method == "GET"
            params = dict(request.url.params)
            self.lookups.append(params)
            answer = self.candidates.get(params["kind"], [])
            if isinstance(answer, httpx.Response):
                return answer
            needle = params.get("q", "").casefold()
            items = [item for item in answer if needle in str(item.get("name", "")).casefold()]
            return httpx.Response(200, json={"kind": params["kind"], "items": items, "total": len(items),
                                             "truncated": False})
        if request.url.path == "/api/usage/runs":
            self.other.append(request)
            if isinstance(self.runs, int):
                return httpx.Response(self.runs, json={"error": "admin_required", "message": "admin token"})
            return httpx.Response(200, json={"items": self.runs})
        if request.url.path == "/config.json":
            self.other.append(request)
            return httpx.Response(200, json={"features": {"mode": "local"}})
        if request.url.path == "/api/v1/bridge/spec/confirm":
            self.other.append(request)
            return httpx.Response(200, json={"token": "tok-1", "spec_hash": "h", "expires_at": "t", "card": {}})
        if request.url.path.endswith("/run"):
            self.other.append(request)
            return self.run_reply
        assert request.method == "POST"
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        self.requests.append((payload, request.headers))
        if payload["session_id"] is None:
            self.sessions += 1
        sid = payload["session_id"] or f"session-{self.sessions}"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        if isinstance(reply, int):
            return httpx.Response(reply)
        return httpx.Response(200, headers={"X-Session-Id": sid, "Content-Type": "text/event-stream"},
                              stream=Fragments(reply))

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def run(self, tasks, fixture=None, **options):
        with self.client() as client:
            return run_once(client, BASE, "test-label", tasks, fixture, **options)


def response(*events):
    return "".join(frame(name, value) for name, value in events) + "event: done\n\n"


# -- SSE ------------------------------------------------------------------------------

def test_sse_comments_multiline_empty_unknown_and_done():
    lines = [":comment", "", "", "event: future", "data: not-json", "", "event: usage",
             'data: {"input": 10,', 'data: "output": null}', "", "event: done", "", 'data: "ignored"', ""]
    assert list(iter_events(lines)) == [{"type": "usage", "input": 10, "output": None}, {"type": "done"}]
    assert parse_frame(["data: [DONE]"]) == {"type": "done"}
    assert parse_frame(["event: done"]) == {"type": "done"}
    assert list(iter_events(['data: "last"'])) == [{"type": "text", "text": "last"}]
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_frame(["event: usage", "data: broken"])


def test_sse_crlf_lines_and_split_utf8_code_points():
    text = "\u5899\u5df2\u8ba1\u5212"
    body = (f'data: {json.dumps(text, ensure_ascii=False)}\r\n\r\nevent: usage\r\n'
            'data: {"role": "planner", "input": 1}\r\n\r\nevent: done\r\n\r\n')
    host = FakeHost([httpx.Response(200, headers={"X-Session-Id": "s", "Content-Type": "text/event-stream"},
                                    stream=Fragments(body))])
    result = host.run([task(kind="none", expect={})])["tasks"][0]
    assert result["text"] == text and result["error"] is None
    assert result["usage"]["input"] == 1


@pytest.mark.parametrize("changes", [
    {"errors": ["bad"]}, {"errors": None}, {"reconcile": {"ready": False}},
    {"reconcile": {"ready": 1}}, {"reconcile": None}, {"spec": None},
])
def test_spec_acceptance_is_strict(changes):
    assert accepted({"type": "spec", **spec()})
    assert not accepted({"type": "spec", **spec(**changes)})


def test_params_compare_numerically_without_boolean_or_string_coercion():
    assert equal_value(3000, 3000.0)
    assert equal_value({"x": [1, 2]}, {"x": [1.0, 2]})
    assert not equal_value(True, 1)
    assert not equal_value("3000", 3000)
    assert not equal_value({"x": 1}, {"x": 1, "y": 2})


def test_usage_null_propagates_per_field_and_no_events_is_unknown():
    result = sum_usage([usage(), usage(input=None, cache_read=None)])
    assert result == {"calls": 2, "input": None, "output": 40, "cache_read": None,
                      "cache_write": 0, "ms": 20}
    assert sum_usage([])["input"] is None and sum_usage([])["calls"] is None
    assert sum_usage([usage(input=0)])["input"] == 0


# -- one task ---------------------------------------------------------------------------

def test_fragmented_stream_feedback_mentions_and_role_totals():
    tool = {"id": "q1", "role": "planner", "name": "query", "args": {"kind": "levels"}, "status": "running"}
    host = FakeHost([
        response(("stage", {"role": "planner", "model": "fixture-model", "status": "start"}),
                 ("tool", tool), ("tool", {**tool, "status": "ok", "ms": 1}),
                 ("usage", usage()), ("spec", spec()), ("", "\u5899\u5df2\u8ba1\u5212")),
        response(("usage", usage("supervisor", input=None, model="small")), ("", "Synthetic feedback checked")),
    ])
    fixture = {"success": True, "result": {"fixture": 5}}
    result = host.run([task(mentions=[{"kind": "level", "id": 10}])], fixture)["tasks"][0]
    assert result["correct"] and result["params_correct"]
    assert result["text"].startswith("\u5899\u5df2\u8ba1\u5212")
    assert result["turns"] == 1 and result["requests"] == 2 and result["feedback_turns"] == 1
    assert result["usage"]["calls"] == 2 and result["tool_calls"] == 1
    assert result["tools"][0]["status"] == "ok"
    assert result["usage"]["input"] is None and result["usage"]["output"] == 40
    assert result["by_role"]["planner"] == {"calls": 1, "input": 100, "output": 20, "cache_read": 40,
                                            "cache_write": 0, "ms": 10, "tool_calls": 1}
    assert result["by_role"]["supervisor"]["input"] is None and result["by_role"]["supervisor"]["calls"] == 1
    assert result["models"] == {"planner": ["fixture-model"], "supervisor": ["small"]}
    assert host.requests[0][0]["mentions"] == [{"kind": "level", "id": 10}]
    assert host.requests[0][0]["bridge"] is True
    assert host.requests[1][0] == {"execution": fixture, "session_id": "session-1"}
    assert host.requests[0][1]["X-Run-Label"] == "test-label"
    assert "x-llm-key" not in host.requests[0][1] and "x-llm-base-url" not in host.requests[0][1]


def test_clarification_rejected_spec_does_not_send_execution():
    host = FakeHost([
        response(("spec", spec(errors=[{"code": "missing"}])), ("", "Which level?")),
        response(("spec", spec()), ("usage", usage())),
        response(("usage", usage("supervisor"))),
    ])
    result = host.run([task(kind="ask", answers=["F2", "unused"])])["tasks"][0]
    assert result["correct"] and result["turns"] == 2
    assert host.requests[1][0]["message"] == "F2"
    assert host.requests[2][0]["execution"] == DEFAULT_EXECUTION


def test_max_turns_caps_user_messages_and_still_measures_feedback():
    host = FakeHost([response(("", "Which level?"))])
    result = host.run([task(answers=["F2", "F3"], expect={"tool": "create_wall", "max_turns": 1})])["tasks"][0]
    assert result["turns"] == 1 and not result["correct"]
    assert result["usage"]["calls"] is None
    host = FakeHost([response(("spec", spec())), response(("usage", usage("supervisor")))])
    result = host.run([task(expect={"tool": "create_wall", "max_turns": 1})])["tasks"][0]
    assert result["turns"] == 1 and result["feedback_turns"] == 1


@pytest.mark.parametrize("bad", [
    503, httpx.ReadTimeout("fixture timeout"), httpx.ConnectError("fixture connection failure"),
    frame("error", {"detail": "fixture failure"}), 'data: "incomplete"\n\n',
    "event: usage\ndata: invalid\n\n",
    frame("tool", {"role": "planner", "id": [], "status": "ok"}),
    frame("usage", usage(input=float("nan"))),
    frame("spec", spec(spec={"action": None})),
])
def test_task_failure_isolated_and_each_task_gets_new_session(bad):
    host = FakeHost([bad, response(("", "No supported tool"), ("usage", usage()))])
    result = host.run([task("bad"), task("good", kind="none", expect={})])["tasks"]
    assert result[0]["error"] and not result[0]["correct"]
    assert result[1]["correct"] and result[1]["error"] is None
    assert host.requests[0][0]["session_id"] is None
    assert host.requests[1][0]["session_id"] is None


@pytest.mark.parametrize("reply,expected", [
    (httpx.Response(422, json={"error": "mention_not_found", "message": "no level 101 in this model"}),
     "HTTP 422: mention_not_found - no level 101 in this model"),
    (httpx.Response(400, json={"detail": "No model configured: fill in Model settings"}),
     "HTTP 400: No model configured: fill in Model settings"),
    (httpx.Response(422, json={"detail": [{"loc": ["body", "message"], "msg": "too long"}]}),
     'HTTP 422: [{"loc": ["body", "message"], "msg": "too long"}]'),
    (httpx.Response(403, json={"error": "local_mode_host", "message": "loopback only", "hint": "x"}),
     "HTTP 403: local_mode_host - loopback only"),
    (httpx.Response(502, text="Bad gateway from the proxy"), "HTTP 502: Bad gateway from the proxy"),
    (httpx.Response(500, text=""), "HTTP 500"),
])
def test_a_failed_request_keeps_the_error_body(reply, expected):
    host = FakeHost([reply])
    result = host.run([task()])["tasks"][0]
    assert result["error"] == expected
    assert result["http_status"] == reply.status_code and not result["correct"]


def test_a_long_error_body_is_cut_to_500_characters():
    host = FakeHost([httpx.Response(400, json={"error": "invalid_args", "message": "x" * 2000})])
    result = host.run([task()])["tasks"][0]
    assert result["error"].startswith("HTTP 400: invalid_args - xxx")
    assert len(result["error"]) == len("HTTP 400: ") + 500


@pytest.mark.parametrize("tool_name", ["query", "get_project_snapshot"])
def test_query_needs_successful_read_tool_and_no_accepted_spec(tool_name):
    query = {"id": "q", "role": "planner", "name": tool_name, "status": "ok", "args": {}}
    host = FakeHost([response(("tool", query), ("", "Found two levels")), response(("", "Found two levels"))])
    result = host.run([task("yes", kind="query", expect={}), task("no", kind="query", expect={})])["tasks"]
    assert result[0]["correct"] and not result[1]["correct"]


@pytest.mark.parametrize("tool_name,correct", [
    ("query_levels", True), ("query_model_stats", True), ("create_wall", False),
])
def test_a_query_answered_with_a_read_only_pack_counts(tool_name, correct):
    host = FakeHost([response(("spec", spec(tool_name)), ("", "Card below")), response(("", "Reported"))])
    result = host.run([task("q", kind="query", expect={})])["tasks"][0]
    assert result["correct"] is correct and result["feedback_turns"] == 1


def test_tool_ids_are_scoped_per_request_and_errors_remain_visible():
    tool = {"id": "same", "role": "planner", "name": "query", "status": "error", "args": {}}
    host = FakeHost([response(("tool", tool), ("", "Need details")),
                     response(("tool", {**tool, "status": "ok"}), ("spec", spec())), response(("", "Checked"))])
    result = host.run([task(answers=["details"])])["tasks"][0]
    assert result["tool_calls"] == 2
    assert result["tools"][0]["status"] == "error"


def test_missing_expected_parameter_fails_even_when_expected_null():
    host = FakeHost([response(("spec", spec())), response(("", "Checked"))])
    result = host.run([task(expect={"tool": "create_wall", "params": {"unknown": None}})])["tasks"][0]
    assert not result["params_correct"] and not result["correct"]


# -- skips ------------------------------------------------------------------------------

def test_required_and_placeholder_tasks_are_skipped_not_scored():
    tasks = [task("needs", requires=["basic-tools"]), task("ids", placeholder_ids=True),
             task("plain", kind="none", expect={})]
    host = FakeHost([response(("", "No tool"), ("usage", usage()))])
    run = host.run(tasks, skip_requires=["basic-tools"])
    needs, ids, plain = run["tasks"]
    assert needs["skipped"].startswith("requires basic-tools") and needs["correct"] is None
    assert ids["skipped"].startswith("placeholder ids") and ids["correct"] is None
    assert plain["correct"] and len(host.requests) == 1
    summary = summarize([run])
    assert [entry["skipped"] is not None for entry in summary["tasks"]] == [True, True, False]
    assert summary["tasks"][0]["runs"] == 0 and summary["scored"]["median"] == 1
    assert summary["correct"]["median"] == 1


def test_without_the_flags_the_marked_tasks_run():
    tasks = [task("needs", requires=["basic-tools"]), task("ids", placeholder_ids=True)]
    host = FakeHost([response(("", "Cannot")), response(("", "Cannot"))])
    run = host.run(tasks, include_placeholder_ids=True)
    assert [result["skipped"] for result in run["tasks"]] == [None, None]
    assert len(host.requests) == 2


def test_example_tasks_mark_unreleased_tools_and_pick_their_mentions():
    tasks = load_tasks(Path(__file__).parents[1] / "tasks.yml")
    assert len(tasks) == 12
    marked = {item["id"]: item.get("requires") for item in tasks if item.get("requires")}
    assert marked == {"new-level": ["basic-tools"], "new-grid": ["basic-tools"], "move-selection": ["basic-tools"]}
    assert {item["id"] for item in tasks if item.get("placeholder_ids")} == set()
    mentions = {item["id"]: item["mentions"] for item in tasks if item.get("mentions")}
    assert mentions == {"mentioned-level": [{"kind": "level", "pick": {"name": "F2"}}],
                        "mentioned-link": [{"kind": "link", "pick": {"first": True}}]}


def test_task_file_validation(tmp_path):
    path = tmp_path / "tasks.yml"
    for data in ([], [task(), task()], [task(kind="invalid")], [task(expect={"max_turns": 0})],
                 [task(answers="answer")], [task(mentions=[{"kind": "level", "id": True}])],
                 [task(mentions={"kind": "level", "id": 1})], [task(mentions=[{"kind": "view", "id": 1}])],
                 [task(mentions=[{"kind": "level", "id": 1}] * 21)],
                 [task(mentions=[{"kind": "level", "pick": {}}])],
                 [task(mentions=[{"kind": "level", "pick": {"name": "F2", "first": True}}])],
                 [task(mentions=[{"kind": "level", "pick": {"name": " "}}])],
                 [task(mentions=[{"kind": "level", "pick": {"first": False}}])],
                 [task(mentions=[{"kind": "level", "pick": {"name": "F2", "level": 1}}])],
                 [task(mentions=[{"kind": "level", "id": 1, "pick": {"name": "F2"}}])],
                 [task(mentions=[{"kind": "level", "pick": {"name": "F2", "category": "OST_Walls"}}])],
                 [task(mentions=[{"kind": "type", "pick": {"name": "Generic - 200mm"}}])],
                 [task(mentions=[{"kind": "type", "pick": {"name": "Generic", "category": "Walls"}}])],
                 [task(requires="basic-tools")], [task(requires=["Basic Tools"])],
                 [task(placeholder_ids="yes")], [task(execution="failed")], [task(execution={"error": "x"})],
                 [task(execution={"success": "no"})]):
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            load_tasks(path)


# -- repeats, host totals ------------------------------------------------------------------

def test_stat_median_min_max_and_unknown():
    assert stat([3, 1, 2]) == {"median": 2, "min": 1, "max": 3}
    assert stat([100, 102]) == {"median": 101, "min": 100, "max": 102}
    assert stat([100, 101]) == {"median": 100.5, "min": 100, "max": 101}
    assert stat([100, None, 300]) == {"median": None, "min": None, "max": None}
    assert stat([]) == {"median": None, "min": None, "max": None}
    assert stat([0, 0]) == {"median": 0, "min": 0, "max": 0}


def run_rows(label, calls, input_, cache_read=None):
    return {"run_label": label, "calls": calls, "input": input_, "output": 20 * calls,
            "cache_read": cache_read, "cache_write": 0, "ms": 10 * calls,
            "first_at": "2026-11-23 00:00:00", "last_at": "2026-11-23 00:00:01"}


def test_repeats_use_numbered_labels_and_summarize_with_unknowns():
    replies = [
        # repeat 1: wall (planner + supervisor), none
        response(("usage", usage(input=100)), ("spec", spec())), response(("usage", usage("supervisor", input=10))),
        response(("", "No tool"), ("usage", usage(input=50))),
        # repeat 2: the planner's input unknown
        response(("usage", usage(input=None)), ("spec", spec())), response(("usage", usage("supervisor", input=30))),
        response(("", "No tool"), ("usage", usage(input=70))),
        # repeat 3
        response(("usage", usage(input=300)), ("spec", spec())), response(("usage", usage("supervisor", input=20))),
        response(("", "No tool"), ("usage", usage(input=60))),
    ]
    host = FakeHost(replies)
    with host.client() as client:
        result = run_bench(client, BASE, "rep", [task(), task("none", kind="none", expect={})], repeat=3)
    labels = [headers["X-Run-Label"] for _, headers in host.requests]
    assert labels == ["rep-1"] * 3 + ["rep-2"] * 3 + ["rep-3"] * 3
    assert [run["label"] for run in result["runs"]] == ["rep-1", "rep-2", "rep-3"]
    wall, none = result["summary"]["tasks"]
    assert wall["runs"] == 3 and wall["correct"] == 3
    # one unknown input among the repeats: median, min and max unknown
    assert wall["metrics"]["input"] == {"median": None, "min": None, "max": None}
    assert wall["metrics"]["output"] == {"median": 40, "min": 40, "max": 40}
    assert wall["by_role"]["planner"]["input"] == {"median": None, "min": None, "max": None}
    assert wall["by_role"]["supervisor"]["input"] == {"median": 20, "min": 10, "max": 30}
    assert none["metrics"]["input"] == {"median": 60, "min": 50, "max": 70}
    # the role that made no call in the none task: zero, not unknown
    assert "supervisor" not in none["by_role"]
    totals = result["summary"]["totals"]
    assert totals["input"] == {"median": None, "min": None, "max": None}
    assert totals["calls"] == {"median": 3, "min": 3, "max": 3}
    assert result["summary"]["by_role"]["supervisor"]["input"] == {"median": 20, "min": 10, "max": 30}
    assert result["summary"]["by_role"]["planner"]["calls"] == {"median": 2, "min": 2, "max": 2}


def test_a_failed_repeat_makes_the_task_numbers_unknown():
    replies = [response(("", "No tool"), ("usage", usage(input=50))),
               response(("usage", usage(input=70)), ("error", {"detail": "model failed"}))]
    host = FakeHost(replies)
    with host.client() as client:
        result = run_bench(client, BASE, "f", [task("none", kind="none", expect={})], repeat=2)
    entry = result["summary"]["tasks"][0]
    assert entry["runs"] == 2 and entry["correct"] == 1 and entry["failed"] == 1
    assert entry["errors"] == ["ValueError: model failed"]
    assert entry["metrics"]["input"]["median"] is None
    # the repeat's totals still hold the call that was made
    assert result["runs"][1]["totals"]["input"] == 70


def test_role_zero_fill_only_when_the_host_reported_usage():
    from bench.metrics import role_values
    done = {"error": None, "usage": {"calls": 1}, "by_role": {"planner": {}}}
    assert role_values(done, "supervisor")["input"] == 0
    assert role_values({**done, "usage": {"calls": None}}, "supervisor") is None
    assert role_values({**done, "error": "x"}, "supervisor") is None


def test_host_totals_cross_check_match_mismatch_and_unavailable():
    replies = [response(("usage", usage()), ("", "No tool"))]
    host = FakeHost(replies)
    host.runs = []

    def runs_after(request):
        # before the run: nothing; after: the host's line for the label
        return [run_rows("cc-1", 1, 100, cache_read=40)] if host.requests else []

    original = host.handle

    def handle(request):
        if request.url.path == "/api/usage/runs":
            return httpx.Response(200, json={"items": runs_after(request)})
        return original(request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = run_bench(client, BASE, "cc", [task("n", kind="none", expect={})])
    check = result["runs"][0]["host_usage"]
    assert check["available"] and check["match"] and check["mismatches"] == []
    assert check["host"]["input"] == 100

    host = FakeHost([response(("usage", usage()), ("", "No tool"))])
    host.handle_original = host.handle

    def mismatched(request):
        if request.url.path == "/api/usage/runs":
            rows = [run_rows("mm-1", 2, 200, cache_read=None)] if host.requests else []
            return httpx.Response(200, json={"items": rows})
        return host.handle_original(request)

    with httpx.Client(transport=httpx.MockTransport(mismatched)) as client:
        result = run_bench(client, BASE, "mm", [task("n", kind="none", expect={})])
    check = result["runs"][0]["host_usage"]
    assert not check["match"] and set(check["mismatches"]) == {"calls", "input", "output", "cache_read", "ms"}

    host = FakeHost([response(("usage", usage()), ("", "No tool"))], runs=401)
    with host.client() as client:
        result = run_bench(client, BASE, "na", [task("n", kind="none", expect={})])
    check = result["runs"][0]["host_usage"]
    assert check == {"available": False, "reason": "HTTP 401: admin_required - admin token"}


def test_the_cross_check_compares_roles_when_the_host_gives_by_role():
    from bench.runner import cross_check
    run = {"label": "r-1",
           "totals": {"calls": 3, "input": 300, "output": 60, "cache_read": None, "cache_write": 0, "ms": 30},
           "by_role": {"planner": {"calls": 2, "input": 200, "output": 40, "cache_read": None, "cache_write": 0,
                                   "ms": 20, "tool_calls": 1},
                       "supervisor": {"calls": 1, "input": 100, "output": 20, "cache_read": 0, "cache_write": 0,
                                      "ms": 10, "tool_calls": 0}}}
    part = {"role": "planner", "model": "big", "calls": 1, "input": 100, "output": 20, "cache_read": None,
            "cache_write": 0, "ms": 10}
    row = {"run_label": "r-1", "calls": 3, "input": 300, "output": 60, "cache_read": None, "cache_write": 0,
           "ms": 30, "by_role": [part, {**part, "model": "other", "cache_read": 5},
                                 {**part, "role": "supervisor", "cache_read": 0}]}
    check = cross_check(run, {"r-1": row}, None)
    # two models of the planner add up; one unknown cache count makes the planner's unknown
    assert check["match"], check["mismatches"]
    assert check["host_by_role"]["planner"] == {"calls": 2, "input": 200, "output": 40, "cache_read": None,
                                                "cache_write": 0, "ms": 20}
    row["by_role"][2] = {**part, "role": "supervisor", "cache_read": None}
    row["by_role"].append({**part, "role": "analyst"})
    check = cross_check(run, {"r-1": row}, None)
    assert check["mismatches"] == ["supervisor.cache_read", "analyst (only on the host)"]
    assert cross_check(run, {"r-1": {k: v for k, v in row.items() if k != "by_role"}}, None)["host_by_role"] is None


def test_a_label_the_host_already_has_is_refused_before_any_chat():
    host = FakeHost([], runs=[run_rows("taken-2", 1, 1)])
    with host.client() as client, pytest.raises(ValueError, match="taken-2"):
        run_bench(client, BASE, "taken", [task()], repeat=2)
    assert host.requests == []


# -- execute ------------------------------------------------------------------------------

def test_execute_confirms_runs_and_reports_the_real_result():
    host = FakeHost([response(("spec", spec()), ("usage", usage())), response(("usage", usage("supervisor")))])
    result = host.run([task()], execute=True)["tasks"][0]
    confirm, run = host.other
    assert confirm.url.path == "/api/v1/bridge/spec/confirm"
    assert json.loads(confirm.content)["spec"] == spec()["spec"]
    assert run.url.path == "/api/v1/bridge/tools/create_wall/run"
    assert json.loads(run.content) == {"params": {"height": 3000.0}, "token": "tok-1"}
    assert host.requests[1][0]["execution"] == {"success": True, "evidence_id": "ev-1"}
    assert result["execution"] == {"success": True, "evidence_id": "ev-1"} and result["correct"]


def test_execute_reports_a_failed_run_in_the_result_shape():
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))],
                    run_reply=httpx.Response(503, json={"error": "revit_unreachable", "message": "no Revit"}))
    result = host.run([task()], execute=True)["tasks"][0]
    assert host.requests[1][0]["execution"] == {"success": False, "error": "revit_unreachable", "message": "no Revit"}
    assert result["error"] is None


def test_a_report_answered_with_a_new_card_is_reported_again_at_most_three_times():
    """The default mode: a new accepted spec in the report's reply (the host's supervisor sent
    the task back) gets the fixed result again, as a designer confirming it would send it."""
    sent_back = response(("usage", usage("supervisor")), ("spec", spec()), ("usage", usage()))
    host = FakeHost([response(("spec", spec()), ("usage", usage())), sent_back, sent_back,
                     response(("usage", usage("supervisor")), ("", "Asked."))])
    result = host.run([task()], {"success": False, "error": "x"})["tasks"][0]
    assert result["feedback_turns"] == 3 and result["requests"] == 4 and result["correct"]
    assert [r[0].get("execution") for r in host.requests[1:]] == [{"success": False, "error": "x"}] * 3
    # A host that keeps sending it back is reported to three times, no more.
    host = FakeHost([response(("spec", spec()))] + [sent_back] * 3)
    result = host.run([task()])["tasks"][0]
    assert result["feedback_turns"] == 3 and len(host.requests) == 4 and result["error"] is None
    # A task's own execution goes back instead of the run's fixture.
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))])
    host.run([task(execution={"success": False, "error": "validation_failed"})], {"success": True})
    assert host.requests[1][0]["execution"] == {"success": False, "error": "validation_failed"}


def test_execute_reports_once_even_when_a_new_card_comes_back():
    sent_back = response(("usage", usage("supervisor")), ("spec", spec()))
    host = FakeHost([response(("spec", spec())), sent_back])
    result = host.run([task(execution={"success": False})], execute=True)["tasks"][0]
    assert result["feedback_turns"] == 1 and len(host.requests) == 2
    assert host.requests[1][0]["execution"] == {"success": True, "evidence_id": "ev-1"}     # the real result


def test_the_default_mode_never_confirms_or_runs():
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))])
    host.run([task()])
    assert host.other == []


# -- compare ------------------------------------------------------------------------------

def summary_task(task_id="a", correct=1, runs=1, tokens=100, skipped=None, failed=0, **fields):
    metrics = {field: {"median": value, "min": value, "max": value}
               for field, value in {"calls": 1, "input": tokens, "output": 10, "cache_read": None,
                                    "cache_write": 0, **fields}.items()}
    return {"id": task_id, "kind": "tool", "requires": [], "skipped": skipped, "runs": 0 if skipped else runs,
            "correct": correct, "failed": failed, "errors": [], "metrics": metrics, "by_role": {}}


def results(*tasks):
    return {"version": 2, "summary": {"tasks": list(tasks)}}


@pytest.mark.parametrize("old,new,status", [
    (summary_task(), summary_task(correct=0), "regression"),
    (summary_task(), summary_task(tokens=105), "ok"),
    (summary_task(), summary_task(tokens=106), "regression"),
    (summary_task(tokens=0), summary_task(tokens=1), "regression"),
    # unknown on one side or both: cannot say it did not get worse
    (summary_task(tokens=100), summary_task(tokens=None), "uncomparable"),
    (summary_task(tokens=None), summary_task(tokens=200), "uncomparable"),
    (summary_task(tokens=None), summary_task(tokens=None), "uncomparable"),
    # wrong both times: more tokens are not a regression, unknown is not looked at
    (summary_task(correct=0), summary_task(correct=0, tokens=200), "ok"),
    (summary_task(correct=0), summary_task(correct=0, tokens=None), "ok"),
    # repeats: the share of correct repeats
    (summary_task(correct=3, runs=3), summary_task(correct=2, runs=3), "regression"),
    (summary_task(correct=2, runs=3), summary_task(correct=3, runs=3, tokens=500), "ok"),
    (summary_task(), summary_task(skipped="requires basic-tools"), "uncomparable"),
    (summary_task(skipped="requires basic-tools"), summary_task(), "new"),
    (summary_task(skipped="x"), summary_task(skipped="x"), "skipped"),
])
def test_compare_rules(old, new, status):
    rows = compare_runs(results(old), results(new))
    assert rows[0]["status"] == status
    assert rows[0]["tokens"]["cache_read"]["delta"] is None


def test_compare_deltas_are_unknown_when_either_side_is():
    rows = compare_runs(results(summary_task(tokens=100)), results(summary_task(tokens=None)))
    assert rows[0]["tokens"]["input"] == {"before": 100, "after": None, "delta": None}
    assert rows[0]["reasons"] == ["input tokens unknown (after)"]
    rows = compare_runs(results(summary_task(tokens=100)), results(summary_task(tokens=90)))
    assert rows[0]["tokens"]["input"] == {"before": 100, "after": 90, "delta": -10}


def test_compare_missing_and_new_tasks():
    rows = compare_runs(results(summary_task()), results(summary_task("new")))
    assert rows[0]["status"] == "regression" and rows[0]["reasons"] == ["task missing"]
    assert rows[1]["status"] == "new"


@pytest.mark.parametrize("old,new,code", [
    (summary_task(), summary_task(), 0),
    (summary_task(), summary_task(correct=0), 1),
    (summary_task(tokens=100), summary_task(tokens=None), 3),
])
def test_compare_cli_exit_codes_and_table(tmp_path, capsys, old, new, code):
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path, value in zip(paths, (results(old), results(new))):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["compare", *map(str, paths)]) == code
    out = capsys.readouterr().out
    assert "Cache read" in out
    if code == 3:
        assert "uncomparable: input tokens unknown (after)" in out and "100 -> ?" in out


def test_compare_flags_runs_that_fed_results_back_differently(tmp_path, capsys):
    old, new = results(summary_task()), {**results(summary_task()), "feedback_policy": FEEDBACK_FOLLOWS}
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path, value in zip(paths, (old, new)):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["compare", *map(str, paths)]) == 3
    out = capsys.readouterr().out
    assert "NOTE: the runs fed execution results back differently: before one report per task" in out
    # The same policy: nothing to note.
    paths[0].write_text(json.dumps(new), encoding="utf-8")
    assert main(["compare", *map(str, paths)]) == 0
    assert "NOTE" not in capsys.readouterr().out


def test_a_run_records_its_feedback_policy():
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))])
    with host.client() as client:
        assert run_bench(client, BASE, "p", [task()])["feedback_policy"] == FEEDBACK_FOLLOWS
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))])
    with host.client() as client:
        assert run_bench(client, BASE, "q", [task()], execute=True)["feedback_policy"] == "one report per task"


def test_compare_exit_code_prefers_regression_over_uncomparable():
    rows = compare_runs(results(summary_task("a"), summary_task("b", tokens=100)),
                        results(summary_task("a", correct=0), summary_task("b", tokens=None)))
    assert [row["status"] for row in rows] == ["regression", "uncomparable"]
    assert compare_exit_code(rows) == 1


def test_compare_finds_labels_in_out(tmp_path, capsys):
    (tmp_path / "one.json").write_text(json.dumps(results(summary_task())), encoding="utf-8")
    (tmp_path / "two.json").write_text(json.dumps(results(summary_task())), encoding="utf-8")
    assert main(["compare", "one", "two", "--out", str(tmp_path)]) == 0
    (tmp_path / "old.json").write_text(json.dumps({"version": 1, "tasks": []}), encoding="utf-8")
    assert main(["compare", str(tmp_path / "old.json"), "two", "--out", str(tmp_path)]) == 2
    assert "version 2" in capsys.readouterr().err


# -- command line -----------------------------------------------------------------------------

@pytest.fixture
def cli_host(monkeypatch, tmp_path):
    """main() with httpx.Client routed to a FakeHost; the options the bench passed are kept."""
    monkeypatch.chdir(tmp_path)
    holder = {"host": FakeHost([]), "options": []}
    real_client = httpx.Client

    def client(**kwargs):
        holder["options"].append(kwargs)
        return real_client(transport=httpx.MockTransport(holder["host"].handle), **kwargs)

    monkeypatch.setattr(cli.httpx, "Client", client)
    tasks = tmp_path / "tasks.yml"
    tasks.write_text(json.dumps([task()]), encoding="utf-8")
    holder["tasks"] = tasks
    return holder


def test_cli_run_writes_bench_results_in_the_current_directory(cli_host, tmp_path, capsys):
    cli_host["host"].replies = [response(("spec", spec()), ("usage", usage())),
                                response(("usage", usage("supervisor")))]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"success": True, "result": "fixture"}), encoding="utf-8")
    assert main(["run", "--base-url", "http://127.0.0.1:7860", "--label", "sample",
                 "--tasks", str(cli_host["tasks"]), "--execution-fixture", str(fixture)]) == 0
    result = json.loads((tmp_path / "bench-results" / "sample.json").read_text(encoding="utf-8"))
    assert result["version"] == 2 and result["mode"] == "synthetic" and result["repeat"] == 1
    assert result["host"]["features"] == {"mode": "local"}
    assert cli_host["host"].requests[1][0]["execution"] == {"success": True, "result": "fixture"}
    assert cli_host["options"][0]["trust_env"] is False
    out = capsys.readouterr()
    assert "Model ms" in out.out and "Saved: " in out.out and "WARNING" not in out.err
    # the same label again: the file exists, nothing is sent
    sent = len(cli_host["host"].requests)
    assert main(["run", "--label", "sample", "--tasks", str(cli_host["tasks"])]) == 2
    assert len(cli_host["host"].requests) == sent


@pytest.mark.parametrize("base", [
    "http://192.168.1.5:7860", "http://example.com", "http://10.0.0.1", "https://revit.example/",
    "http://0.0.0.0:7860",
])
def test_cli_refuses_a_host_that_is_not_this_machine(cli_host, capsys, base):
    assert main(["run", "--base-url", base, "--label", "far", "--tasks", str(cli_host["tasks"])]) == 2
    assert "--allow-remote" in capsys.readouterr().err
    assert cli_host["options"] == []


@pytest.mark.parametrize("base", ["http://127.0.0.1:7860", "http://localhost:7860", "http://[::1]:7860",
                                  "http://127.0.0.2:9000"])
def test_cli_takes_loopback_addresses(cli_host, base):
    cli_host["host"].replies = [response(("spec", spec())), response(("", "ok"))]
    assert main(["run", "--base-url", base, "--label", "near", "--tasks", str(cli_host["tasks"])]) == 0


def test_cli_allow_remote(cli_host):
    cli_host["host"].replies = [response(("spec", spec())), response(("", "ok"))]
    assert main(["run", "--base-url", "http://192.168.1.5:7860", "--allow-remote", "--label", "far",
                 "--tasks", str(cli_host["tasks"])]) == 0
    assert cli_host["options"][0]["trust_env"] is False


def test_cli_execute_warns_and_is_off_by_default(cli_host, capsys):
    cli_host["host"].replies = [response(("spec", spec())), response(("", "ok"))]
    assert main(["run", "--label", "exe", "--execute", "--tasks", str(cli_host["tasks"])]) == 0
    err = capsys.readouterr().err
    assert "WARNING: --execute confirms every accepted spec and RUNS it" in err
    assert [request.url.path for request in cli_host["host"].other
            if request.method == "POST"] == ["/api/v1/bridge/spec/confirm", "/api/v1/bridge/tools/create_wall/run"]
    args = cli.build_parser().parse_args(["run", "--label", "x"])
    assert args.execute is False


@pytest.mark.parametrize("argv,message", [
    (["--label", "../invalid"], "--label"),
    (["--label", "CON"], "reserved device name"),
    (["--label", "nul.x"], "reserved device name"),
    (["--label", "a" * 63, "--repeat", "2"], "too long"),
    (["--label", "x", "--repeat", "0"], "--repeat"),
    (["--label", "x", "--skip-requires", "Basic Tools"], "--skip-requires"),
    (["--label", "x", "--execute", "--execution-fixture", "f.json"], "--execute"),
    (["--label", "x", "--timeout", "0"], "--timeout"),
    (["--label", "x", "--out", "results/NUL"], "reserved device"),
    (["--label", "x", "--out", "com1.d"], "reserved device"),
])
def test_cli_argument_errors(cli_host, capsys, argv, message):
    assert main(["run", "--tasks", str(cli_host["tasks"]), *argv]) == 2
    assert message in capsys.readouterr().err
    assert cli_host["host"].requests == []


def test_cli_skip_requires_and_repeat(cli_host, tmp_path, capsys):
    cli_host["tasks"].write_text(json.dumps([task("needs", requires=["basic-tools"]),
                                             task("n", kind="none", expect={})]), encoding="utf-8")
    cli_host["host"].replies = [response(("", "No tool"), ("usage", usage(input=10))),
                                response(("", "No tool"), ("usage", usage(input=30)))]
    assert main(["run", "--label", "sk", "--repeat", "2", "--skip-requires", "basic-tools,mentions",
                 "--tasks", str(cli_host["tasks"])]) == 0
    result = json.loads((tmp_path / "bench-results" / "sk.json").read_text(encoding="utf-8"))
    assert result["skip_requires"] == ["basic-tools", "mentions"]
    assert result["summary"]["tasks"][0]["skipped"].startswith("requires basic-tools")
    assert result["summary"]["tasks"][1]["metrics"]["input"] == {"median": 20, "min": 10, "max": 30}
    out = capsys.readouterr().out
    assert "needs: skipped, requires basic-tools" in out and "20 (10-30)" in out


def test_cli_prints_before_saving_and_falls_back_when_out_is_unwritable(cli_host, tmp_path, monkeypatch, capsys):
    cli_host["host"].replies = [response(("spec", spec())), response(("", "ok"))]
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    real_write = Path.write_text

    def failing(self, *args, **kwargs):
        if self.parent.name == "bench-results":
            raise OSError("disk full")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)
    assert main(["run", "--label", "fb", "--tasks", str(cli_host["tasks"])]) == 0
    out = capsys.readouterr()
    assert out.out.index("Task") < out.out.index("Saved: ")
    assert "disk full" in out.err
    assert (tmp_path / "tmp" / "revit-bridge-bench-fb.json").is_file()


# -- picks: mentions looked up by name ---------------------------------------------------

LEVELS = [{"kind": "level", "id": 311, "name": "F1", "label": "F1", "detail": "0 mm"},
          {"kind": "level", "id": 312, "name": "F2", "label": "F2", "detail": "3000 mm"},
          {"kind": "level", "id": 313, "name": "F2 mezzanine", "label": "F2 mezzanine", "detail": "4500 mm"}]
LINKS = [{"kind": "link", "id": 502, "name": "MEP.rvt : 1", "label": "MEP.rvt", "detail": "not loaded",
          "disabled": True, "reason": "not_loaded"},
         {"kind": "link", "id": 501, "name": "Structure.rvt : 1", "label": "Structure.rvt", "detail": "Structure"}]
TYPES = [{"kind": "type", "id": 401, "name": "Generic - 200mm", "label": "Basic Wall: Generic - 200mm",
          "detail": "Walls", "category": "OST_Walls", "family": "Basic Wall"}]


def test_picks_are_looked_up_and_sent_with_the_first_message_only():
    host = FakeHost([response(("", "Which height?")), response(("spec", spec())), response(("", "Done"))],
                    candidates={"level": LEVELS, "link": LINKS, "type": TYPES})
    picks = [{"kind": "level", "pick": {"name": "F2"}}, {"kind": "link", "pick": {"first": True}},
             {"kind": "type", "pick": {"name": "Generic - 200mm", "category": "OST_Walls"}},
             {"kind": "level", "id": 77}]
    result = host.run([task(kind="ask", answers=["3000"], mentions=picks)])["tasks"][0]
    sent = [{"kind": "level", "id": 312}, {"kind": "link", "id": 501}, {"kind": "type", "id": 401},
            {"kind": "level", "id": 77}]
    assert result["error"] is None and result["mentions"] == sent
    assert host.lookups == [{"kind": "level", "limit": "200", "q": "F2"}, {"kind": "link", "limit": "200"},
                            {"kind": "type", "limit": "200", "category": "OST_Walls", "q": "Generic - 200mm"}]
    assert host.requests[0][0]["mentions"] == sent                      # exactly F2, not the mezzanine
    assert "mentions" not in host.requests[1][0]                        # the answer: the session keeps them
    assert "mentions" not in host.requests[2][0]                        # the execution report


@pytest.mark.parametrize("pick, candidates, reason", [
    ({"kind": "level", "pick": {"name": "F9"}}, {"level": LEVELS}, "pick not found (level F9)"),
    ({"kind": "link", "pick": {"name": "MEP.rvt : 1"}}, {"link": LINKS}, "pick not found (link MEP.rvt : 1)"),
    ({"kind": "link", "pick": {"first": True}}, {"link": LINKS[:1]}, "pick not found (link first)"),
    ({"kind": "type", "pick": {"name": "Thick", "category": "OST_Walls"}}, {"type": TYPES},
     "pick not found (type Thick in OST_Walls)"),
])
def test_a_pick_that_finds_nothing_skips_the_task_unscored(pick, candidates, reason):
    host = FakeHost([response(("", "No tool"), ("usage", usage()))], candidates=candidates)
    run = host.run([task("picked", mentions=[pick]), task("plain", kind="none", expect={})])
    picked, plain = run["tasks"]
    assert picked == {"id": "picked", "kind": "tool", "requires": [], "skipped": reason, "correct": None,
                      "error": None}
    assert plain["correct"] and len(host.requests) == 1                 # only the next task chatted
    summary = summarize([run])
    assert summary["tasks"][0]["skipped"] == reason and summary["tasks"][0]["runs"] == 0
    assert summary["scored"]["median"] == 1 and summary["correct"]["median"] == 1


def test_a_pick_with_two_items_of_that_name_fails_the_task_before_any_chat():
    host = FakeHost([], candidates={"level": LEVELS[1:2] * 2})
    result = host.run([task(mentions=[{"kind": "level", "pick": {"name": "F2"}}])])["tasks"][0]
    assert result["correct"] is False and result["error"].startswith("ValueError")
    assert "2 items have that name" in result["error"]
    assert result["mentions"] is None and host.requests == [] and result["turns"] == 0


def test_a_pick_the_host_refuses_keeps_its_reason():
    host = FakeHost([], candidates={"level": httpx.Response(503, json={"error": "revit_unreachable",
                                                                       "message": "connection refused"})})
    result = host.run([task(mentions=[{"kind": "level", "pick": {"name": "F2"}}])])["tasks"][0]
    assert result["http_status"] == 503 and result["error"] == "HTTP 503: revit_unreachable - connection refused"
    assert host.requests == []
    answered = FakeHost([], candidates={"level": httpx.Response(200, json={"error": "revit_error", "kind": "level"})})
    failed = answered.run([task(mentions=[{"kind": "level", "pick": {"first": True}}])])["tasks"][0]
    assert "the host answered revit_error" in failed["error"]


def test_literal_ids_need_no_lookup_and_a_task_without_mentions_sends_none():
    host = FakeHost([response(("", "ok")), response(("", "ok"))])
    run = host.run([task("ids", kind="none", expect={}, mentions=[{"kind": "level", "id": 101}]),
                    task("plain", kind="none", expect={})])
    assert host.lookups == []
    assert host.requests[0][0]["mentions"] == [{"kind": "level", "id": 101}]
    assert "mentions" not in host.requests[1][0]
    assert run["tasks"][0]["mentions"] == [{"kind": "level", "id": 101}] and "mentions" not in run["tasks"][1]


def test_execute_confirms_with_the_chat_session():
    host = FakeHost([response(("spec", spec())), response(("", "Reported"))])
    host.run([task()], execute=True)
    confirm = host.other[0]
    assert json.loads(confirm.content) == {"spec": spec()["spec"], "confirmed_by": "token-bench",
                                           "session_id": "session-1"}
