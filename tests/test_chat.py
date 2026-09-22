"""Bring-your-own-model chat: header precedence, streaming, no key at rest."""
from __future__ import annotations

import pytest

from backend import llm as llm_module
from backend.api import chat as chat_module
from backend.config import WebSettings
from backend.llm import LLMError, LLMSettings
from backend.log_store import get_log_store
from backend.session import get_session_store

SERVER = WebSettings.from_env({"LLM_BASE_URL": "https://srv.example/v1", "LLM_MODEL": "srv-model", "LLM_API_KEY": "srv-key"})


def test_headers_win_over_server_defaults():
    resolved = LLMSettings.resolve({"x-llm-model": "mine", "x-llm-key": "my-key"}, SERVER)
    assert (resolved.base_url, resolved.model, resolved.api_key) == ("https://srv.example/v1", "mine", "my-key")
    assert resolved.source == "browser"
    assert "my-key" not in repr(resolved)
    assert resolved.describe()["api_key"] == "set"

    resolved = LLMSettings.resolve({}, SERVER)
    assert resolved.source == "server" and resolved.api_key == "srv-key"

    assert not LLMSettings.resolve({}, WebSettings.from_env({})).configured


def test_browser_base_url_must_be_https_unless_allowed():
    with pytest.raises(LLMError) as exc:
        LLMSettings.resolve({"x-llm-base-url": "http://169.254.169.254/v1"}, SERVER)
    assert exc.value.status == 400
    with pytest.raises(LLMError):
        LLMSettings.resolve({"x-llm-base-url": "ftp://x"}, SERVER)
    ok = LLMSettings.resolve({"x-llm-base-url": "https://api.example/v1/"}, SERVER)
    assert ok.base_url == "https://api.example/v1"
    relaxed = WebSettings.from_env({"LLM_ALLOW_HTTP": "1"})
    assert LLMSettings.resolve({"x-llm-base-url": "http://localhost:11434/v1"}, relaxed).base_url.startswith("http://")


def test_chat_refuses_without_a_model(client):
    resp = client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 400
    assert "No model configured" in resp.json()["detail"]


def _fake_stream(tokens, seen: dict):
    async def stream_chat(llm, messages, **kwargs):
        seen["llm"] = llm
        seen["messages"] = messages
        for t in tokens:
            yield t
    return stream_chat


