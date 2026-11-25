"""The models file (``models.yml``): the model of each role, keys in the environment.

Absent, nothing changes (the same requests to the model, the same SSE stream). Present,
it is checked at startup - a bad one refuses the start without echoing a value - and the
planner's entry stands where LLM_* stood; the X-LLM-* headers replace that entry for one
request. All key values below are made up and set with monkeypatch.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
import yaml
from starlette.datastructures import Headers

from backend import llm as llm_module
from backend.api import chat as chat_module
from backend.config import ConfigError, WebSettings, get_settings
from backend.llm import LLMError
from backend.log_store import get_log_store
from backend.models_file import (
    NO_MODEL_FROM_FILE, ModelTarget, get_models, load_models, parse_models, resolve,
)
from tests.fake_model import USAGE_SHAPES, FakeModel, text_turn, tool_turn
from tests.test_api_v1 import WALL, spec_for
from tests.test_host_loop import events, sse

MAIN_KEY = "made-up-main-key-0123456789abcdef"
SMALL_KEY = "made-up-small-key-fedcba9876543210"
VISITOR_KEY = "made-up-visitor-key-42"
ENV_KEY = "made-up-env-key-77"

MIXED = """\
models:
  main:  { base_url: https://main.example/v1, model: main-model, api_key_env: W5A_FAKE_MAIN_KEY }
  small: { base_url: https://small.example/v1/, model: small-model, api_key_env: W5A_FAKE_SMALL_KEY,
           max_tokens: 512, temperature: 0 }
roles:
  analyst: small
  planner: main
  supervisor: small
"""

ONE_MODEL = """\
models:
  only: { base_url: https://main.example/v1, model: main-model, api_key_env: W5A_FAKE_MAIN_KEY }
roles:
  planner: only
"""


class Recorder:
    """The scripted model behind httpx.MockTransport, keeping where each request went and
    with which Authorization."""

    def __init__(self):
        self.model = FakeModel([])
        self.sent: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append({"url": str(request.url), "authorization": request.headers.get("authorization", ""),
                          "body": json.loads(request.content.decode("utf-8"))})
        return self.model._handle(request)


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = rec.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return rec


def write_models(tmp_path, text: str, name: str = "models.yml"):
    """A models file in the test's DATA_DIR (the default MODELS_FILE)."""
    path = tmp_path / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def keys(env):
    env.setenv("W5A_FAKE_MAIN_KEY", MAIN_KEY)
    env.setenv("W5A_FAKE_SMALL_KEY", SMALL_KEY)
    return env


def steady(text: str) -> str:
    """``text`` without what differs from run to run: the snapshot's clock and duration."""
    text = re.sub(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", "<time>", text)
    return re.sub(r'(duration_ms\\?": )[0-9]+', r"\g<1>0", text)


def normalized(text: str) -> list:
    """SSE frames with what differs from run to run fixed: durations and the snapshot's clock."""
    text = steady(text)
    frames = []
    for event, payload in sse(text):
        if event in ("usage", "tool") and isinstance(payload, dict) and "ms" in payload:
            payload = {**payload, "ms": 0}
        frames.append((event, payload))
    return frames


def bodies(recorder: Recorder) -> list:
    text = json.dumps([(s["url"], s["authorization"], s["body"]) for s in recorder.sent], ensure_ascii=False)
    return json.loads(steady(text))


def conversation(client, recorder: Recorder, headers: dict) -> list:
    """A scripted conversation: tools and a spec, the execution report, a second message
    and the no-bridge mode; the frames of each response, normalized."""
    model = recorder.model
    model.turns += [
        tool_turn(("get_project_snapshot", {"categories": ["OST_Walls"]}), ("query", {"kind": "levels"}),
                  content="Reading. ", usage=USAGE_SHAPES["openai"][0]),
        tool_turn(("propose_spec", {"spec": spec_for("create_wall", **WALL)}), usage=USAGE_SHAPES["deepseek"][0]),
        text_turn("The card is below.", usage=USAGE_SHAPES["gemini"][0]),
        text_turn("It ran.", usage=USAGE_SHAPES["anthropic_native"][0]),
        tool_turn(("list_tools", {})), text_turn("Eight tools."),
        text_turn("No bridge here."),
    ]
    out = []
    first = client.post("/api/chat", json={"message": "a wall on L1"}, headers=headers)
    assert first.status_code == 200, first.text
    sid = first.headers["X-Session-Id"]
    out.append(normalized(first.text))
    run = {"success": True, "result": {"created": [1]}, "validation": {"passed": True}, "evidence_id": "ev_1"}
    for body in ({"session_id": sid, "execution": run}, {"session_id": sid, "message": "which tools?"},
                 {"message": "hello", "bridge": False}):
        resp = client.post("/api/chat", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        out.append(normalized(resp.text))
    return out


# -- no file: as before -----------------------------------------------------------------

def test_without_a_file_the_requests_and_the_stream_are_the_same_as_before(revit, make_client, env, recorder,
                                                                           monkeypatch):
    """No models file: the same model requests and SSE frames as the route's own resolution
    before the file existed (resolve_llm with the request's headers), for the browser's model
    and for the server's; and no completion option beyond stream_completion's defaults."""
    headers = {"X-LLM-Base-Url": "https://model.example/v1", "X-LLM-Model": "fake-1", "X-LLM-Key": VISITOR_KEY}

    def run(with_feature: bool, extra_env: dict, sent_headers: dict):
        for name, value in extra_env.items():
            env.setenv(name, value)
        client = make_client()
        assert get_models() is None
        if not with_feature:
            async def before(role, request_headers):          # chat.py before the models file
                return ModelTarget(llm=await llm_module.resolve_llm(request_headers, get_settings()))
            monkeypatch.setattr(chat_module, "resolve_model", before)
        recorder.sent.clear()
        frames = conversation(client, recorder, sent_headers)
        monkeypatch.setattr(chat_module, "resolve_model", resolve)
        return frames, bodies(recorder)

    server = {"LLM_BASE_URL": "https://srv.example/v1/", "LLM_MODEL": "srv-model", "LLM_API_KEY": ENV_KEY}
    for extra_env, sent_headers in (({}, headers), (server, {})):
        now, before = run(True, extra_env, sent_headers), run(False, extra_env, sent_headers)
        assert now == before
        frames, sent = now
        assert len(sent) == 7 and [e for e, _ in frames[0]][0] == "stage"
        for _, _, body in sent:
            assert body["temperature"] == 0.3 and body["max_tokens"] == 16384
            assert set(body) <= {"model", "messages", "temperature", "max_tokens", "stream", "stream_options",
                                 "tools", "tool_choice"}


def test_one_entry_for_every_role_sends_what_the_same_environment_sends(revit, make_client, env, keys, recorder,
                                                                         tmp_path):
    """A file whose one entry is LLM_*'s endpoint, model and key: the model sees the same
    requests and the page the same stream as without the file."""
    env.setenv("LLM_BASE_URL", "https://main.example/v1")
    env.setenv("LLM_MODEL", "main-model")
    env.setenv("LLM_API_KEY", MAIN_KEY)
    without = conversation(make_client(), recorder, {}), bodies(recorder)
    recorder.sent.clear()
    write_models(tmp_path, ONE_MODEL)
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"):
        env.delenv(name)
    client = make_client()
    assert get_models() is not None
    assert conversation(client, recorder, {}) == without[0]
    assert bodies(recorder) == without[1]


# -- a valid file ------------------------------------------------------------------------------

def test_a_mixed_file_is_read_and_the_planner_runs_on_its_entry(revit, make_client, keys, recorder, tmp_path):
    write_models(tmp_path, MIXED)
    client = make_client()
    models = get_models()
    assert models.roles == {"analyst": "small", "planner": "main", "supervisor": "small"}
    assert models.entries["small"].base_url == "https://small.example/v1"          # trailing / dropped
    assert (models.entries["small"].max_tokens, models.entries["small"].temperature) == (512, 0.0)

    features = client.get("/config.json").json()["features"]
    assert features["models"] == {"analyst": "small-model", "planner": "main-model", "supervisor": "small-model"}
    assert features["modelsFile"] is True and features["serverModel"] is True

    recorder.model.turns += [tool_turn(("query", {"kind": "levels"}), usage=USAGE_SHAPES["openai"][0]),
                             text_turn("L1 and L2.", usage=USAGE_SHAPES["deepseek"][0])]
    resp = client.post("/api/chat", json={"message": "levels?"}, headers={"X-Run-Label": "mixed-1"})
    assert resp.status_code == 200, resp.text
    assert [(s["url"], s["authorization"]) for s in recorder.sent] == [
        ("https://main.example/v1/chat/completions", f"Bearer {MAIN_KEY}")] * 2
    assert all(s["body"]["model"] == "main-model" and s["body"]["max_tokens"] == 16384 for s in recorder.sent)
    frames = sse(resp.text)
    assert {(s["role"], s["model"]) for s in events(frames, "stage")} == {("planner", "main-model")}
    assert [(u["role"], u["model"]) for u in events(frames, "usage")] == [("planner", "main-model")] * 2
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert [(r["role"], r["model"], r["run_label"]) for r in rows] == [("planner", "main-model", "mixed-1")] * 2


def test_a_missing_role_uses_the_planners_entry_and_an_entry_may_be_unused(tmp_path):
    text = """\
models:
  main: {base_url: "https://main.example/v1", model: main-model, api_key_env: MAIN_KEY}
  spare: {base_url: "http://127.0.0.1:11434/v1", model: local-model, api_key_env: SPARE_KEY}
  dev: {base_url: "http://localhost:8000/v1", model: dev-model, api_key_env: _DEV2}
roles:
  planner: main
"""
    models = parse_models(yaml.safe_load(text), tmp_path / "models.yml")
    assert models.roles == {"analyst": "main", "planner": "main", "supervisor": "main"}
    assert models.model_names() == {"analyst": "main-model", "planner": "main-model", "supervisor": "main-model"}
    assert set(models.entries) == {"main", "spare", "dev"}


def test_entry_options_reach_the_request(make_client, keys, recorder, tmp_path):
    write_models(tmp_path, MIXED.replace("planner: main", "planner: small"))
    client = make_client()
    recorder.model.turns += [text_turn("ok")]
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    body = recorder.sent[0]["body"]
    assert (body["model"], body["max_tokens"], body["temperature"]) == ("small-model", 512, 0.0)
    assert recorder.sent[0]["url"] == "https://small.example/v1/chat/completions"
    # The visitor's own model name replaces the entry's model: its options do not go with it.
    recorder.model.turns += [text_turn("ok")]
    client.post("/api/chat", json={"message": "hi"}, headers={"X-LLM-Model": "visitor-model"})
    body = recorder.sent[1]["body"]
    assert (body["model"], body["max_tokens"], body["temperature"]) == ("visitor-model", 16384, 0.3)


def resolve_now(*args, **kwargs) -> ModelTarget:
    return asyncio.run(resolve(*args, **kwargs))


# -- the headers replace the planner's entry only ------------------------------------------------

def test_headers_replace_the_planners_entry_only(keys, tmp_path):
    settings = get_settings()
    models = load_models(write_models(tmp_path, MIXED))
    browser = Headers({"X-LLM-Base-Url": "https://visitor.example/v1", "X-LLM-Model": "visitor-model",
                       "X-LLM-Key": VISITOR_KEY})

    planner = resolve_now("planner", browser, settings=settings, models=models)
    assert (planner.base_url, planner.model, planner.api_key, planner.entry) == (
        "https://visitor.example/v1", "visitor-model", VISITOR_KEY, "main")
    assert planner.llm.source == "browser" and planner.options == {}
    for role in ("analyst", "supervisor"):
        other = resolve_now(role, browser, settings=settings, models=models)
        assert (other.base_url, other.model, other.api_key, other.entry) == (
            "https://small.example/v1", "small-model", SMALL_KEY, "small")
        assert other.llm.source == "server" and other.options == {"max_tokens": 512, "temperature": 0.0}

    # Field by field, as with LLM_*: a key alone uses the entry's endpoint and model ...
    own_key = resolve_now("planner", Headers({"X-LLM-Key": VISITOR_KEY}), settings=settings, models=models)
    assert (own_key.base_url, own_key.model, own_key.api_key) == ("https://main.example/v1", "main-model", MAIN_KEY)
    own_model = resolve_now("planner", Headers({"X-LLM-Model": "m2"}), settings=settings, models=models)
    assert (own_model.base_url, own_model.model, own_model.api_key) == ("https://main.example/v1", "m2", MAIN_KEY)
    # ... and the entry's key never goes to the browser's endpoint, which is checked as before.
    with pytest.raises(LLMError) as exc:
        resolve_now("planner", Headers({"X-LLM-Base-Url": "https://visitor.example/v1"}), settings=settings,
                      models=models)
    assert exc.value.status == 400 and "X-LLM-Key is required" in str(exc.value)
    for url in ("http://visitor.example/v1", "https://10.0.0.5/v1", "https://127.0.0.1/v1"):
        with pytest.raises(LLMError) as exc:
            resolve_now("planner", Headers({"X-LLM-Base-Url": url, "X-LLM-Key": VISITOR_KEY}),
                          settings=settings, models=models)
        assert exc.value.status == 400
    # The browser's settings never touch a role on another entry, even when they would be refused.
    other = resolve_now("analyst", Headers({"X-LLM-Base-Url": "http://10.0.0.5/v1"}), settings=settings,
                          models=models)
    assert other.base_url == "https://small.example/v1"


def test_roles_on_the_planners_entry_take_the_headers_too(keys, tmp_path):
    """The headers replace the ENTRY the planner points to: a role on that entry gets them."""
    models = load_models(write_models(tmp_path, MIXED.replace("analyst: small", "analyst: main")))
    browser = Headers({"X-LLM-Key": VISITOR_KEY})
    analyst = resolve_now("analyst", browser, settings=get_settings(), models=models)
    supervisor = resolve_now("supervisor", browser, settings=get_settings(), models=models)
    assert analyst.api_key == MAIN_KEY and supervisor.api_key == SMALL_KEY


def test_without_a_file_every_role_is_the_environment_and_the_headers(env):
    env.setenv("LLM_BASE_URL", "https://srv.example/v1")
    env.setenv("LLM_MODEL", "srv-model")
    env.setenv("LLM_API_KEY", ENV_KEY)
    settings = WebSettings.from_env()
    for role in ("analyst", "planner", "supervisor"):
        target = resolve_now(role, Headers({"X-LLM-Model": "m2"}), settings=settings, models=None)
        assert (target.base_url, target.model, target.api_key, target.entry) == (
            "https://srv.example/v1", "srv-model", ENV_KEY, None)
        assert target.options == {}
    with pytest.raises(ValueError):
        resolve_now("reviewer", {}, settings=settings, models=None)


# -- the key is read at call time ------------------------------------------------------------------

def test_the_key_variable_is_read_on_every_call(make_client, env, recorder, tmp_path):
    write_models(tmp_path, MIXED)
    env.setenv("W5A_FAKE_SMALL_KEY", SMALL_KEY)
    env.delenv("W5A_FAKE_MAIN_KEY", raising=False)
    client = make_client()                                 # unset at startup: no error
    assert client.get("/config.json").json()["features"]["serverModel"] is False

    refused = client.post("/api/chat", json={"message": "hi"})
    assert refused.status_code == 400 and refused.json()["detail"] == NO_MODEL_FROM_FILE
    assert "W5A_FAKE" not in refused.text and recorder.sent == []

    env.setenv("W5A_FAKE_MAIN_KEY", MAIN_KEY)              # set while the host runs
    recorder.model.turns += [text_turn("one"), text_turn("two"), text_turn("three")]
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    assert client.get("/config.json").json()["features"]["serverModel"] is True
    env.setenv("W5A_FAKE_MAIN_KEY", "made-up-rotated-key-99")
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    # The browser may bring the key when the variable is not set.
    env.delenv("W5A_FAKE_MAIN_KEY")
    assert client.post("/api/chat", json={"message": "hi"}, headers={"X-LLM-Key": VISITOR_KEY}).status_code == 200
    assert [s["authorization"] for s in recorder.sent] == [
        f"Bearer {MAIN_KEY}", "Bearer made-up-rotated-key-99", f"Bearer {VISITOR_KEY}"]


# -- refusals ---------------------------------------------------------------------------------------

SECRET = "sk-made-up-0123456789abcdefghijklmnopqrstuvwxyz"

REFUSED = [
    # (file text, what the message names)
    ("models: [\n  - 1\nroles: {", "not valid YAML (line"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K}}\n  "
     f"bad: {{base_url: {SECRET}: x}}\nroles: {{planner: main}}\n", "not valid YAML (line 3"),
    ("models:\n  main: &m {base_url: https://a.example/v1, model: m, api_key_env: K}\n  copy: *m\n"
     "roles: {planner: main}\n", "aliases (*name) are not allowed"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\n"
     "  main: {base_url: https://b.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n",
     "a key is given twice"),
    ("", "the file must be a mapping with models and roles"),
    ("- models\n", "the top level must be a mapping"),
    ("roles: {planner: main}\n", "models: missing"),
    ("models: {}\nroles: {planner: main}\n", "models: must be a mapping of entry names"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\n", "roles: missing"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\nroles: {analyst: main}\n",
     "roles.planner: missing"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\n"
     "roles: {planner: main, reviewer: main}\n", "roles: 'reviewer' is not a role"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\n"
     "roles: {planner: main, analyst: nowhere}\n", "roles.analyst: must name an entry under models"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n"
     "extra: 1\n", "'extra': not a field of the file"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, api_key: {SECRET}}}\n"
     "roles: {planner: main}\n", "models.main.api_key: not allowed - keys never go in this file"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, key: {SECRET}}}\n"
     "roles: {planner: main}\n", "models.main.key: not allowed"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, token: {SECRET}}}\n"
     "roles: {planner: main}\n", "models.main.token: not allowed"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, "
     f"extra_headers: {{Authorization: Bearer {SECRET}}}}}\nroles: {{planner: main}}\n",
     "models.main.extra_headers: not allowed"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K, region: eu}\n"
     "roles: {planner: main}\n", "models.main: 'region' is not a field of an entry"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, {SECRET}: 1}}\n"
     "roles: {planner: main}\n", "models.main: a field (name not shown)"),
    ("models:\n  main: {model: m, api_key_env: K}\nroles: {planner: main}\n", "models.main.base_url: missing"),
    ("models:\n  main: {base_url: https://a.example/v1, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.model: missing"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m}\nroles: {planner: main}\n",
     "models.main.api_key_env: missing"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: {SECRET}}}\n"
     "roles: {planner: main}\n", "models.main.api_key_env: must be the NAME of an environment variable"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: lower_case}\n"
     "roles: {planner: main}\n", "models.main.api_key_env: must be the NAME"),
    ("models:\n  main: {base_url: http://a.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.base_url: must be https://"),
    ("models:\n  main: {base_url: http://192.168.1.20:11434/v1, model: m, api_key_env: K}\n"
     "roles: {planner: main}\n", "models.main.base_url: must be https://"),
    ("models:\n  main: {base_url: ftp://a.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.base_url: must be https://"),
    (f"models:\n  main: {{base_url: 'https://user:{SECRET}@a.example/v1', model: m, api_key_env: K}}\n"
     "roles: {planner: main}\n", "models.main.base_url: must not carry a user name or password"),
    (f"models:\n  main: {{base_url: 'https://a.example/v1?key={SECRET}', model: m, api_key_env: K}}\n"
     "roles: {planner: main}\n", "models.main.base_url: must not have a query"),
    ("models:\n  main: {base_url: 'https://a b/v1', model: m, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.base_url: not a valid URL"),
    ("models:\n  main: {base_url: 42, model: m, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.base_url: must be a URL"),
    ("models:\n  main: {base_url: https://a.example/v1, model: 7, api_key_env: K}\nroles: {planner: main}\n",
     "models.main.model: must be the model's name"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K, max_tokens: 0}\n"
     "roles: {planner: main}\n", "models.main.max_tokens: must be a whole number"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K, max_tokens: true}\n"
     "roles: {planner: main}\n", "models.main.max_tokens: must be a whole number"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K, temperature: 3}\n"
     "roles: {planner: main}\n", "models.main.temperature: must be a number from 0 to 2"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K, temperature: .nan}\n"
     "roles: {planner: main}\n", "models.main.temperature: must be a number"),
    ("models:\n  main: just-text\nroles: {planner: main}\n", "models.main: must be a mapping"),
    (f"models:\n  {SECRET}: {{base_url: https://a.example/v1, model: m, api_key_env: K}}\n"
     f"roles: {{planner: {SECRET}}}\n", "models: the name of entry 1 must be 1-32 letters"),
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n"
     "!!python/object:os.system {}: 1\n", "not valid YAML"),
]


