"""Pin requests and SSE streams for the single loop and both roles modes.

The original records from host v2 stay in default_path_golden.json. Inline
forms add one planner tool and new card directions, so v3 has its own
request records at both output limits. These scripted conversations do not
ask a question; their SSE streams must still match the original records.

Regenerate the current catalogue delta records after an intentional tool change:
    REGEN_DEFAULT_PATH_GOLDEN=1 uv run pytest tests/test_default_path.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import llm as llm_module
from tests.fake_model import USAGE_SHAPES, RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.test_api_v1 import WALL, spec_for

GOLDEN = Path(__file__).with_name("default_path_golden.json")
REGENERATE = os.environ.get("REGEN_DEFAULT_PATH_GOLDEN") == "1"

SERVER = {"LLM_BASE_URL": "https://srv.example/v1", "LLM_MODEL": "srv-model", "LLM_API_KEY": "made-up-srv-key"}
BROWSER = {"X-LLM-Base-Url": "https://model.example/v1", "X-LLM-Model": "fake-1", "X-LLM-Key": "made-up-visitor"}
SENT_HEADERS = ("authorization", "content-type", "accept", "x-title")

ROUTING_TOOL = {"route": "tool", "tool": "create_wall", "facts_needed": ["end point"], "note": "Ask for the end."}
ROUTING_QUERY = {"route": "query", "facts_needed": [], "note": "Read the levels."}
DONE = {"verdict": "done", "report": "One wall was created on L1; the validator passed."}
RETRY = {"verdict": "retry", "report": "The level was not found.", "fix": "Use L1."}
OK = {"success": True, "tool": "create_wall", "error": None, "evidence_id": "ev_3",
      "result": {"Status": "Success", "ElementId": 4242},
      "validation": {"validator": "element_count", "passed": True,
                     "checks": [{"name": "count", "passed": True, "detail": "walls 1 -> 2"}],
                     "before": {"walls": 1}, "after": {"walls": 2}}, "warnings": []}
FAILED = {"success": False, "tool": "create_wall", "error": "Level 'L9' not found", "evidence_id": "ev_1",
          "warnings": []}

_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z")
_DURATION = re.compile(r'(duration_ms\\*"\s*:\s*)[0-9]+')
_MS = re.compile(r'("ms"\s*:\s*)[0-9]+')


def normalize(text: str) -> str:
    """``text`` without what differs from run to run: the snapshot's clock and duration, the ms of
    a trace event."""
    text = _TIME.sub("<time>", text)
    text = _DURATION.sub(r"\g<1>0", text)
    return _MS.sub(r"\g<1>0", text)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Recorder(RoleScriptedModel):
    """The role-scripted model, keeping each request as it went out: URL, headers, raw body."""

    def __init__(self):
        super().__init__()
        self.raw: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        head = "\n".join(f"{name}: {request.headers.get(name, '')}" for name in SENT_HEADERS)
        self.raw.append(f"{request.method} {request.url}\n{head}\n{request.content.decode('utf-8')}")
        return super()._handle(request)


@pytest.fixture
def recorder(monkeypatch):
    fake = Recorder()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


def post(client, body: dict, headers: dict, streams: list[str]) -> str:
    resp = client.post("/api/chat", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    streams.append(resp.text)
    return resp.headers["X-Session-Id"]


def single_loop(client, model: Recorder, headers: dict, extra: dict) -> list[str]:
    """The one loop: tools and a spec, the execution report, a second message, no bridge."""
    model.script("single",
                 tool_turn(("get_project_snapshot", {"categories": ["OST_Walls"]}), ("query", {"kind": "levels"}),
                           content="Reading. ", usage=USAGE_SHAPES["openai"][0]),
                 tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)}), usage=USAGE_SHAPES["deepseek"][0]),
                 text_turn("The card is below.", usage=USAGE_SHAPES["gemini"][0]),
                 text_turn("It ran.", usage=USAGE_SHAPES["anthropic_native"][0]),
                 tool_turn(("list_tools", {})), text_turn("Eight tools."),
                 text_turn("No bridge here."))
    streams: list[str] = []
    sid = post(client, {"message": "a wall on L1", **extra}, headers, streams)
    post(client, {"session_id": sid, "execution": OK, **extra}, headers, streams)
    post(client, {"session_id": sid, "message": "which tools?", **extra}, headers, streams)
    post(client, {"message": "hello", "bridge": False, **extra}, headers, streams)
    return streams


def roles_with_analyst(client, model: Recorder, headers: dict, extra: dict) -> list[str]:
    """The analyst routes a wall; the planner proposes; a failed run goes back (retry), a good one
    is done; then a query; then no bridge."""
    spec = {"spec": spec_for("create_wall", **WALL)}
    model.script("analyst", json_turn(ROUTING_TOOL, usage=USAGE_SHAPES["openai"][0]),
                 json_turn(ROUTING_QUERY, usage=USAGE_SHAPES["deepseek"][0]))
    model.script("planner",
                 tool_turn(("propose_spec", spec), usage=USAGE_SHAPES["deepseek"][0]),
                 text_turn("The card is below.", usage=USAGE_SHAPES["gemini"][0]),
                 tool_turn(("propose_spec", spec)), text_turn("Proposed again."),
                 tool_turn(("query", {"kind": "levels"})), text_turn("L1 and L2."))
    model.script("supervisor", json_turn(RETRY, usage=USAGE_SHAPES["openai_cache_write"][0]), json_turn(DONE))
    model.script("single", text_turn("No bridge here."))
    streams: list[str] = []
    sid = post(client, {"message": "a wall on L1 from (0,0) to (5000,0), 3000 high", **extra}, headers, streams)
    post(client, {"session_id": sid, "execution": FAILED, **extra}, headers, streams)
    post(client, {"session_id": sid, "execution": OK, **extra}, headers, streams)
    post(client, {"session_id": sid, "message": "levels?", **extra}, headers, streams)
    post(client, {"message": "hello", "bridge": False, **extra}, headers, streams)
    return streams


def roles_without_analyst(client, model: Recorder, headers: dict, extra: dict) -> list[str]:
    """Planner and supervisor: every message to the planner with route tool."""
    model.script("planner",
                 tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)}), usage=USAGE_SHAPES["openai"][0]),
                 text_turn("The card is below."),
                 tool_turn(("query", {"kind": "levels"})), text_turn("L1 and L2."))
    model.script("supervisor", json_turn(DONE, usage=USAGE_SHAPES["deepseek"][0]))
    streams: list[str] = []
    sid = post(client, {"message": "a wall on L1", **extra}, headers, streams)
    post(client, {"session_id": sid, "execution": OK, **extra}, headers, streams)
    post(client, {"session_id": sid, "message": "levels?", **extra}, headers, streams)
    return streams


# name -> (environment, request headers, conversation)
SCENARIOS = {
    "single-server": ({"LLM_SINGLE_MODEL": "1", **SERVER}, {}, single_loop),
    "single-browser": ({"LLM_SINGLE_MODEL": "1"}, BROWSER, single_loop),
    "roles-analyst-server": ({"ROLES_ANALYST": "on", **SERVER}, {}, roles_with_analyst),
    "roles-server": ({"ROLES_ANALYST": "off", **SERVER}, {}, roles_without_analyst),
}


# The mode each scenario's turns run in (the chat modes; the record predates the field): the
# turn without the bridge runs as single.
MODES = {
    "single-server": ["single"] * 4,
    "single-browser": ["single"] * 4,
    "roles-analyst-server": ["roles_analyst"] * 4 + ["single"],
    "roles-server": ["roles"] * 3,
}


def without_mode(stream: str) -> tuple[str, str | None]:
    """The stream as it was before the chat modes - the ``mode`` field taken off its first
    ``stage`` event, the only change the modes make to it - and that mode."""
    blocks = stream.split("\n\n")
    for index, block in enumerate(blocks):
        if block.startswith("event: stage\ndata: "):
            payload = json.loads(block[len("event: stage\ndata: "):])
            mode = payload.pop("mode", None)
            blocks[index] = "event: stage\ndata: " + json.dumps(payload, ensure_ascii=False)
            return "\n\n".join(blocks), mode
    return stream, None


def run_scenario(name: str, env, revit, recorder: Recorder, *, extra: dict | None = None,
                 env_override: dict | None = None, headers: dict | None = None) -> tuple[list[str], list[str]]:
    """``(request texts, stream texts)`` of one scenario, normalized; each stream without the
    mode of its first stage event, which must be the scenario's (``MODES``)."""
    environment, scenario_headers, conversation = SCENARIOS[name]
    env.delenv("LLM_SINGLE_MODEL", raising=False)
    for key, value in {**environment, **(env_override or {})}.items():
        if value is None:
            env.delenv(key, raising=False)
        else:
            env.setenv(key, value)
    from tests.conftest import _reset
    _reset()
    from backend.main import create_app
    with TestClient(create_app()) as client:
        streams = conversation(client, recorder, {**scenario_headers, **(headers or {})}, extra or {})
    assert recorder.left() == {}, recorder.left()
    stripped = [without_mode(s) for s in streams]
    assert [mode for _, mode in stripped] == MODES[name]
    return [normalize(r) for r in recorder.raw], [normalize(s) for s, _ in stripped]


