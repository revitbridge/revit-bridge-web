from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bench.__main__ import load_tasks, main
from bench.metrics import accepted, compare_runs, equal_value, sum_usage
from bench.runner import DEFAULT_EXECUTION, run_tasks
from bench.sse import iter_events, parse_frame


def frame(event, value):
    return (f"event: {event}\n" if event else "") + "data: " + json.dumps(value) + "\n\n"


def usage(role="planner", **changes):
    return {"role": role, "model": "fixture-model", "round": 1, "input": 100,
            "output": 20, "cache_read": 40, "cache_write": 0, "ms": 10, **changes}


def spec(**changes):
    return {"spec": {"action": {"kind": "run_tool", "tool": "create_wall"},
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
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.sessions = 0

    def handle(self, request):
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
        if isinstance(reply, int):
            return httpx.Response(reply)
        return httpx.Response(200, headers={"X-Session-Id": sid, "Content-Type": "text/event-stream"},
                              stream=Fragments(reply))

    def run(self, tasks, fixture=None):
        with httpx.Client(transport=httpx.MockTransport(self.handle)) as client:
            return run_tasks(client, "http://testserver", "test-label", tasks, fixture)


def response(*events):
    return "".join(frame(name, value) for name, value in events) + "event: done\n\n"


def test_sse_comments_multiline_empty_unknown_and_done():
    lines = [":comment", "", "", "event: future", "data: not-json", "", "event: usage",
             'data: {"input": 10,', 'data: "output": null}', "", "event: done", "", 'data: "ignored"', ""]
    assert list(iter_events(lines)) == [{"type": "usage", "input": 10, "output": None}, {"type": "done"}]
    assert parse_frame(["data: [DONE]"]) == {"type": "done"}
    assert parse_frame(["event: done"]) == {"type": "done"}
    assert list(iter_events(['data: "last"'])) == [{"type": "text", "text": "last"}]
    with pytest.raises(ValueError, match="Invalid JSON"):
        parse_frame(["event: usage", "data: broken"])


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
    assert result == {"input": None, "output": 40, "cache_read": None,
                      "cache_write": 0, "ms": 20, "model_calls": 2}
    assert sum_usage([])["input"] is None
    assert sum_usage([usage(input=0)])["input"] == 0


def test_fragmented_stream_feedback_mentions_and_role_totals():
    tool = {"id": "q1", "role": "planner", "name": "query", "args": {"kind": "levels"}, "status": "running"}
    host = FakeHost([
        response(("stage", {"role": "planner", "model": "fixture-model", "status": "start"}),
                 ("tool", tool), ("tool", {**tool, "status": "ok", "ms": 1}),
                 ("usage", usage()), ("spec", spec()), ("", "\u5899\u5df2\u8ba1\u5212")),
        response(("usage", usage("supervisor", input=None)), ("", "Synthetic feedback checked")),
    ])
    fixture = {"success": True, "result": {"fixture": 5}}
    result = host.run([task(mentions=[{"kind": "level", "id": 10}])], fixture)["tasks"][0]
    assert result["correct"] and result["params_correct"]
    assert result["turns"] == 1 and result["requests"] == 2 and result["feedback_turns"] == 1
    assert result["model_calls"] == 2 and result["tool_calls"] == 1
    assert result["tools"][0]["status"] == "ok"
    assert result["usage"]["input"] is None and result["usage"]["output"] == 40
    assert result["by_role"]["planner"]["input"] == 100
    assert result["by_role"]["supervisor"]["input"] is None
    assert host.requests[0][0]["mentions"] == [{"kind": "level", "id": 10}]
    assert host.requests[0][0]["bridge"] is True
    assert host.requests[1][0] == {"execution": fixture, "session_id": "session-1"}
    assert host.requests[0][1]["X-Run-Label"] == "test-label"


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
    assert result["model_calls"] is None
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


def test_query_needs_successful_read_tool_and_no_accepted_spec():
    query = {"id": "q", "role": "analyst", "name": "query", "status": "ok", "args": {}}
    host = FakeHost([response(("tool", query), ("", "Found two levels")), response(("", "Found two levels"))])
    result = host.run([task("yes", kind="query", expect={}), task("no", kind="query", expect={})])["tasks"]
    assert result[0]["correct"] and not result[1]["correct"]


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


def saved(task_id="a", correct=True, tokens=100):
    return {"id": task_id, "correct": correct,
            "usage": {"input": tokens, "output": 10, "cache_read": None, "cache_write": 0}}


@pytest.mark.parametrize("old,new,regression", [
    (saved(), saved(correct=False), True), (saved(), saved(tokens=105), False),
    (saved(), saved(tokens=106), True), (saved(tokens=0), saved(tokens=1), True),
    (saved(tokens=None), saved(tokens=200), False), (saved(correct=False), saved(tokens=200), False),
])
def test_compare_rules_and_cli_exit_codes(tmp_path, capsys, old, new, regression):
    before, after = {"tasks": [old]}, {"tasks": [new]}
    rows = compare_runs(before, after)
    assert rows[0]["regression"] is regression
    assert rows[0]["tokens"]["cache_read"]["delta"] is None
    paths = [tmp_path / "a.json", tmp_path / "b.json"]
    for path, value in zip(paths, (before, after)):
        path.write_text(json.dumps(value), encoding="utf-8")
    assert main(["compare", *map(str, paths)]) == int(regression)
    assert "Cache read" in capsys.readouterr().out


def test_compare_missing_and_new_tasks():
    rows = compare_runs({"tasks": [saved()]}, {"tasks": [saved("new")]})
    assert rows[0]["regression"] and rows[0]["reasons"] == ["task missing"]
    assert not rows[1]["regression"]


def test_example_tasks_and_validation(tmp_path):
    tasks = load_tasks(Path(__file__).parents[1] / "tasks.yml")
    assert len(tasks) == 12
    assert sum(bool(task.get("mentions")) for task in tasks) == 2
    path = tmp_path / "tasks.yml"
    for data in ([], [task(), task()], [task(kind="invalid")], [task(expect={"max_turns": 0})],
                 [task(answers="answer")], [task(mentions=[{"kind": "level", "id": True}])]):
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            load_tasks(path)


def test_cli_run_outputs_only_outside_repo_by_default(tmp_path, monkeypatch, capsys):
    import bench.__main__ as cli
    host = FakeHost([response(("spec", spec())), response(("usage", usage("supervisor")))])
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(host.handle), **kwargs))
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path))
    tasks = tmp_path / "tasks.yml"
    tasks.write_text(json.dumps([task()]), encoding="utf-8")
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"success": True, "result": "fixture"}), encoding="utf-8")
    assert main(["run", "--base", "http://testserver", "--label", "sample", "--tasks", str(tasks),
                 "--execution-fixture", str(fixture)]) == 0
    result = json.loads((tmp_path / "revit-bridge-bench" / "sample.json").read_text())
    assert result["synthetic_execution"]
    assert host.requests[1][0]["execution"] == {"success": True, "result": "fixture"}
    assert "Turns+feedback" in capsys.readouterr().out
    assert main(["run", "--base", "http://testserver", "--label", "../invalid"]) == 2