@pytest.mark.parametrize("text, needle", REFUSED)
def test_an_invalid_file_refuses_to_start_naming_file_field_and_problem(make_client, env, tmp_path, text, needle,
                                                                       capsys):
    path = write_models(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        make_client()
    message = str(exc.value)
    assert message.startswith(f"models file {path}: "), message
    assert needle in message, message
    assert SECRET not in message and "made-up" not in message
    # The console entry prints it and exits 2, the value never shown there either.
    from backend.main import build_app_or_exit
    with pytest.raises(SystemExit) as stop:
        build_app_or_exit()
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert f"cannot start: models file {path}" in err and SECRET not in err


def test_every_problem_is_listed(tmp_path):
    with pytest.raises(ConfigError) as exc:
        parse_models({"models": {"a": {"base_url": "http://x.example", "model": "", "api_key_env": "k"}},
                      "roles": {"planner": "b", "boss": "a"}}, tmp_path / "models.yml")
    message = str(exc.value)
    for needle in ("models.a.base_url: must be https://", "models.a.model: must be", "models.a.api_key_env: must be",
                   "roles.planner: must name an entry", "roles: 'boss' is not a role"):
        assert needle in message


def test_a_missing_file_where_models_file_points_refuses_to_start(make_client, env, tmp_path):
    missing = tmp_path / "elsewhere" / "models.yml"
    env.setenv("MODELS_FILE", str(missing))
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert f"MODELS_FILE names {missing}, where there is no file" in str(exc.value)
    # A directory is no file either; a file there is read.
    missing.mkdir(parents=True)
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert str(exc.value) == f"models file {missing}: not a file"
    other = tmp_path / "other.yml"
    other.write_text(ONE_MODEL, encoding="utf-8")
    env.setenv("MODELS_FILE", str(other))
    make_client()
    assert get_models().path == other


def test_the_default_path_without_a_file_is_no_error(make_client, env, tmp_path):
    assert get_settings().models_file == tmp_path / "data" / "models.yml"
    assert not get_settings().models_file_explicit
    make_client()
    assert get_models() is None
    env.setenv("MODELS_FILE", "  ")                       # empty: the default
    make_client()
    assert get_models() is None


def test_a_large_or_undecodable_file_is_refused(make_client, env, tmp_path):
    path = write_models(tmp_path, ONE_MODEL + "#" * (64 * 1024))
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert "larger than 64 KB" in str(exc.value)
    path.write_bytes(b"models:\n  main: {model: \xff\xfe}\n")
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert str(exc.value) == f"models file {path}: not UTF-8 text"
    path.write_bytes(b"\xef\xbb\xbf" + ONE_MODEL.encode())      # Notepad's BOM is fine
    make_client()
    assert get_models() is not None


# -- secrets go nowhere -------------------------------------------------------------------------------

def test_keys_appear_in_no_answer_log_or_export(make_client, keys, recorder, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    write_models(tmp_path, MIXED)
    client = make_client()
    recorder.model.turns += [text_turn("ok", usage=USAGE_SHAPES["openai"][0])]
    chat = client.post("/api/chat", json={"message": "hi"}, headers={"X-Run-Label": "secret-check"})
    assert chat.status_code == 200
    config_text = client.get("/config.json").text
    export = json.dumps(client.app.openapi())
    usage = client.get("/api/usage/runs", headers={"X-Admin-Token": "fixture-admin-password"}).text
    for text in (config_text, export, chat.text, usage, caplog.text):
        assert MAIN_KEY not in text and SMALL_KEY not in text and "Bearer" not in text
    # /config.json has the model names and nothing else of the file.
    for text in (config_text, export, usage, chat.text):
        assert "main.example" not in text and "small.example" not in text and "W5A_FAKE" not in text
    assert json.loads(config_text)["features"]["models"]["planner"] == "main-model"
    # The startup log says where the models come from: names, never a key or an address.
    assert "models file" in caplog.text and "main-model" in caplog.text and "main.example" not in caplog.text


def test_an_unset_key_variable_is_logged_by_name_only(make_client, env, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    write_models(tmp_path, MIXED)
    env.setenv("W5A_FAKE_SMALL_KEY", SMALL_KEY)
    env.delenv("W5A_FAKE_MAIN_KEY", raising=False)
    make_client()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "backend.models"]
    assert warnings == ["models file entry main: W5A_FAKE_MAIN_KEY is not set; its roles need X-LLM-Key from "
                        "the browser until it is"]
    assert SMALL_KEY not in caplog.text


def test_the_example_file_is_valid_and_holds_no_key():
    path = Path(__file__).resolve().parent.parent / "models.example.yml"
    models = load_models(path, explicit=True)
    assert models.model_names() == {"analyst": "some-small-model", "planner": "some-model",
                                    "supervisor": "some-small-model"}
    assert {e.api_key_env for e in models.entries.values()} == {"LLM_API_KEY"}
    text = path.read_text(encoding="utf-8")
    # Key prefixes, spelled in pieces so the repository's own key scan finds nothing here.
    prefixes = ["sk" + "-", "AI" + "za", "gh" + "p_", "Bearer"]
    assert text.isascii() and not any(prefix in text for prefix in prefixes)


# -- round 2: values YAML cannot read, names that could be a key ----------------------------------

ENTRY = "models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K}\nroles: {planner: main}\n"
LONG_DIGITS = "7" * 5000

UNREADABLE = [
    # (file text, a made-up value that must not appear anywhere)
    ("models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: !!int sk-made-up-123}\n"
     "roles: {planner: main}\n", "sk-made-up-123"),
    ("models:\n  main: {base_url: https://a.example/v1, model: !!bool madeupboolvalue, api_key_env: K}\n"
     "roles: {planner: main}\n", "madeupboolvalue"),
    ("models:\n  main: {base_url: https://a.example/v1, model: !!float madeupfloatvalue, api_key_env: K}\n"
     "roles: {planner: main}\n", "madeupfloatvalue"),
    ("models:\n  main: {base_url: https://a.example/v1, model: 2024-02-30, api_key_env: K}\n"
     "roles: {planner: main}\n", "2024-02-30"),
    ("models:\n  main: {base_url: https://a.example/v1, model: !!timestamp madeuptimevalue, api_key_env: K}\n"
     "roles: {planner: main}\n", "madeuptimevalue"),
    (f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: K, max_tokens: {LONG_DIGITS}}}\n"
     "roles: {planner: main}\n", "7777777777"),
]