def hashes(texts: list[str]) -> list[str]:
    return [digest(t) for t in texts]


# The record's section taken from aa5556c (never regenerated) and the one of the current default.
AA5556C_LIMIT = 4096
AA5556C_SECTION = f"max_tokens_{AA5556C_LIMIT}"
VIEW_PACKS = {"set_view_detail_level", "set_view_display_style"}
PLAN_4096 = "view_packs_4096"


def current_section() -> str:
    return f"view_packs_{llm_module.DEFAULT_MAX_TOKENS}"


def recorded(name: str, section: str | None = None) -> dict:
    records = json.loads(GOLDEN.read_text(encoding="utf-8"))
    section = section or current_section()
    if section.startswith("view_packs_"):
        base = recorded(name, section.replace("view_packs_", "trial_fixes_"))
    elif section.startswith("trial_fixes_"):
        base = recorded(name, section.replace("trial_fixes_", "workflows_"))
    elif section.startswith("workflows_"):
        base = recorded(name, section.replace("workflows_", "selection_capture_"))
    elif section.startswith("selection_capture_"):
        base = records[section.replace("selection_capture_", "tool_authoring_")][name]
    else:
        return records[section][name]
    for kind, changes in records[section][name].items():
        for index, value in changes.items():
            base[kind][int(index)] = value
    return base


