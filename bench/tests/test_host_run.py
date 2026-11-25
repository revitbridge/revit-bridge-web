"""The bench against the real app (local mode, in-process TestClient), the suite's scripted
model (tests/fake_model.py) and fake add-in (tests/fake_revit.py). No provider is called:
the model's HTTP client is routed to the script."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import config
from backend import llm as llm_module
from backend.api.model_tools import WORKBENCH_LINK
from bench.__main__ import load_tasks
from bench.runner import run_bench
from tests.conftest import _fresh_usage_option_memory, env, local_env, revit  # noqa: F401 - fixtures
from tests.fake_model import USAGE_SHAPES, FakeModel, RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.fake_revit import FakeRevit

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


def test_a_level_picked_by_name_reaches_the_model_and_its_spec_runs(host):
    """The pick is looked up on the host (GET /mentions), checked by the host when the task's
    message goes out, the model binds it as the package says, and with --execute the
    confirmation carries the session, so the host accepts the binding and the pack runs."""
    client, model, revit = host
    spec = wall_spec()
    for binding in spec["parameters"]:
        if binding["name"] == "level_name":
            binding.update(value="L2", unit=None, source="tool", evidence="mention:level#2")
    model.turns += [tool_turn(("propose_spec", {"spec": spec}), usage=FULL),
                    text_turn("The card is below.", usage=FULL),
                    text_turn("One wall was created on L2.", usage=FULL)]
    picked = {"id": "picked", "kind": "tool", "message": "A wall on the picked level, (0,0) to (5000,0).",
              "mentions": [{"kind": "level", "pick": {"name": "L2"}}],
              "expect": {"tool": "create_wall", "params": {"level_name": "L2", "height": 3000}, "max_turns": 3}}
    result = run_bench(client, BASE, "picked", [picked], execute=True)
    task = result["runs"][0]["tasks"][0]
    assert task["error"] is None and task["correct"], task["error"]
    assert task["mentions"] == [{"kind": "level", "id": 2}]
    assert task["execution"]["success"] is True
    # The model saw the package's block after the task's message, and accepted the binding.
    first_user = model.requests[0]["messages"][-1]["content"]
    assert first_user.startswith(picked["message"] + "\n\nDesigner's selections") and 'level "L2" (id 2' in first_user
    assert model.tool_results("propose_spec")[0]["accepted"] is True


def test_a_level_the_model_does_not_have_skips_the_task(host):
    client, model, revit = host
    missing = {"id": "missing", "kind": "tool", "message": "A wall on F9.",
               "mentions": [{"kind": "level", "pick": {"name": "F9"}}],
               "expect": {"tool": "create_wall", "max_turns": 3}}
    result = run_bench(client, BASE, "missing", [missing])
    task = result["runs"][0]["tasks"][0]
    assert task["skipped"] == "pick not found (level F9)" and task["correct"] is None
    assert model.requests == []


# -- the roles with the analyst (ROLES_ANALYST=on; the default is planner + supervisor) ----------

ROUTE_TOOL = {"route": "tool", "tool": "create_wall", "facts_needed": [], "note": ""}
ROUTE_QUERY = {"route": "query", "facts_needed": [], "note": ""}
ROUTE_NONE = {"route": "none", "facts_needed": [], "note": "No tool pipes sprinklers."}
REPORT_DONE = {"verdict": "done", "report": "One wall; the validator passed."}


@pytest.fixture
def roles_host(local_env, revit, monkeypatch):  # noqa: F811 - fixtures imported above
    """A local host with the three roles (ROLES_ANALYST=on), on the one LLM_* model; returns
    (client, model, revit) with a model that answers each role from its own script."""
    local_env.delenv("LLM_SINGLE_MODEL")
    local_env.setenv("ROLES_ANALYST", "on")
    local_env.setenv("LLM_BASE_URL", "https://model.example/v1")
    local_env.setenv("LLM_MODEL", "fake-1")
    local_env.setenv("LLM_API_KEY", "fake-key-not-real")
    local_env.setenv("CHAT_RATE_LIMIT", "1000")
    config.reset_settings()
    fake = RoleScriptedModel()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    from backend.main import create_app
    with TestClient(create_app(), base_url=BASE, client=("127.0.0.1", 50000)) as client:
        yield client, fake, revit


def test_a_run_under_the_roles_records_each_role_and_reaches_the_supervisor(roles_host):
    client, model, revit = roles_host
    # levels: the analyst routes, the planner reads and answers
    model.script("analyst", json_turn(ROUTE_QUERY, usage=SMALL))
    model.script("planner", tool_turn(("query", {"kind": "levels"}), usage=FULL), text_turn("L1, L2.", usage=FULL))
    # wall: the analyst routes, the planner proposes, the supervisor reports the fixed result
    model.script("analyst", json_turn(ROUTE_TOOL, usage=SMALL))
    model.script("planner", tool_turn(("propose_spec", {"spec": wall_spec()}), usage=DEEPSEEK),
                 text_turn("The card is below.", usage=DEEPSEEK))
    model.script("supervisor", json_turn(REPORT_DONE, usage=NO_CACHE))
    # pipes: the analyst alone
    model.script("analyst", json_turn(ROUTE_NONE, usage=SMALL))
    result = run_bench(client, BASE, "roles", TASKS[:3])
    run = result["runs"][0]
    by_id = {task["id"]: task for task in run["tasks"]}
    for task_id in ("levels", "wall", "pipes"):
        assert by_id[task_id]["correct"], (task_id, by_id[task_id]["error"])
    assert model.left() == {}
    assert result["chat"] == {"host": "roles_analyst", "modes_seen": ["roles_analyst"],
                              "roles_seen": ["analyst", "planner", "supervisor"]}
    assert result["host"]["features"]["roles"] is True and result["chat_mode"] == "auto"
    # per role, from the usage events
    wall = by_id["wall"]
    assert wall["feedback_turns"] == 1 and wall["usage"]["calls"] == 4
    assert list(wall["by_role"]) == ["analyst", "planner", "supervisor"]
    assert wall["by_role"]["analyst"]["input"] == 194 and wall["by_role"]["planner"]["input"] == 2000
    assert wall["by_role"]["supervisor"]["calls"] == 1 and wall["by_role"]["supervisor"]["cache_read"] is None
    assert wall["models"] == {"analyst": ["fake-1"], "planner": ["fake-1"], "supervisor": ["fake-1"]}
    assert wall["text"].endswith("One wall; the validator passed.")
    pipes = by_id["pipes"]
    assert pipes["usage"]["calls"] == 1 and list(pipes["by_role"]) == ["analyst"]
    assert pipes["text"] == f"No tool pipes sprinklers.\n\n{WORKBENCH_LINK}"
    assert by_id["levels"]["by_role"]["planner"]["tool_calls"] == 1
    # the host's line agrees, role by role
    check = run["host_usage"]
    assert check["match"], check
    assert set(check["host_by_role"]) == {"analyst", "planner", "supervisor"}
    assert run["by_role"]["analyst"]["calls"] == 3 and run["by_role"]["supervisor"]["calls"] == 1
    summary = result["summary"]
    assert set(summary["by_role"]) == {"analyst", "planner", "supervisor"}
    # a task with no supervisor call counts it as zero calls, not unknown
    assert {t["id"]: t["by_role"]["supervisor"]["calls"]["median"] for t in summary["tasks"]
            if t["runs"] and "supervisor" in t["by_role"]} == {"wall": 1}


def test_a_task_sent_back_is_reported_again_until_the_host_asks(roles_host):
    client, model, revit = roles_host
    supervisor_tasks = load_tasks(Path(__file__).resolve().parents[1] / "tasks-supervisor.yml")
    failed = {**supervisor_tasks[0], "expect": {"tool": "create_wall", "params": {"level_name": "L1"}, "max_turns": 3}}
    model.script("analyst", json_turn(ROUTE_TOOL))
    model.script("planner", tool_turn(("propose_spec", {"spec": wall_spec()})), text_turn("card"))
    for attempt in (1, 2):
        model.script("supervisor", json_turn({"verdict": "retry", "report": f"Not counted ({attempt}).",
                                              "fix": "check the level"}))
        model.script("planner", tool_turn(("propose_spec", {"spec": wall_spec()})), text_turn(f"card {attempt}"))
    model.script("supervisor", json_turn({"verdict": "retry", "report": "Still not counted.", "fix": "again"}))
    result = run_bench(client, BASE, "sentback", [failed])
    task = result["runs"][0]["tasks"][0]
    assert task["error"] is None and task["correct"]
    assert task["feedback_turns"] == 3 and task["requests"] == 4
    assert model.left() == {}
    assert task["by_role"]["supervisor"]["calls"] == 3 and task["by_role"]["planner"]["calls"] == 6
    assert "sent back 2 times" in task["text"]
    # every report carried the task's own result, not the run's fixture
    reports = [json.loads(m["content"].split("\n", 1)[1]) for body in model.requests_of("supervisor")
               for m in body["messages"] if m["role"] == "user"]
    assert [r["execution"]["error"] for r in reports] == ["validation_failed"] * 3
    assert [r["sent_back_so_far"] for r in reports] == [0, 1, 2]


def test_the_supervisor_tasks_file_is_valid():
    tasks = load_tasks(Path(__file__).resolve().parents[1] / "tasks-supervisor.yml")
    assert [t["id"] for t in tasks] == ["wall-validator-failed", "wall-refused"]
    assert tasks[0]["execution"]["validation"]["passed"] is False and tasks[1]["execution"]["refused"] is True


def test_the_one_loop_is_labelled_as_such(host):
    client, model, revit = host
    model.turns += [text_turn("No tool here does this; build one on the workbench.", usage=FULL)]
    result = run_bench(client, BASE, "single", [TASKS[2]])
    assert result["chat"] == {"host": "single", "modes_seen": ["single"], "roles_seen": ["planner"]}


# -- the tasks of the packs revit-bridge 0.9.1 added ---------------------------------------------

TASKS_FILE = Path(__file__).resolve().parents[1] / "tasks.yml"
SUPERVISOR_FILE = Path(__file__).resolve().parents[1] / "tasks-supervisor.yml"
NUMBER_TYPES = {"double", "float", "number", "integer", "int"}


def expected_spec(task: dict) -> dict:
    """The spec a model that read the task right proposes: the expected tool and values."""
    tool, params = task["expect"]["tool"], task["expect"]["params"]
    return {"task": task["message"], "action": {"kind": "run_tool", "tool": tool},
            "parameters": [{"name": k, "value": v, "unit": "mm" if isinstance(v, (int, float)) else None,
                            "source": "designer", "evidence": task["message"]} for k, v in params.items()]}


def test_every_expected_tool_and_value_fits_the_installed_packs(tmp_path):
    """Each task's expected tool is a pack of the installed package, each expected parameter
    one of its parameters, with a value of its type (numbers for lengths, text for names)."""
    from revit_bridge.capabilities import ToolStore

    store = ToolStore(tmp_path / "capabilities")                # no user packs: the wheel's only
    tasks = load_tasks(TASKS_FILE) + load_tasks(SUPERVISOR_FILE)
    checked = 0
    for task in tasks:
        tool = task.get("expect", {}).get("tool")
        if tool is None:
            continue
        pack = store.load(tool)
        assert pack is not None, (task["id"], tool)
        declared = {p["name"]: p for p in pack.parameters}
        for name, value in task["expect"].get("params", {}).items():
            assert name in declared, (task["id"], tool, name, sorted(declared))
            if declared[name]["type"] in NUMBER_TYPES:
                assert type(value) in (int, float), (task["id"], name, value)
            else:
                assert isinstance(value, str), (task["id"], name, value)
            checked += 1
    assert checked >= 15


def test_v2_tasks_bind_the_current_model_items_and_account_for_usage(roles_host, monkeypatch):
    from backend import roles
    from tests.conftest import builtin_pack_names
    from tests import test_mentions as picker
    from tests.fake_revit import SNAPSHOT_RESULT

    client, model, revit = roles_host
    types = [dict(picker.TYPE_ROWS[0], FamilyTypeId=401, TypeName="OST_Walls-TypeA"),
             dict(picker.TYPE_ROWS[0], FamilyTypeId=702, FamilyName="Fixed", TypeName="OST_Windows-TypeA",
                  Category="Windows", BuiltInCategory="OST_Windows")]
    monkeypatch.setattr(picker, "TYPE_ROWS", types)
    monkeypatch.setitem(SNAPSHOT_RESULT["CategoryNames"], "OST_Windows", "Windows")
    wall = {"id": 981, "name": "Basic wall", "category": "OST_Walls", "shown": "Walls", "level": "L1"}
    revit.handler = picker.picker_handler({"levels": picker.LEVEL_ROWS, "calls": [], "selection": [wall], "elements": [wall]})
    tasks = [t for t in load_tasks(TASKS_FILE) if t["id"] in {"wall-by-type", "wall-from-selection", "window-along-wall"}]
    for task in tasks:
        spec = expected_spec(task)
        values = {"reference_id": (981, "981"), "wall_id": (981, "981"), "level_name": (1, "L1"),
                  "type_name": (702, "OST_Windows-TypeA") if task["id"] == "window-along-wall" else (401, "OST_Walls-TypeA")}
        for name, index in task["expect"]["pick_params"].items():
            pick = task["mentions"][index]
            number, value = values[name]
            spec["parameters"].append({"name": name, "value": value,
                                       "source": "designer" if name == "reference_id" else "tool",
                                       "evidence": f"mention:{pick['kind']}#{number}"})
        model.script("analyst", json_turn(dict(ROUTE_TOOL, tool=spec["action"]["tool"]), usage=SMALL))
        model.script("planner", tool_turn(("list_tools", {}), usage=FULL),
                     tool_turn(("propose_spec", {"spec": spec}), usage=FULL), text_turn("Card.", usage=FULL))
        model.script("supervisor", json_turn(REPORT_DONE, usage=DEEPSEEK))

    result = run_bench(client, BASE, "v2-picks", tasks, mode="roles_analyst")["runs"][0]
    assert len(result["tasks"]) == 3
    for task in result["tasks"]:
        assert task["correct"], (task["id"], task.get("error"), task.get("events"), task.get("skipped"))
        assert task["feedback_turns"] == 1 and task["usage"]["calls"] == 5
    inventory = builtin_pack_names()
    for request in model.requests_of("analyst"):
        rows = request["messages"][-1]["content"].split("Tools:\n", 1)[1].split("\n\nDigest of the open model:", 1)[0].splitlines()
        assert [row.removeprefix("- ").split(": ", 1)[0] for row in rows] == sorted(inventory)
        assert all(row.partition(": ")[2] for row in rows)
    for catalog in model.tool_results("list_tools"):
        assert {tool["name"] for tool in catalog} == inventory
    assert model.requests_of("planner")[0]["tools"] == roles.ROUTE_DEFINITIONS["tool"]
    assert result["host_usage"]["match"], result["host_usage"]
    assert [task["actual_params"].get("wall_id") for task in result["tasks"]] == [None, None, "981"]


PACK_CODE = ("Level.Create(", "Grid.Create(", "ElementTransformUtils.MoveElements(")


def counting(handler):
    """The fake add-in's count probe answers 1 + the runs of PACK_CODE so far (the suite's
    model_handler answers 1 before any run and 2 after): in a sequence each run moves it by one."""
    runs = [0]

    def answer(request: dict) -> dict:
        code = str(request.get("params", {}).get("code", ""))
        if request.get("method") == "send_code_to_revit":
            if "GetElementCount" in code and "Enum.Parse(typeof(BuiltInCategory)" in code:
                return FakeRevit.code_result(request.get("id"), 1 + runs[0])
            if any(marker in code for marker in PACK_CODE):
                runs[0] += 1
        return handler(request)
    return answer


def test_the_new_pack_tasks_run_in_the_fake_addin(host):
    """new-level, new-grid and move-selection as written: the model proposes what the task
    expects, the host accepts the spec against the real packs, --execute confirms and runs each
    in the fake add-in (its count probe moves by one per run, for the count_delta validators of
    create_level and create_grid; move_elements has none), and each scores correct."""
    client, model, revit = host
    revit.handler = counting(revit.handler)
    tasks = [t for t in load_tasks(TASKS_FILE) if t["id"] in ("new-level", "new-grid", "move-selection")]
    assert [t["expect"]["tool"] for t in tasks] == ["create_level", "create_grid", "move_elements"]
    for task in tasks:
        model.turns += [tool_turn(("propose_spec", {"spec": expected_spec(task)}), usage=FULL),
                        text_turn("The card is below.", usage=FULL),
                        text_turn("Done.", usage=FULL)]
    result = run_bench(client, BASE, "new-packs", tasks, execute=True)
    by_id = {task["id"]: task for task in result["runs"][0]["tasks"]}
    for task_id in ("new-level", "new-grid", "move-selection"):
        assert by_id[task_id]["skipped"] is None and by_id[task_id]["error"] is None, by_id[task_id]
        assert by_id[task_id]["correct"], (task_id, by_id[task_id]["param_checks"])
        assert by_id[task_id]["execution"]["success"] is True and by_id[task_id]["execution"]["evidence_id"]
    assert by_id["new-level"]["execution"]["validation"]["validator"] == "count_delta"
    assert by_id["new-grid"]["execution"]["validation"]["validator"] == "count_delta"
    assert by_id["move-selection"]["execution"]["validation"] is None
    assert model.tool_results("propose_spec") and all(v["accepted"] for v in model.tool_results("propose_spec"))
    sent = [str(r.get("params", {}).get("code", "")) for r in revit.requests]
    for marker in PACK_CODE:
        assert len([code for code in sent if marker in code]) == 1, marker


def test_the_refused_fixture_has_the_run_route_s_shape(env):  # noqa: F811 - fixture imported above
    """tasks-supervisor.yml feeds a refused run back without running anything: its execution has
    the keys and the fixed values of what the run route answers for one (tests/test_refused.py:
    the add-in's own reply for a pack that threw its refusal, through the package)."""
    from backend.main import create_app
    from tests.test_refused import REFUSED_KEYS, refusing_addin, run_refused

    with FakeRevit(refusing_addin()) as fake:
        env.setenv("REVIT_BRIDGE_PORT", str(fake.port))
        with TestClient(create_app()) as client:
            out, _ = run_refused(client)
    fixture = next(t for t in load_tasks(SUPERVISOR_FILE) if t["id"] == "wall-refused")["execution"]
    assert list(fixture) == list(out) and set(out) == REFUSED_KEYS          # the same keys, in its order
    for key in ("success", "result", "validation", "preconditions_failed", "refused"):
        assert fixture[key] == out[key], key
    assert isinstance(fixture["error"], str) and not fixture["error"].startswith("REFUSED")


# -- the chat mode and --confirm once against the host ------------------------------------------------

WALL_TASK = {"id": "wall", "kind": "tool", "message": "A wall on L1, 3000 high, (0,0) to (5000,0).",
             "expect": {"tool": "create_wall", "params": {"level_name": "L1", "height": 3000}, "max_turns": 3}}
LEVELS_TASK = {"id": "levels", "kind": "query", "message": "List the levels.", "expect": {"max_turns": 1}}


def test_the_mode_reaches_the_host_and_its_record(host):
    """A local host (its default here: the one loop) runs the mode the bench asks for in every
    request - planner + supervisor: the report goes to the supervisor - and the host records it."""
    client, model, revit = host
    model.turns += [tool_turn(("propose_spec", {"spec": wall_spec()}), usage=FULL),
                    text_turn("The card is below.", usage=FULL),
                    json_turn({"verdict": "done", "report": "One wall."}, usage=FULL),
                    tool_turn(("query", {"kind": "levels"}), usage=SMALL), text_turn("L1, L2.", usage=SMALL)]
    result = run_bench(client, BASE, "moded", [WALL_TASK, LEVELS_TASK], mode="roles")
    tasks = {task["id"]: task for task in result["runs"][0]["tasks"]}
    assert tasks["wall"]["correct"] and tasks["levels"]["correct"], [t["error"] for t in tasks.values()]
    assert model.turns == []
    assert result["chat_mode"] == "roles"
    assert result["chat"] == {"host": "single", "modes_seen": ["roles"], "roles_seen": ["planner", "supervisor"]}
    assert tasks["wall"]["modes"] == ["roles"] and tasks["wall"]["text"].endswith("One wall.")
    assert all(body.get("mode") is None for body in model.requests)          # the mode never reaches the model
    runs = {row["run_label"]: row for row in client.get("/api/usage/runs").json()["items"]}
    assert runs["moded-1"]["mode"] == "roles"
    assert result["runs"][0]["host_usage"]["match"]


def test_mode_single_on_a_host_with_the_analyst(roles_host):
    client, model, revit = roles_host
    model.script("single", tool_turn(("query", {"kind": "levels"})), text_turn("L1, L2."))
    result = run_bench(client, BASE, "one", [LEVELS_TASK], mode="single")
    assert result["runs"][0]["tasks"][0]["correct"] and model.roles == ["single", "single"]
    assert result["chat"]["host"] == "roles_analyst" and result["chat"]["modes_seen"] == ["single"]


def test_confirm_once_lets_the_model_reach_propose_spec(host):
    """The model reconciles a ready spec and stops in text asking for a yes (a structural sign the
    host streams: a ready reconcile, no propose_spec); --confirm once sends one yes, the model
    proposes, and the task is scored on that - with the yes turn's tokens counted. Nothing runs in
    Revit: the report gets the fixed result."""
    client, model, revit = host
    stop = [tool_turn(("reconcile", {"spec": wall_spec()}), usage=FULL),
            text_turn("Everything is ready. Reply yes to create the wall.", usage=FULL)]
    model.turns += [*stop,
                    tool_turn(("propose_spec", {"spec": wall_spec()}), usage=SMALL), text_turn("The card is below.", usage=SMALL),
                    text_turn("One wall was created.", usage=SMALL)]
    result = run_bench(client, BASE, "auto", [WALL_TASK], confirm="once")
    task = result["runs"][0]["tasks"][0]
    assert task["correct"] and task["error"] is None, task["error"]
    assert (task["turns"], task["confirm_turns"], task["feedback_turns"], task["requests"]) == (1, 1, 1, 3)
    assert task["usage"]["calls"] == 5 and task["usage"]["input"] == 2 * 12000 + 3 * 194
    assert [m["content"] for m in model.requests[2]["messages"] if m["role"] == "user"][-1] == "yes"
    assert result["confirm_policy"] == "once" and result["mode"] == "synthetic"
    assert not any("Wall.Create" in str(request.get("params", {}).get("code", "")) for request in revit.requests)
    assert result["runs"][0]["host_usage"]["match"]

    # The same model without --confirm: the task stops at the text and is not correct.
    model.turns += list(stop)
    task = run_bench(client, BASE, "manual", [WALL_TASK])["runs"][0]["tasks"][0]
    assert not task["correct"] and task["confirm_turns"] == 0 and task["requests"] == 1


def test_confirm_once_needs_a_ready_reconcile(host):
    client, model, revit = host
    unknown_level = wall_spec()
    for binding in unknown_level["parameters"]:
        if binding["name"] == "level_name":
            binding["value"] = "L9"
    model.turns += [tool_turn(("reconcile", {"spec": unknown_level})), text_turn("Which level? L9 does not exist.")]
    task = run_bench(client, BASE, "notready", [WALL_TASK], confirm="once")["runs"][0]["tasks"][0]
    assert task["confirm_turns"] == 0 and task["requests"] == 1 and model.turns == []