@pytest.mark.parametrize("text, value", UNREADABLE)
def test_a_value_yaml_cannot_read_refuses_the_start_by_its_type_only(make_client, env, tmp_path, text, value,
                                                                    capsys, caplog):
    caplog.set_level(logging.DEBUG)
    path = write_models(tmp_path, text)
    with pytest.raises(ConfigError) as exc:
        make_client()
    message = str(exc.value)
    assert message.startswith(f"models file {path}: not valid YAML (a value YAML cannot read: "), message
    assert value not in message and exc.value.__cause__ is None and exc.value.__suppress_context__
    from backend.main import build_app_or_exit
    with pytest.raises(SystemExit) as stop:
        build_app_or_exit()
    assert stop.value.code == 2
    captured = capsys.readouterr()
    assert f"cannot start: models file {path}" in captured.err
    for text_out in (captured.err, captured.out, caplog.text):
        assert value not in text_out


def test_a_huge_temperature_is_a_plain_problem(make_client, env, tmp_path, capsys):
    huge = "9" * 400                                         # past what a float holds
    path = write_models(tmp_path, ENTRY.replace("api_key_env: K}", f"api_key_env: K, temperature: {huge}}}"))
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert str(exc.value) == f"models file {path}: models.main.temperature: must be a number from 0 to 2"
    for value in (f"-{huge}", "1e400", "-.inf", ".inf"):
        write_models(tmp_path, ENTRY.replace("api_key_env: K}", f"api_key_env: K, temperature: {value}}}"))
        with pytest.raises(ConfigError) as exc:
            make_client()
        assert "models.main.temperature: must be a number from 0 to 2" in str(exc.value)