def assert_catalogue_streams(name, streams):
    legacy = recorded(name, AA5556C_SECTION)["streams"]
    for index, stream in enumerate(streams):
        if name.startswith("single-") and index == 2:
            # Only list_tools' size and preview changed as the catalogue grew.
            # Compare every other byte with the same turn in the V1 checkout.
            blocks = stream.split("\n\n")
            for i, block in enumerate(blocks):
                if block.startswith("event: tool\ndata: "):
                    event = json.loads(block.split("\ndata: ", 1)[1])
                    if event["name"] == "list_tools" and event["status"] == "ok":
                        del event["preview"], event["size"]
                        blocks[i] = "event: tool\ndata: " + json.dumps(event, ensure_ascii=False)
            normalized_v1 = {"single-server": "829c16046f065629f557ea1b30739f55a4c066b0180928b551911f773d7df2e6",
                             "single-browser": "74f10ac97f9ea92f500eb58723652f7492e6727f1ea16db0bd65f1c6c0d11f02"}
            assert digest("\n\n".join(blocks)) == normalized_v1[name]
        else:
            assert digest(stream) == legacy[index]


@pytest.mark.parametrize("limit", [AA5556C_LIMIT, None], ids=["aa5556c-limit-4096", "current-default"])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_historical_catalogue_still_produces_the_recorded_bytes(name, limit, env, revit, recorder, monkeypatch):
    """Replay the original planner surface and 0.10.1 inventory against every historical assertion."""
    from backend import roles
    from backend.api import chat, model_tools
    from revit_bridge.capabilities import ToolStore
    added = {"clear_selection", "connect_pipes", "create_duct", "create_pipe", "create_pipes_by_points",
             "link_cad", "link_revit_model", "list_mep_types", "load_family", "place_family_instance",
             "select_by_filter", "select_elements", "select_in_box", "set_section_box", "set_view_orientation",
             "zoom_to_elements"} | VIEW_PACKS
    list_tools = ToolStore.list_tools
    monkeypatch.setattr(ToolStore, "list_tools", lambda store: [p for p in list_tools(store) if p.name not in added])
    new_functions = {"ask_selection", "open_view", "capture_view_image", "use_workflow"}
    legacy_tools = [t for t in model_tools.TOOL_DEFINITIONS if t["function"]["name"] not in new_functions]
    monkeypatch.setattr(chat, "TOOL_DEFINITIONS", legacy_tools)
    monkeypatch.setattr(chat, "list_host_functions", lambda: [])
    for route, defs in roles.ROUTE_DEFINITIONS.items():
        monkeypatch.setitem(roles.ROUTE_DEFINITIONS, route, [t for t in defs if t in legacy_tools])
    if limit is not None:
        monkeypatch.setattr(llm_module, "DEFAULT_MAX_TOKENS", limit)
    section = f"tool_authoring_{llm_module.DEFAULT_MAX_TOKENS}"
    requests, streams = run_scenario(name, env, revit, recorder)
    sent = [json.loads(r.split("\n", len(SENT_HEADERS) + 1)[-1])["max_tokens"] for r in recorder.raw]
    assert set(sent) == {limit or llm_module.DEFAULT_MAX_TOKENS}
    prior = recorded(name, f"direct_reads_{llm_module.DEFAULT_MAX_TOKENS}")
    assert hashes([r.replace("/capabilities/authoring", "/capabilities/workbench") for r in requests]) == prior["requests"]
    assert hashes(streams) == prior["streams"]
    assert_catalogue_streams(name, streams)
    record = recorded(name, section)
    assert len(requests) == len(record["requests"])
    for index, (text, expected) in enumerate(zip(requests, record["requests"])):
        assert digest(text) == expected, f"request {index + 1} of {name} differs from the record"
    assert len(streams) == len(record["streams"])
    for index, (text, expected) in enumerate(zip(streams, record["streams"])):
        assert digest(text) == expected, f"stream {index + 1} of {name} differs from the record"
    if limit is None:
        # The two v3 sets of request bytes differ only in their output limit.
        old = recorded(name, "tool_authoring_4096")
        written_back = [re.sub(r'("max_tokens":\s*)%d\b' % llm_module.DEFAULT_MAX_TOKENS, rf"\g<1>{AA5556C_LIMIT}",
                               text, count=1) for text in requests]
        assert hashes(written_back) == old["requests"] and hashes(streams) == old["streams"]


