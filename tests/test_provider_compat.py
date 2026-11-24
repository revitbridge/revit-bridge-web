"""Provider options of a models file entry (backend/llm.py, backend/models_file.py): the
responses api, max_completion_tokens, reasoning_effort, timeout_s, the provider's own keys on a
tool call (Gemini's extra_content) going back with it, and a reply that used up its output limit
on thinking. Every endpoint here is a fake behind httpx.MockTransport; no provider is called and
every key is made up."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from backend import llm as llm_module
from backend.api.model_tools import TOOL_DEFINITIONS
from backend.config import ConfigError
from backend.llm import (
    CallStats, LLMSettings, ProviderMemory, ToolCall, Usage, parse_responses_usage, responses_input,
    stream_completion, with_call_extras,
)
from backend.log_store import get_log_store
from backend.models_file import load_models, parse_models
from backend.session import get_session_store
from tests.fake_model import USAGE_SHAPES, RoleScriptedModel, json_turn, text_turn, tool_turn
from tests.test_api_v1 import WALL, spec_for
from tests.test_host_loop import events, sse, tokens

MAIN_KEY = "made-up-main-key-w8"
SMALL_KEY = "made-up-small-key-w8"


def models_text(main: str = "", small: str = "", roles: str = "planner: main") -> str:
    return (f"models:\n"
            f"  main: {{base_url: https://main.example/v1, model: main-model, api_key_env: W8_FAKE_MAIN_KEY{main}}}\n"
            f"  small: {{base_url: https://small.example/v1, model: small-model, api_key_env: W8_FAKE_SMALL_KEY{small}}}\n"
            f"roles: {{{roles}}}\n")


def write_models(tmp_path, text: str):
    path = tmp_path / "data" / "models.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- a fake provider with both apis ------------------------------------------------------------------

class Provider:
    """``/responses`` answers the next of ``replies`` (a Responses object, or an httpx.Response);
    ``/chat/completions`` goes to a role-scripted chat model. Every request is kept with its URL,
    Authorization and the read timeout of the client that sent it."""

    def __init__(self):
        self.chat = RoleScriptedModel()
        self.replies: list = []
        self.sent: list[dict] = []
        self.timeouts: list[float | None] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.sent.append({"url": str(request.url), "authorization": request.headers.get("authorization"),
                          "accept": request.headers.get("accept"), "body": body})
        if str(request.url).endswith("/responses"):
            if not self.replies:
                return httpx.Response(500, text='{"error": "no reply scripted"}')
            reply = self.replies.pop(0)
            return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)
        return self.chat._handle(request)

    def responses_bodies(self) -> list[dict]:
        return [s["body"] for s in self.sent if s["url"].endswith("/responses")]


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        timeout = kwargs.get("timeout")
        fake.timeouts.append(timeout.read if isinstance(timeout, httpx.Timeout) else None)
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


@pytest.fixture
def keys(env):
    env.setenv("W8_FAKE_MAIN_KEY", MAIN_KEY)
    env.setenv("W8_FAKE_SMALL_KEY", SMALL_KEY)
    return env


def app_client(env, tmp_path, text: str, **environment) -> TestClient:
    write_models(tmp_path, text)
    for name, value in environment.items():
        env.setenv(name, value)
    from tests.conftest import _reset
    _reset()
    from backend.main import create_app
    return TestClient(create_app())


REASONING = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "made-up-encrypted-reasoning"}
USAGE = {"input_tokens": 9668, "input_tokens_details": {"cached_tokens": 9594, "cache_write_tokens": 71},
         "output_tokens": 255, "output_tokens_details": {"reasoning_tokens": 40}, "total_tokens": 9923}
USAGE_NO_CACHE = {"input_tokens": 1200, "output_tokens": 30, "total_tokens": 1230}


def call_item(name: str, args: dict, call_id: str) -> dict:
    return {"type": "function_call", "id": f"fc_{call_id}", "call_id": call_id, "name": name,
            "arguments": json.dumps(args), "status": "completed"}


def message_item(text: str) -> dict:
    return {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def reply(*output, usage=None, status="completed", incomplete=None) -> dict:
    return {"id": "resp_1", "object": "response", "status": status, "output": list(output), "usage": usage,
            "incomplete_details": incomplete}


# -- the responses api -------------------------------------------------------------------------------

def test_a_responses_entry_maps_messages_tools_usage_and_carries_reasoning(revit, env, keys, tmp_path, provider):
    client = app_client(env, tmp_path, models_text(main=", api: responses, reasoning_effort: low, max_tokens: 2048"))
    levels = call_item("query", {"kind": "levels"}, "call_abc")
    provider.replies += [reply(REASONING, levels, usage=USAGE),
                         reply(message_item("Two levels: L1 and L2."), usage=USAGE_NO_CACHE)]
    resp = client.post("/api/chat", json={"message": "levels?"}, headers={"X-Run-Label": "resp-1"})
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)

    first, second = provider.responses_bodies()
    assert [s["url"] for s in provider.sent] == ["https://main.example/v1/responses"] * 2
    assert {s["authorization"] for s in provider.sent} == {f"Bearer {MAIN_KEY}"}
    assert provider.sent[0]["accept"] == "application/json"
    # The request: no chat api fields, no temperature (the entry sets none), effort and encrypted reasoning.
    assert set(first) == {"model", "input", "store", "reasoning", "include", "tools", "tool_choice",
                          "max_output_tokens"}
    assert (first["model"], first["store"], first["reasoning"], first["include"], first["max_output_tokens"]) == (
        "main-model", False, {"effort": "low"}, ["reasoning.encrypted_content"], 2048)
    assert first["tool_choice"] == "auto"
    # Tools: the flat form, the same names, descriptions and schemas, not strict.
    assert first["tools"] == [{"type": "function", "name": t["function"]["name"],
                               "description": t["function"]["description"],
                               "parameters": t["function"]["parameters"], "strict": False} for t in TOOL_DEFINITIONS]
    # Input: the system prompt first, then the designer's message.
    assert [item.get("role") for item in first["input"]] == ["system", "user"]
    assert first["input"][1]["content"] == "levels?"
    # The second call: the reply's output items as they came (the reasoning with its encrypted
    # content, the function call with its id), then the tool's result.
    assert second["input"][:2] == first["input"]
    assert second["input"][2:4] == [REASONING, levels]
    output = second["input"][4]
    assert output["type"] == "function_call_output" and output["call_id"] == "call_abc"
    assert '"L1"' in output["output"] and json.loads(output["output"])          # the tool's JSON text

    # The browser's stream: the tool events and the text as for a chat call.
    assert tokens(frames) == "Two levels: L1 and L2."
    assert [t["status"] for t in events(frames, "tool")] == ["running", "ok"]
    usage = events(frames, "usage")
    assert [(u["input"], u["cache_read"], u["cache_write"], u["output"]) for u in usage] == [
        (9668, 9594, 71, 255), (1200, None, None, 30)]
    rows = get_log_store().calls_of_session(resp.headers["X-Session-Id"])
    assert [(r["model"], r["input"], r["cache_read"], r["output"]) for r in rows] == [
        ("main-model", 9668, 9594, 255), ("main-model", 1200, None, 30)]

    # The next message of the session: the earlier call goes back as it came, the final text as text.
    provider.replies.append(reply(message_item("Still two."), usage=USAGE_NO_CACHE))
    client.post("/api/chat", json={"message": "and now?", "session_id": resp.headers["X-Session-Id"]})
    third = provider.responses_bodies()[-1]
    assert third["input"][2:4] == [REASONING, levels]
    assert third["input"][-2:] == [{"role": "assistant", "content": "Two levels: L1 and L2."},
                                   {"role": "user", "content": "and now?"}]
    # Nothing of the reasoning is in the session's messages or the interaction log.
    session = get_session_store().get(resp.headers["X-Session-Id"])
    assert "made-up-encrypted" not in json.dumps(session.history)
    assert "made-up-encrypted" not in json.dumps(get_log_store().query(limit=50))


def test_a_reply_without_encrypted_reasoning_goes_back_as_plain_calls(revit, env, keys, tmp_path, provider):
    """No reasoning_effort: no include, no reasoning field; a reasoning item without encrypted
    content cannot be sent back (store is false), so the call goes back without ids."""
    client = app_client(env, tmp_path, models_text(main=", api: responses, temperature: 0.2"))
    bare = {"type": "reasoning", "id": "rs_2", "summary": []}
    provider.replies += [reply(bare, call_item("query", {"kind": "levels"}, "call_x"), usage=USAGE_NO_CACHE),
                         reply(message_item("L1, L2."))]
    assert client.post("/api/chat", json={"message": "levels?"}).status_code == 200
    first, second = provider.responses_bodies()
    assert "reasoning" not in first and "include" not in first
    assert first["temperature"] == 0.2 and first["max_output_tokens"] == 4096
    assert second["input"][2] == {"type": "function_call", "call_id": "call_x", "name": "query",
                                  "arguments": '{"kind": "levels"}'}
    assert second["input"][3]["type"] == "function_call_output"


def test_a_responses_planner_with_chat_roles(revit, env, keys, tmp_path, provider):
    """The planner on the responses api, the analyst and the supervisor on the chat api."""
    env.delenv("LLM_SINGLE_MODEL")
    client = app_client(env, tmp_path, models_text(main=", api: responses, reasoning_effort: low",
                                                   roles="analyst: small, planner: main, supervisor: small"),
                        ROLES_ANALYST="on")
    provider.chat.script("analyst", json_turn({"route": "tool", "tool": "create_wall", "facts_needed": [], "note": ""},
                                              usage=USAGE_SHAPES["deepseek"][0]))
    provider.chat.script("supervisor", json_turn({"verdict": "done", "report": "Done."}))
    provider.replies += [reply(REASONING, call_item("propose_spec", {"spec": spec_for("create_wall", **WALL)}, "c1"),
                               usage=USAGE),
                         reply(message_item("The card is below."), usage=USAGE_NO_CACHE)]
    resp = client.post("/api/chat", json={"message": "a wall on L1"})
    frames = sse(resp.text)
    assert len(events(frames, "spec")) == 1 and tokens(frames) == "The card is below."
    assert [s["url"] for s in provider.sent] == ["https://small.example/v1/chat/completions",
                                                 "https://main.example/v1/responses",
                                                 "https://main.example/v1/responses"]
    assert [(e["role"], e["model"]) for e in events(frames, "usage")] == [
        ("analyst", "small-model"), ("planner", "main-model"), ("planner", "main-model")]
    report = client.post("/api/chat", json={"execution": {"success": True, "evidence_id": "ev_1"},
                                            "session_id": resp.headers["X-Session-Id"]})
    assert tokens(sse(report.text)) == "Done."
    assert provider.sent[-1]["url"] == "https://small.example/v1/chat/completions"


def test_a_browser_model_on_a_responses_entry_uses_the_chat_api(revit, env, keys, tmp_path, provider):
    """The X-LLM-* headers replace the entry: the chat api with today's defaults."""
    client = app_client(env, tmp_path, models_text(main=", api: responses, reasoning_effort: high, timeout_s: 900"))
    provider.chat.script("single", text_turn("hello"))
    resp = client.post("/api/chat", json={"message": "hi"}, headers={"X-LLM-Model": "visitor-model"})
    assert tokens(sse(resp.text)) == "hello"
    sent = provider.sent[0]
    assert sent["url"] == "https://main.example/v1/chat/completions"
    assert (sent["body"]["model"], sent["body"]["max_tokens"], sent["body"]["temperature"]) == ("visitor-model", 4096, 0.3)
    assert "reasoning_effort" not in sent["body"]
    assert provider.timeouts[-1] == llm_module.DEFAULT_TIMEOUT == 300.0


