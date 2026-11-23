"""``GET /api/usage`` and ``/api/usage/runs``: the model calls read back, unknown kept unknown,
behind the same admin rule as ``/api/logs``."""
from __future__ import annotations

from backend.log_store import InteractionLogStore, get_log_store
from tests.test_deploy_mode import LOCAL


def _seed(store: InteractionLogStore) -> None:
    calls = [
        ("s1", 1, 1, {"input": 100, "output": 10, "cache_read": 0, "cache_write": None}, "run-a"),
        ("s1", 1, 2, {"input": 120, "output": 5, "cache_read": 100, "cache_write": None}, "run-a"),
        ("s2", 1, 1, {"input": None, "output": 7, "cache_read": None, "cache_write": None}, "run-b"),
        ("s2", 2, 1, {"input": 50, "output": 3, "cache_read": 0, "cache_write": 0}, "run-b"),
        ("s3", 1, 1, {"input": 9, "output": 9, "cache_read": 9, "cache_write": 9}, None),
    ]
    for session_id, turn, round_no, usage, label in calls:
        store.record_call(session_id=session_id, turn=turn, round=round_no, role="planner", model="m",
                          usage=usage, ms=10, run_label=label)


def test_usage_routes_keep_unknown_unknown(make_client, local_env):
    client = make_client(**LOCAL)                    # a local host: no password
    _seed(get_log_store())
    one = client.get("/api/usage", params={"session_id": "s2"})
    assert one.status_code == 200
    assert one.json()["session_id"] == "s2"
    assert [(r["turn"], r["round"], r["input"], r["output"]) for r in one.json()["items"]] == [(1, 1, None, 7), (2, 1, 50, 3)]
    assert set(one.json()["items"][0]) == {"session_id", "turn", "round", "role", "model", "input", "output",
                                           "cache_read", "cache_write", "ms", "created_at", "run_label"}
    assert client.get("/api/usage", params={"session_id": "nope"}).json()["items"] == []
    assert client.get("/api/usage").status_code == 422

    runs = client.get("/api/usage/runs").json()["items"]
    assert [r["run_label"] for r in runs] == ["run-a", "run-b"]         # unlabelled calls are not a run
    a, b = runs
    assert (a["calls"], a["input"], a["output"], a["cache_read"], a["cache_write"], a["ms"]) == (2, 220, 15, 100, None, 20)
    # One call without an input count makes the run's input unknown, not 50.
    assert (b["calls"], b["input"], b["output"], b["cache_read"], b["cache_write"]) == (2, None, 10, None, None)
    assert a["first_at"] <= a["last_at"]


def test_usage_routes_follow_the_logs_admin_rule(make_client, env):
    env.setenv("ADMIN_PASSWORD", "hunter2")
    client = make_client()
    assert client.get("/api/usage", params={"session_id": "s1"}).status_code == 403
    assert client.get("/api/usage/runs").status_code == 403
    assert client.get("/api/usage/runs", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert client.get("/api/usage/runs", headers={"X-Admin-Token": "hunter2"}).json() == {"items": []}


def test_the_export_documents_the_usage_routes_and_the_run_label(client):
    spec = client.get("/openapi.json").json()
    assert {"/api/usage", "/api/usage/runs"} <= set(spec["paths"])
    chat = spec["paths"]["/api/chat"]["post"]
    assert [(p["name"], p["in"], p["required"]) for p in chat["parameters"]] == [("x-run-label", "header", False)]
    row = spec["components"]["schemas"]["CallRow"]["properties"]
    assert {"input", "output", "cache_read", "cache_write"} <= set(row)