@pytest.mark.parametrize("limit", [4096, 16384])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_workflow_bytes(name, limit, env, revit, recorder, monkeypatch):
    from revit_bridge.capabilities import ToolStore
    list_tools = ToolStore.list_tools
    monkeypatch.setattr(ToolStore, "list_tools", lambda store: [p for p in list_tools(store) if p.name not in VIEW_PACKS])
    monkeypatch.setattr(llm_module, "DEFAULT_MAX_TOKENS", limit)
    requests, streams = run_scenario(name, env, revit, recorder)
    assert_catalogue_streams(name, streams)
    actual = {"requests": hashes(requests), "streams": hashes(streams)}
    prior = recorded(name, f"workflows_{limit}")
    assert actual["streams"] == prior["streams"]
    prior_description = "Only their click starts observation."
    current_description = "The host immediately observes their normal selection in Revit."
    assert hashes([r.replace(current_description, prior_description) for r in requests]) == prior["requests"]
    assert all(len(actual[k]) == len(prior[k]) for k in actual)
    assert actual == recorded(name, f"trial_fixes_{limit}")


@pytest.mark.parametrize("limit", [4096, 16384])
@pytest.mark.parametrize("name", list(SCENARIOS))
def test_view_pack_catalogue_bytes(name, limit, env, revit, recorder, monkeypatch):
    monkeypatch.setattr(llm_module, "DEFAULT_MAX_TOKENS", limit)
    requests, streams = run_scenario(name, env, revit, recorder)
    assert_catalogue_streams(name, streams)
    actual = {"requests": hashes(requests), "streams": hashes(streams)}
    prior = recorded(name, f"trial_fixes_{limit}")
    assert all(len(values) == len(prior[kind]) for kind, values in actual.items())
    if REGENERATE:
        data = json.loads(GOLDEN.read_text(encoding="utf-8"))
        data.setdefault(current_section(), {})[name] = {
            k: {str(i): value for i, value in enumerate(values) if value != prior[k][i]}
            for k, values in actual.items()}
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert actual == recorded(name)
    if limit == 16384:
        replaced = [r.replace('"max_tokens":16384', '"max_tokens":4096', 1) for r in requests]
        assert {"requests": hashes(replaced), "streams": hashes(streams)} == recorded(name, PLAN_4096)


