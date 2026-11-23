"""The bench against the real app (local mode, in-process TestClient), the suite's scripted
model (tests/fake_model.py) and fake add-in (tests/fake_revit.py). No provider is called:
the model's HTTP client is routed to the script."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import config
from backend import llm as llm_module
from bench.runner import run_bench
from tests.conftest import _fresh_usage_option_memory, env, local_env, revit  # noqa: F401 - fixtures
from tests.fake_model import USAGE_SHAPES, FakeModel, text_turn, tool_turn

BASE = "http://127.0.0.1:7860"
WALL = {"level_name": "L1", "start_x": 0, "start_y": 0, "end_x": 5000, "end_y": 0, "height": 3000}
FULL = USAGE_SHAPES["openai_cache_write"][0]          # (12000, 80, 4000, 7000)
SMALL = USAGE_SHAPES["openrouter"][0]                 # (194, 2, 0, 100)
NO_CACHE = USAGE_SHAPES["gemini"][0]                  # (758, 967, None, None)
DEEPSEEK = USAGE_SHAPES["deepseek"][0]                # (1000, 50, 800, None)


def wall_spec() -> dict:
    return {"task": "run create_wall", "action": {"kind": "run_tool", "tool": "create_wall"},
            "parameters": [{"name": k, "value": v, "unit": "mm" if isinstance(v, (int, float)) else None,
                            "source": "answer", "evidence": f"q_{k}"} for k, v in WALL.items()]}


TASKS = [
    {"id": "levels", "kind": "query", "message": "List the levels.", "expect": {"max_turns": 1}},
    {"id": "wall", "kind": "tool", "message": "A wall on L1, 3000 high, (0,0) to (5000,0).",
     "expect": {"tool": "create_wall", "params": {"level_name": "L1", "height": 3000}, "max_turns": 3}},
    {"id": "pipes", "kind": "none", "message": "Sprinkler piping.", "expect": {"max_turns": 1}},
    {"id": "ask", "kind": "ask", "message": "A wall, (0,0) to (5000,0). Ask me the level.",
     "answers": ["L1"], "expect": {"tool": "create_wall", "params": {"level_name": "L1"}, "max_turns": 3}},
    {"id": "grid", "kind": "tool", "message": "Grid A.", "requires": ["basic-tools"],
     "expect": {"tool": "create_grid", "max_turns": 3}},
    {"id": "linked", "kind": "query", "message": "Levels of the link.", "mentions": [{"kind": "link", "id": 202}],
     "requires": ["mentions"], "placeholder_ids": True, "expect": {"max_turns": 1}},
]


def script(second_levels_usage: dict) -> list[dict]:
    """The model's turns for one pass over TASKS (grid and linked are skipped: no turns)."""
    return [
        # levels: a query, then the answer
        tool_turn(("query", {"kind": "levels"}), usage=FULL),
        text_turn("Two levels: L1 and L2.", usage=second_levels_usage),
        # wall: the spec, the reply, then the report on the execution result
        tool_turn(("propose_spec", {"spec": wall_spec()}), usage=DEEPSEEK),
        text_turn("The card is below.", usage=DEEPSEEK),
        text_turn("One wall was created.", usage=NO_CACHE),
        # pipes: no tool for it
        text_turn("No tool here does this; build one on the workbench.", usage=FULL),
        # ask: a question, the answer, the spec, the report
        text_turn("Which level?", usage=FULL),
        tool_turn(("propose_spec", {"spec": wall_spec()}), usage=FULL),
        text_turn("The card is below.", usage=FULL),
        text_turn("Done.", usage=FULL),
    ]


@pytest.fixture
def host(local_env, revit, monkeypatch):  # noqa: F811 - fixtures imported above
    """A local host whose server-side model is the script; returns (client, model, revit)."""
    local_env.setenv("LLM_BASE_URL", "https://model.example/v1")
    local_env.setenv("LLM_MODEL", "fake-1")
    local_env.setenv("LLM_API_KEY", "fake-key-not-real")
    local_env.setenv("CHAT_RATE_LIMIT", "1000")
    config.reset_settings()
    fake = FakeModel([])
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    from backend.main import create_app
    with TestClient(create_app(), base_url=BASE, client=("127.0.0.1", 50000)) as client:
        yield client, fake, revit


