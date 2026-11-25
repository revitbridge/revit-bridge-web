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
                                           "cache_read", "cache_write", "ms", "created_at", "run_label", "mode"}
    assert one.json()["items"][0]["mode"] is None            # recorded without a mode
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


def test_absurd_counts_are_unknown_and_never_overflow(make_client, local_env):
    """A count beyond 2**53 is stored as unknown (the row is kept); a sum that would pass it
    is unknown too - the route answers, never 500 on SQLite's integer overflow."""
    client = make_client(**LOCAL)
    store = get_log_store()
    for _ in range(2):
        store.record_call(session_id="big", turn=1, round=1, role="planner", model="m",
                          usage={"input": 2 ** 62, "output": 5, "cache_read": -1, "cache_write": True},
                          ms=10, run_label="big")
    for _ in range(3):
        store.record_call(session_id="edge", turn=1, round=1, role="planner", model="m",
                          usage={"input": 2 ** 53, "output": 2 ** 51, "cache_read": 0, "cache_write": 1.5},
                          ms=1, run_label="edge")
    rows = client.get("/api/usage", params={"session_id": "big"}).json()["items"]
    assert len(rows) == 2                                   # kept, not dropped
    assert [(r["input"], r["output"], r["cache_read"], r["cache_write"]) for r in rows] == [(None, 5, None, None)] * 2
    runs = client.get("/api/usage/runs")
    assert runs.status_code == 200
    big, edge = runs.json()["items"]
    assert (big["calls"], big["input"], big["output"], big["ms"]) == (2, None, 10, 20)
    # 3 x 2**53 passes the limit: unknown. 3 x 2**51 does not: exact.
    assert (edge["input"], edge["output"], edge["cache_read"], edge["cache_write"]) == (None, 3 * 2 ** 51, 0, None)
    assert isinstance(edge["output"], int) and isinstance(edge["ms"], int)


# -- the runs route: per role and model -------------------------------------------------------------------

def test_runs_list_each_role_and_model(make_client, local_env):
    client = make_client(**LOCAL)
    store = get_log_store()
    calls = [("analyst", "small", {"input": 10, "output": 1, "cache_read": 0, "cache_write": None}),
             ("planner", "big", {"input": 100, "output": 20, "cache_read": 50, "cache_write": None}),
             ("planner", "big", {"input": 120, "output": None, "cache_read": 60, "cache_write": None}),
             ("supervisor", "small", {"input": 30, "output": 3, "cache_read": 0, "cache_write": 0})]
    for role, model, usage in calls:
        store.record_call(session_id="s", turn=1, round=1, role=role, model=model, usage=usage, ms=5,
                          run_label="roles-1")
    store.record_call(session_id="t", turn=1, round=1, role="planner", model=None,
                      usage={"input": 1, "output": 1, "cache_read": 1, "cache_write": 1}, ms=1, run_label="old")
    runs = client.get("/api/usage/runs").json()["items"]
    first, second = runs
    assert (first["calls"], first["input"], first["output"], first["cache_read"]) == (4, 260, None, 110)
    assert first["by_role"] == [
        {"role": "analyst", "model": "small", "calls": 1, "input": 10, "output": 1, "cache_read": 0,
         "cache_write": None, "ms": 5},
        {"role": "planner", "model": "big", "calls": 2, "input": 220, "output": None, "cache_read": 110,
         "cache_write": None, "ms": 10},
        {"role": "supervisor", "model": "small", "calls": 1, "input": 30, "output": 3, "cache_read": 0,
         "cache_write": 0, "ms": 5}]
    assert second["by_role"] == [{"role": "planner", "model": None, "calls": 1, "input": 1, "output": 1,
                                  "cache_read": 1, "cache_write": 1, "ms": 1}]
    # The export documents the new field next to the old ones.
    schema = client.app.openapi()["components"]["schemas"]
    assert "by_role" in schema["RunTotal"]["properties"] and "by_role" not in schema["RunTotal"].get("required", [])
    assert {"role", "model", "calls", "input", "output", "cache_read", "cache_write", "ms"} == set(
        schema["RolePart"]["properties"])