@pytest.mark.parametrize("answer, problem", [
    (httpx.Response(400, json={"error": {"message": "Unsupported parameter: temperature"}}), "HTTP 400"),
    (reply(status="incomplete", incomplete={"reason": "max_output_tokens"}, usage=USAGE_NO_CACHE),
     "used up its output limit (max_output_tokens 2048)"),
    (reply(status="failed", usage=USAGE_NO_CACHE) | {"error": {"code": "server_error", "message": "x"}},
     "the response failed (server_error)"),
    (httpx.Response(200, text="not json"), "not a responses api object"),
])
def test_a_responses_failure_is_an_error_event(env, keys, tmp_path, provider, answer, problem):
    client = app_client(env, tmp_path, models_text(main=", api: responses, max_tokens: 2048"))
    provider.replies.append(answer)
    frames = sse(client.post("/api/chat", json={"message": "hi"}).text)
    assert problem in events(frames, "error")[0]["detail"]
    assert [s["status"] for s in events(frames, "stage")] == ["start", "end"]
    # An answered call may have cost tokens: its usage is reported (unknown when there was none).
    if not isinstance(answer, httpx.Response) or answer.status_code == 200:
        assert len(events(frames, "usage")) == 1


def test_responses_usage_mapping():
    assert parse_responses_usage(USAGE) == Usage(input=9668, output=255, cache_read=9594, cache_write=71)
    # No cache fields: unknown, never 0; output_tokens already holds the reasoning.
    assert parse_responses_usage({"input_tokens": 10, "output_tokens": 5,
                                  "output_tokens_details": {"reasoning_tokens": 4}}) == Usage(10, 5, None, None)
    assert parse_responses_usage(None) == Usage()
    assert parse_responses_usage({"input_tokens": -1, "output_tokens": "3"}) == Usage()