def test_anything_the_check_trips_over_is_reported_by_type(monkeypatch, tmp_path):
    from backend import models_file

    def broken(data, path, **options):
        raise OverflowError("made-up-text-of-the-exception")
    monkeypatch.setattr(models_file, "parse_models", broken)
    path = write_models(tmp_path, ENTRY)
    with pytest.raises(ConfigError) as exc:
        load_models(path)
    assert str(exc.value) == f"models file {path}: cannot be checked (OverflowError)"


def test_a_long_entry_name_is_shown_by_position(make_client, env, tmp_path):
    long_name = "a-name-of-17-char"                          # 17 characters: not shown
    short_name = "sixteen-chars-ok"                          # 16: shown
    assert len(long_name) == 17 and len(short_name) == 16
    path = write_models(tmp_path, (
        f"models:\n  {short_name}: {{base_url: http://a.example/v1, model: m, api_key_env: K}}\n"
        f"  {long_name}: {{base_url: http://a.example/v1, model: m, api_key_env: K}}\n"
        f"roles: {{planner: {short_name}}}\n"))
    with pytest.raises(ConfigError) as exc:
        make_client()
    message = str(exc.value)
    assert f"models.{short_name}.base_url: must be https://" in message
    assert "models.<entry #2>.base_url: must be https://" in message
    assert long_name not in message and str(path) in message