def test_a_full_run_with_repeats_against_the_host(host):
    client, model, revit = host
    model.turns += script(FULL) + script(SMALL)
    result = run_bench(client, BASE, "it", TASKS, repeat=2, skip_requires=["basic-tools"])

    first, second = result["runs"]
    assert [task["id"] for task in first["tasks"]] == ["levels", "wall", "pipes", "ask", "grid", "linked"]
    by_id = {task["id"]: task for task in first["tasks"]}
    assert by_id["grid"]["skipped"].startswith("requires basic-tools")
    assert by_id["linked"]["skipped"].startswith("placeholder ids")
    for task_id in ("levels", "wall", "pipes", "ask"):
        assert by_id[task_id]["correct"], (task_id, by_id[task_id]["error"])
        assert by_id[task_id]["error"] is None
    assert model.turns == []

    # the levels task: two calls, every count known
    levels = by_id["levels"]
    assert levels["usage"]["calls"] == 2 and levels["tool_calls"] == 1
    assert (levels["usage"]["input"], levels["usage"]["output"], levels["usage"]["cache_read"],
            levels["usage"]["cache_write"]) == (24000, 160, 8000, 14000)
    assert levels["by_role"]["planner"]["input"] == 24000 and levels["models"] == {"planner": ["fake-1"]}
    # the wall task: deepseek has no cache write, the report call no cache at all: unknown stays unknown
    wall = by_id["wall"]
    assert wall["feedback_turns"] == 1 and wall["requests"] == 2 and wall["usage"]["calls"] == 3
    assert wall["usage"]["input"] == 2758 and wall["usage"]["output"] == 1067
    assert wall["usage"]["cache_read"] is None and wall["usage"]["cache_write"] is None
    assert wall["actual_params"]["level_name"] == "L1"
    assert by_id["ask"]["turns"] == 2

    # the host's line for each label is what the events said, unknowns included
    for run in result["runs"]:
        assert run["host_usage"]["available"] and run["host_usage"]["match"], run["host_usage"]
        # per role as well: the host's by_role (role and model) summed per role
        assert set(run["host_usage"]["host_by_role"]) == {"planner"}
    assert first["totals"]["calls"] == 10 and first["totals"]["cache_read"] is None

    # repeats: the levels task's second call differed
    summary = {task["id"]: task for task in result["summary"]["tasks"]}
    assert summary["levels"]["correct"] == 2 and summary["levels"]["runs"] == 2
    assert summary["levels"]["metrics"]["input"] == {"median": 18097, "min": 12194, "max": 24000}
    assert summary["wall"]["metrics"]["cache_read"] == {"median": None, "min": None, "max": None}
    assert summary["grid"]["skipped"] and summary["grid"]["runs"] == 0
    assert second["totals"]["input"] == first["totals"]["input"] - 12000 + 194

    # nothing ran in Revit: the add-in saw no code but reads
    assert not any("Wall.Create" in str(request.get("params", {}).get("code", "")) for request in revit.requests)

    # every chat request carried its repeat's label and no model setting
    rows = client.get("/api/usage/runs").json()["items"]
    assert {row["run_label"] for row in rows} == {"it-1", "it-2"}


def test_execute_runs_the_pack_in_the_fake_addin(host):
    client, model, revit = host
    model.turns += [tool_turn(("propose_spec", {"spec": wall_spec()}), usage=FULL),
                    text_turn("The card is below.", usage=FULL),
                    text_turn("One wall was created; the validator agrees.", usage=FULL)]
    result = run_bench(client, BASE, "exe", [TASKS[1]], execute=True)
    task = result["runs"][0]["tasks"][0]
    assert result["mode"] == "execute"
    assert task["error"] is None and task["correct"]
    assert task["execution"]["success"] is True and task["execution"]["evidence_id"]
    assert any("Wall.Create" in str(request.get("params", {}).get("code", "")) for request in revit.requests)
    # the report turn got the real result
    reported = [m["content"] for m in model.requests[-1]["messages"] if m.get("role") == "user"][-1]
    assert task["execution"]["evidence_id"] in reported


def test_the_error_body_of_a_refused_chat_is_kept(host, local_env):  # noqa: F811
    client, model, revit = host
    local_env.delenv("LLM_BASE_URL")
    config.reset_settings()
    result = run_bench(client, BASE, "nomodel", [TASKS[2]])
    task = result["runs"][0]["tasks"][0]
    assert task["http_status"] == 400
    assert task["error"].startswith("HTTP 400: No model configured")
    assert task["usage"]["calls"] is None and not task["correct"]
    assert model.requests == []