def test_responses_input_mapping_without_memory():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": "Reading.", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "query", "arguments": '{"kind":"levels"}'}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "[1]"},
                {"role": "assistant", "content": None}]
    assert responses_input(messages) == [
        {"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Reading."},
        {"type": "function_call", "call_id": "c1", "name": "query", "arguments": '{"kind":"levels"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "[1]"}]


# -- the chat api: extra keys on a tool call ----------------------------------------------------------

SIGNATURE = {"google": {"thought_signature": "made-up-signature-AAAA"}}


def test_extra_content_goes_back_on_its_call_and_nowhere_else(revit, env, keys, tmp_path, provider, caplog):
    caplog.set_level(logging.DEBUG)
    client = app_client(env, tmp_path, models_text())
    call = {"id": "fc-1", "name": "query", "arguments": {"kind": "levels"}, "extra": {"extra_content": SIGNATURE}}
    provider.chat.script("single", {"content": "", "tool_calls": [call]}, text_turn("L1 and L2."))
    resp = client.post("/api/chat", json={"message": "levels?"})
    assert tokens(sse(resp.text)) == "L1 and L2."
    first, second = (s["body"] for s in provider.sent)
    sent_call = next(m for m in second["messages"] if m["role"] == "assistant")["tool_calls"][0]
    assert sent_call == {"id": "fc-1", "type": "function",
                         "function": {"name": "query", "arguments": '{"kind": "levels"}'}, "extra_content": SIGNATURE}
    # The session keeps the plain message; the signature lives in the session's memory only.
    session = get_session_store().get(resp.headers["X-Session-Id"])
    assert "made-up-signature" not in json.dumps(session.history)
    assert "made-up-signature" not in repr(session)
    # The next turn to the same endpoint and model sends it again ...
    provider.chat.script("single", text_turn("ok"))
    client.post("/api/chat", json={"message": "again", "session_id": resp.headers["X-Session-Id"]})
    assert "made-up-signature" in json.dumps(provider.sent[-1]["body"])
    # ... a turn to another model does not.
    provider.chat.script("single", text_turn("ok"))
    client.post("/api/chat", json={"message": "again", "session_id": resp.headers["X-Session-Id"]},
                headers={"X-LLM-Model": "other-model"})
    assert "made-up-signature" not in json.dumps(provider.sent[-1]["body"])
    assert "made-up-signature" not in caplog.text
    assert "made-up-signature" not in json.dumps(get_log_store().query(limit=50))


def test_extra_keys_are_kept_per_call_and_provider(monkeypatch):
    memory = ProviderMemory(limit=2)
    first = ToolCall("call_0", "query", "{}", extra={"extra_content": 1})
    memory.remember("u\nm", first, {"extra_content": 1})
    part = first.as_message_part()
    messages = [{"role": "assistant", "content": None, "tool_calls": [part]}]
    assert with_call_extras(messages, memory, "u\nm")[0]["tool_calls"][0]["extra_content"] == 1
    assert with_call_extras(messages, memory, "other\nm") is messages             # another endpoint
    same_id = {**part, "function": {"name": "query", "arguments": '{"kind": "x"}'}}
    assert memory.recall("u\nm", same_id) is None                                # same id, another call
    assert messages[0]["tool_calls"][0] == part                                  # never changed in place
    for i in range(3):
        memory.remember("u\nm", ToolCall(f"c{i}", "q", "{}"), {"k": i})
    assert len(memory) == 2 and memory.recall("u\nm", part) is None              # oldest forgotten
    assert with_call_extras(messages, None, "u\nm") is messages


# -- max_completion_tokens, reasoning_effort, timeout_s --------------------------------------------------

def test_max_completion_tokens_effort_and_timeout_reach_the_chat_request(env, keys, tmp_path, provider):
    client = app_client(env, tmp_path, models_text(
        main=", max_tokens: 8000, max_tokens_param: max_completion_tokens, reasoning_effort: high, timeout_s: 600"))
    provider.chat.script("single", text_turn("ok"))
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 200
    body = provider.sent[0]["body"]
    assert body["max_completion_tokens"] == 8000 and "max_tokens" not in body
    assert body["reasoning_effort"] == "high" and body["temperature"] == 0.3
    assert list(body)[:5] == ["model", "messages", "temperature", "max_completion_tokens", "stream"]
    assert provider.timeouts[-1] == 600.0


def test_a_long_timeout_extends_the_call_deadline():
    assert llm_module.call_deadline(llm_module.DEFAULT_TIMEOUT) == llm_module.MODEL_CALL_DEADLINE
    assert llm_module.call_deadline(10) == llm_module.MODEL_CALL_DEADLINE
    assert llm_module.call_deadline(900) >= 900 + llm_module.MODEL_CALL_DEADLINE - llm_module.DEFAULT_TIMEOUT


GOOD = "models:\n  main: {base_url: https://a.example/v1, model: m, api_key_env: K%s}\nroles: {planner: main}\n"


@pytest.mark.parametrize("fields, problem", [
    (", api: completions", "models.main.api: must be one of chat, responses"),
    (", api: Responses", "models.main.api: must be one of chat, responses"),
    (", max_tokens_param: max_output_tokens", "models.main.max_tokens_param: must be one of max_tokens, "
                                              "max_completion_tokens"),
    (", api: responses, max_tokens_param: max_completion_tokens", "max_tokens_param: chat api only"),
    (", reasoning_effort: minimal", "models.main.reasoning_effort: must be one of low, medium, high, xhigh"),
    (", reasoning_effort: none", "models.main.reasoning_effort: must be one of"),
    (", reasoning_effort: sk-made-up-0123456789abcdef", "models.main.reasoning_effort: must be one of"),
    (", timeout_s: 9", "models.main.timeout_s: must be a whole number of seconds from 10 to 900"),
    (", timeout_s: 901", "models.main.timeout_s: must be a whole number"),
    (", timeout_s: '60'", "models.main.timeout_s: must be a whole number"),
    (", timeout_s: 60.5", "models.main.timeout_s: must be a whole number"),
    (", timeout_s: true", "models.main.timeout_s: must be a whole number"),
    (", top_p: 0.5", "models.main: 'top_p' is not a field of an entry (base_url, model, api_key_env, max_tokens, "
                     "temperature, api, max_tokens_param, reasoning_effort, timeout_s)"),
])
def test_bad_provider_options_refuse_the_start(tmp_path, fields, problem):
    with pytest.raises(ConfigError) as exc:
        parse_models(yaml.safe_load(GOOD % fields), tmp_path / "models.yml")
    assert problem in str(exc.value)
    assert "sk-made-up" not in str(exc.value)


def test_good_provider_options_are_read(tmp_path):
    models = parse_models(yaml.safe_load(GOOD % (", api: responses, reasoning_effort: xhigh, timeout_s: 900")),
                          tmp_path / "models.yml")
    entry = models.entries["main"]
    assert (entry.api, entry.max_tokens_param, entry.reasoning_effort, entry.timeout_s) == (
        "responses", None, "xhigh", 900)
    models = parse_models(yaml.safe_load(GOOD % (", api: chat, max_tokens_param: max_tokens, timeout_s: 10")),
                          tmp_path / "models.yml")
    entry = models.entries["main"]
    assert (entry.api, entry.max_tokens_param, entry.timeout_s) == ("chat", "max_tokens", 10)


def commented_examples(text: str) -> list[str]:
    """The commented example files of models.example.yml: each run of ``# `` lines from a
    ``# models:`` line to the next bare ``#``, uncommented."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if line == "# models:":
            current = []
            blocks.append(current)
        if current is not None:
            if not line.startswith("# "):
                current = None
                continue
            current.append(line[2:])
    return ["\n".join(lines) + "\n" for lines in blocks]


def test_the_example_file_and_its_commented_examples_are_valid(tmp_path):
    example = Path(__file__).resolve().parent.parent / "models.example.yml"
    models = load_models(example, explicit=True)
    assert models is not None and models.entry("planner")
    blocks = commented_examples(example.read_text(encoding="utf-8"))
    assert len(blocks) == 4                       # one model, two providers, OpenAI, Gemini
    read = [parse_models(yaml.safe_load(text), tmp_path / "x.yml") for text in blocks]
    openai = next(m for m in read if "api.openai.com" in m.entry("planner").base_url)
    assert openai.entry("planner").api == "responses" and openai.entry("planner").reasoning_effort == "low"
    assert openai.entry("supervisor").api is None
    gemini = next(m for m in read if "generativelanguage" in m.entry("planner").base_url)
    assert gemini.entry("planner").timeout_s == 600 and gemini.entry("planner").max_tokens == 16384


# -- an output limit used up by thinking ----------------------------------------------------------------

def test_an_empty_reply_at_the_output_limit_is_an_error_naming_it(env, keys, tmp_path, provider):
    client = app_client(env, tmp_path, models_text(main=", max_tokens: 1024"))
    provider.chat.script("single", {"content": "", "finish": "length", "usage": USAGE_SHAPES["gemini"][0]})
    resp = client.post("/api/chat", json={"message": "hi"})
    frames = sse(resp.text)
    detail = events(frames, "error")[0]["detail"]
    assert "used up its output limit (max_tokens 1024)" in detail
    assert tokens(frames) == "" and len(events(frames, "usage")) == 1          # the call cost tokens
    assert get_session_store().get(resp.headers["X-Session-Id"]).history == [{"role": "user", "content": "hi"}]


def test_a_cut_reply_with_text_or_a_call_is_still_a_reply(monkeypatch):
    """finish_reason length after some text, or with a tool call: as before, not an error."""
    provider = Provider()
    provider.chat.script("single", {"content": "Half a sen", "finish": "length"},
                         {"content": "", "finish": "length", "tool_calls": [{"name": "list_tools", "arguments": {}}]})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": provider.transport}))
    llm = LLMSettings(base_url="https://m.example/v1", model="m", api_key="made-up")

    async def run():
        return [item async for item in stream_completion(llm, [{"role": "user", "content": "x"}], stats=CallStats())]

    assert "".join(asyncio.run(run())) == "Half a sen"
    items = asyncio.run(run())
    assert [c.name for c in items[-1]] == ["list_tools"]