def test_logs_show_short_names_only(make_client, env, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    long_name = "entry-name-of-24-letters"
    long_env = "W5A_" + "LONG_VARIABLE_NAME_" * 2 + "XX"      # 44 characters
    assert len(long_name) == 24 and len(long_env) > 40
    for name in ("W5A_SHORT_UNSET", long_env):
        env.delenv(name, raising=False)
    write_models(tmp_path, (
        f"models:\n  main: {{base_url: https://a.example/v1, model: m, api_key_env: W5A_SHORT_UNSET}}\n"
        f"  {long_name}: {{base_url: https://b.example/v1, model: m2, api_key_env: {long_env}}}\n"
        f"roles: {{planner: main, analyst: {long_name}}}\n"))
    make_client()
    warnings = sorted(r.getMessage() for r in caplog.records
                      if r.levelno == logging.WARNING and r.name == "backend.models")
    assert warnings == [
        "models file entry <entry #2>: (a long name, not shown) is not set; its roles need X-LLM-Key from the "
        "browser until it is",
        "models file entry main: W5A_SHORT_UNSET is not set; its roles need X-LLM-Key from the browser until it is"]
    assert long_name not in caplog.text and long_env not in caplog.text
    # The entry itself is read as written: the label is only how it is shown.
    assert get_models().entries[long_name].label == "<entry #2>"


@pytest.mark.parametrize("url", ["http://192.168.1.20:11434/v1", "http://models.lan:8080/v1", "http://a.example/v1"])
def test_http_to_another_host_only_with_llm_allow_http(make_client, env, tmp_path, url):
    """Loopback http always; http to any other host with LLM_ALLOW_HTTP=1 - the switch that
    lets the browser's endpoint use http, with the same meaning."""
    path = write_models(tmp_path, ENTRY.replace("https://a.example/v1", url))
    with pytest.raises(ConfigError) as exc:
        make_client()
    assert str(exc.value) == (f"models file {path}: models.main.base_url: must be https:// (http:// only to a "
                              f"loopback address such as 127.0.0.1 or localhost, or to any host with "
                              f"LLM_ALLOW_HTTP=1)")
    env.setenv("LLM_ALLOW_HTTP", "1")
    make_client()
    assert get_models().entries["main"].base_url == url
    # Still a URL of the two schemes, and still without a user name or a query.
    for bad in (url.replace("http://", "ftp://"), url.replace("http://", "http://u:p@"), url + "?k=v"):
        write_models(tmp_path, ENTRY.replace("https://a.example/v1", bad))
        with pytest.raises(ConfigError):
            make_client()


def test_http_to_loopback_needs_no_switch(make_client, env, tmp_path):
    for url in ("http://127.0.0.1:11434/v1", "http://localhost:8000/v1", "http://[::1]:9000/v1", "http://127.1/v1"):
        write_models(tmp_path, ENTRY.replace("https://a.example/v1", f"'{url}'"))      # [::1] needs quotes
        make_client()
        assert get_models().entries["main"].base_url == url