def test_chat_streams_tokens_keeps_history_and_never_stores_the_key(client, env, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(chat_module, "stream_chat", _fake_stream(["Hel", "lo ", "designer"], seen))

    headers = {"X-LLM-Base-Url": "https://api.example/v1", "X-LLM-Model": "gpt-x", "X-LLM-Key": "sk-browser-only"}
    resp = client.post("/api/chat", json={"message": "hi there"}, headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    session_id = resp.headers["X-Session-Id"]
    assert resp.text == 'data: "Hel"\n\ndata: "lo "\n\ndata: "designer"\n\nevent: done\ndata: [DONE]\n\n'

    # The model got the host prompt, the pack index and the user turn.
    assert seen["llm"].model == "gpt-x" and seen["llm"].api_key == "sk-browser-only"
    system, user = seen["messages"][0], seen["messages"][-1]
    assert system["role"] == "system" and "create_wall" in system["content"]
    assert user == {"role": "user", "content": "hi there"}

    # Second turn on the same session carries the first exchange.
    resp = client.post("/api/chat", json={"message": "again", "session_id": session_id}, headers=headers)
    assert resp.headers["X-Session-Id"] == session_id
    roles = [m["role"] for m in seen["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert get_session_store().get(session_id).history[-1]["content"] == "Hello designer"

    # Interaction log holds the text and the model name, never the key.
    rows = get_log_store().query()["items"]
    assert rows[0]["module"] == "chat" and rows[0]["model"] == "gpt-x"
    assert rows[0]["assistant_output"] == "Hello designer"
    assert "sk-browser-only" not in "".join(str(v) for row in rows for v in row.values())


def test_chat_reports_model_errors_as_an_sse_event(client, monkeypatch):
    async def failing(llm, messages, **kwargs):
        raise LLMError("Model endpoint returned HTTP 401: bad key", status=502)
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(chat_module, "stream_chat", failing)
    headers = {"X-LLM-Base-Url": "https://api.example/v1", "X-LLM-Model": "m", "X-LLM-Key": "k"}
    resp = client.post("/api/chat", json={"message": "hi"}, headers=headers)
    assert resp.status_code == 200
    assert resp.text.startswith('event: error\ndata: {"detail": "Model endpoint returned HTTP 401')

    # The interaction log records the model failure, so stats count it.
    row = get_log_store().query()["items"][0]
    assert row["status"] == "error: Model endpoint returned HTTP 401: bad key"
    assert row["assistant_output"] == ""  # the error frame is not reply text
    stats = get_log_store().stats()
    assert stats["total"] == 1 and stats["errors"] == 1


def test_sse_frame_classification():
    from backend.log_store import sse_error_status, sse_tokens

    chunk = 'data: "Hel"\n\ndata: "lo"\n\nevent: done\ndata: [DONE]\n\n'
    assert sse_tokens(chunk) == ["Hel", "lo"]
    assert sse_error_status(chunk) is None

    error = 'event: error\ndata: {"detail": "upstream closed"}\n\n'
    assert sse_tokens(error) == []
    assert sse_error_status(error) == "error: upstream closed"
    assert sse_error_status('event: error\ndata: "plain text"\n\n') == "error: plain text"
    assert sse_tokens('data: {"not": "a token"}\n\n') == []


def test_chat_rate_limit(make_client, env):
    env.setenv("CHAT_RATE_LIMIT", "2")
    client = make_client()
    headers = {"X-LLM-Base-Url": "https://api.example/v1", "X-LLM-Model": "m"}  # no key -> 400 after the limiter
    assert client.post("/api/chat", json={"message": "1"}, headers=headers).status_code == 400
    assert client.post("/api/chat", json={"message": "2"}, headers=headers).status_code == 400
    assert client.post("/api/chat", json={"message": "3"}, headers=headers).status_code == 429


def test_stream_chat_parses_openai_sse(monkeypatch):
    """The httpx layer: SSE deltas become tokens, error statuses become LLMError."""
    import asyncio

    import httpx

    body = (
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        if request.headers.get("authorization") == "Bearer bad":
            return httpx.Response(401, text='{"error":"bad key"}')
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)

    async def collect(key):
        settings = LLMSettings(base_url="https://api.example/v1", model="m", api_key=key)
        return [t async for t in llm_module.stream_chat(settings, [{"role": "user", "content": "x"}])]

    assert asyncio.run(collect("good")) == ["A", "B"]
    assert captured["url"] == "https://api.example/v1/chat/completions"
    assert captured["auth"] == "Bearer good"

    with pytest.raises(LLMError) as exc:
        asyncio.run(collect("bad"))
    assert exc.value.status == 502 and "HTTP 401" in str(exc.value) and "Bearer" not in str(exc.value)


def test_rate_limiter_forgets_idle_addresses():
    """The hit table must not keep every visitor since process start (item 8)."""
    from backend.ratelimit import RateLimiter

    clock = {"now": 1_000.0}
    limiter = RateLimiter(clock=lambda: clock["now"])

    assert limiter.allow("10.0.0.1", 2) and limiter.allow("10.0.0.2", 2)
    assert limiter.keys() == {"10.0.0.1", "10.0.0.2"}

    clock["now"] += 30
    assert limiter.allow("10.0.0.1", 2)  # still inside the window: nothing pruned
    assert limiter.keys() == {"10.0.0.1", "10.0.0.2"}

    clock["now"] += 31  # 10.0.0.2's only hit is now older than the 60 s window
    assert limiter.allow("10.0.0.3", 2)
    assert limiter.keys() == {"10.0.0.1", "10.0.0.3"}

    clock["now"] += 61  # everything idle; a new visitor leaves only itself behind
    assert limiter.allow("10.0.0.4", 2)
    assert limiter.keys() == {"10.0.0.4"}

    # The limit itself still applies within a window; 0 disables it.
    assert limiter.allow("10.0.0.4", 2)
    assert limiter.allow("10.0.0.4", 2) is False
    assert limiter.allow("10.0.0.4", 0)