ENTRY = "models:\n  only: {base_url: https://srv.example/v1, model: srv-model, api_key_env: W8_SRV_KEY%s}\n" \
        "roles: {planner: only}\n"


@pytest.mark.parametrize("fields", ["", ", api: chat, max_tokens_param: max_tokens"])
@pytest.mark.parametrize("name", ["single-server", "roles-analyst-server", "roles-server"])
def test_a_models_file_entry_without_options_sends_what_llm_sends(name, fields, env, revit, recorder, tmp_path):
    """The server's model from a models file entry instead of LLM_* - with none of the new fields,
    or with the chat api and max_tokens spelled out - sends the same bytes and streams the same."""
    if REGENERATE:
        pytest.skip("comparing against the record")
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ENTRY % fields, encoding="utf-8")
    env.setenv("W8_SRV_KEY", SERVER["LLM_API_KEY"])
    requests, streams = run_scenario(name, env, revit, recorder,
                                     env_override={"LLM_BASE_URL": None, "LLM_MODEL": None, "LLM_API_KEY": None})
    record = recorded(name)
    assert hashes(requests) == record["requests"]
    assert hashes(streams) == record["streams"]


@pytest.mark.parametrize("name, mode", [("single-server", "single"), ("roles-analyst-server", "roles_analyst"),
                                        ("roles-server", "roles")])
def test_a_requested_mode_sends_what_its_switch_sends(name, mode, env, revit, recorder):
    """The mode named in each request (by an admin on this cloud host), with the switches left
    at the default (planner + supervisor): the requests and streams of that switch."""
    if REGENERATE:
        pytest.skip("comparing against the record")
    from tests.conftest import FIXTURE_ADMIN_PASSWORD
    requests, streams = run_scenario(name, env, revit, recorder, extra={"mode": mode},
                                     env_override={"LLM_SINGLE_MODEL": None, "ROLES_ANALYST": None},
                                     headers={"X-Admin-Token": FIXTURE_ADMIN_PASSWORD})
    record = recorded(name)
    assert hashes(requests) == record["requests"]
    assert hashes(streams) == record["streams"]


@pytest.mark.parametrize("value, section", [("16384", None), ("4096", PLAN_4096)])
def test_llm_max_tokens_is_the_only_value_it_changes(value, section, env, revit, recorder):
    """LLM_MAX_TOKENS=16384 sends what the default sends; 4096 sends the v3 requests for that limit."""
    if REGENERATE:
        pytest.skip("comparing against the record")
    requests, streams = run_scenario("single-server", env, revit, recorder, env_override={"LLM_MAX_TOKENS": value})
    record = recorded("single-server", section)
    assert hashes(requests) == record["requests"] and hashes(streams) == record["streams"]


def test_a_run_is_stable(env, revit, recorder):
    """The normalization catches everything that changes between two runs of one scenario."""
    first = run_scenario("roles-analyst-server", env, revit, recorder)
    recorder.raw.clear()
    recorder.requests.clear()
    recorder.roles.clear()
    second = run_scenario("roles-analyst-server", env, revit, recorder)
    assert first == second
