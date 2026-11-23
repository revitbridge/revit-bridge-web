"""Token usage of one model call: the provider shapes ``parse_usage`` reads, the usage
option on the request, and the retry without it for an endpoint that refuses it."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from backend import llm as llm_module
from backend.llm import CallStats, LLMError, LLMSettings, Usage, parse_usage, stream_completion
from tests.fake_model import USAGE_SHAPES, FakeModel, text_turn, tool_turn


def use_model(monkeypatch, fake: FakeModel) -> FakeModel:
    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = fake.transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)
    return fake


async def _collect(llm: LLMSettings, stats: CallStats, tools=None) -> list:
    return [item async for item in stream_completion(llm, [{"role": "user", "content": "x"}], tools=tools,
                                                     stats=stats)]


LLM = LLMSettings(base_url="https://model.example/v1", model="m", api_key="fake-key-not-real")


# -- usage parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("shape", sorted(USAGE_SHAPES))
def test_parse_usage_reads_every_documented_shape(shape):
    raw, (input_, output, cache_read, cache_write) = USAGE_SHAPES[shape]
    assert parse_usage(raw) == Usage(input=input_, output=output, cache_read=cache_read, cache_write=cache_write)


def test_parse_usage_absent_is_none_never_zero():
    assert parse_usage(None) == Usage() and parse_usage("12") == Usage() and parse_usage([1]) == Usage()
    assert parse_usage({"prompt_tokens": 10}) == Usage(input=10)
    assert parse_usage({"completion_tokens": 3}) == Usage(output=3)
    # A details object that is there but empty says nothing about the cache.
    assert parse_usage({"prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {}}) == \
        Usage(input=10, output=3)
    # An explicit zero stays zero.
    assert parse_usage({"prompt_tokens": 0, "completion_tokens": 0,
                        "prompt_tokens_details": {"cached_tokens": 0}}) == Usage(0, 0, 0, None)
    assert parse_usage({"prompt_tokens": 12.0}) == Usage(input=12)


@pytest.mark.parametrize("delivery", ["final_chunk", "finish_chunk", "every_chunk"])
def test_stream_completion_asks_for_usage_and_reads_the_last_object(monkeypatch, delivery):
    raw, expected = USAGE_SHAPES["openai"]
    fake = use_model(monkeypatch, FakeModel([tool_turn(("list_tools", {}), content="Looking.", usage=raw)],
                                            usage_delivery=delivery))
    stats = CallStats()
    items = asyncio.run(_collect(LLM, stats, tools=[{"type": "function", "function": {"name": "list_tools"}}]))
    # Content and calls exactly as before the usage chunk existed.
    assert "".join(i for i in items if isinstance(i, str)) == "Looking."
    assert [c.name for c in items[-1]] == ["list_tools"]
    assert fake.requests[0]["stream_options"] == {"include_usage": True}
    assert stats.accepted and stats.asked_usage and not stats.retried
    assert (stats.usage.input, stats.usage.output, stats.usage.cache_read, stats.usage.cache_write) == expected


def test_a_stream_without_usage_leaves_every_count_unknown(monkeypatch):
    use_model(monkeypatch, FakeModel([text_turn("hi")]))
    stats = CallStats()
    assert "".join(asyncio.run(_collect(LLM, stats))) == "hi"
    assert stats.usage == Usage() and stats.accepted and stats.asked_usage


def test_an_endpoint_that_refuses_the_option_is_asked_again_without_it_once(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="backend.llm")
    fake = use_model(monkeypatch, FakeModel([text_turn("one", usage=USAGE_SHAPES["openai"][0]),
                                             text_turn("two", usage=USAGE_SHAPES["openai"][0])],
                                            refuse_usage_option=True))
    stats = CallStats()
    assert "".join(asyncio.run(_collect(LLM, stats))) == "one"
    assert len(fake.refused) == 1 and "stream_options" in fake.refused[0]
    assert "stream_options" not in fake.requests[0]
    # The retried call did not ask, and this provider sends usage only when asked: unknown.
    assert stats.retried and stats.accepted and not stats.asked_usage and stats.usage == Usage()
    assert "naming stream_options" in caplog.text and "not asking this endpoint and model" in caplog.text
    assert "fake-key-not-real" not in caplog.text
    # The endpoint is remembered: the next call does not pay the refused attempt again.
    later = CallStats()
    assert "".join(asyncio.run(_collect(LLM, later))) == "two"
    assert len(fake.refused) == 1 and "stream_options" not in fake.requests[1]
    assert not later.retried and not later.asked_usage and later.usage == Usage()


def test_after_a_refusal_usage_sent_anyway_is_still_read(monkeypatch):
    """DeepSeek documents usage on the last chunk even without the option: when an endpoint
    that refused the option sends it anyway, it is read (the retried call included)."""
    raw, expected = USAGE_SHAPES["deepseek"]
    use_model(monkeypatch, FakeModel([text_turn("x", usage=raw)], refuse_usage_option=True,
                                     usage_needs_option=False, usage_delivery="finish_chunk"))
    stats = CallStats()
    asyncio.run(_collect(LLM, stats))
    assert stats.retried and (stats.usage.input, stats.usage.output, stats.usage.cache_read) == expected[:3]


def _scripted(monkeypatch, answers):
    """A transport that answers each request with the next of ``answers`` (status, text) and
    then with a short stream; returns the request bodies it saw."""
    seen: list[dict] = []
    ok = 'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        status, text = answers.pop(0) if answers else (200, ok)
        return httpx.Response(status, text=text)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    return seen


def test_other_refusals_are_not_retried(monkeypatch):
    seen = _scripted(monkeypatch, [
        (400, '{"error": "max_tokens is too large"}'),
        (401, '{"error": "invalid key", "request": {"stream_options": {"include_usage": true}}}'),
        (403, '{"error": "include_usage not allowed for this key"}'),
    ])
    with pytest.raises(LLMError, match="HTTP 400"):
        asyncio.run(_collect(LLM, CallStats()))
    assert len(seen) == 1                         # a 4xx that does not name the option: no retry
    # 401 and 403 are about the key, whatever the body echoes: never retried, never remembered.
    with pytest.raises(LLMError, match="HTTP 401"):
        asyncio.run(_collect(LLM, CallStats()))
    with pytest.raises(LLMError, match="HTTP 403"):
        asyncio.run(_collect(LLM, CallStats()))
    assert len(seen) == 3
    stats = CallStats()
    asyncio.run(_collect(LLM, stats))
    assert "stream_options" in seen[3] and not stats.retried   # still asked
    assert llm_module._REFUSES_USAGE_OPTION == {}


def test_a_refusal_is_remembered_only_once_the_retry_is_accepted(monkeypatch):
    seen = _scripted(monkeypatch, [
        # 400 naming the option, and the retry without it fails for another reason: not remembered.
        (400, '{"error": {"message": "bad request", "param": "stream_options"}}'),
        (400, '{"error": "max_tokens is too large"}'),
    ])
    with pytest.raises(LLMError, match="max_tokens"):
        asyncio.run(_collect(LLM, CallStats()))
    assert "stream_options" in seen[0] and "stream_options" not in seen[1]
    asyncio.run(_collect(LLM, CallStats()))
    assert "stream_options" in seen[2]            # still asked: the refusal was never confirmed
    assert llm_module._REFUSES_USAGE_OPTION == {}


def test_the_memory_is_per_address_and_model(monkeypatch):
    """One gateway, several providers behind it: a model that refuses the option does not
    switch usage off for the others."""
    seen = _scripted(monkeypatch, [(400, '{"error": "Unrecognized request argument supplied: stream_options"}')])
    refusing = LLMSettings(base_url="https://gateway.example/v1", model="old-model", api_key="k")
    stats = CallStats()
    asyncio.run(_collect(refusing, stats))
    assert stats.retried and stats.accepted and len(seen) == 2
    assert set(llm_module._REFUSES_USAGE_OPTION) == {("https://gateway.example/v1/chat/completions", "old-model")}
    asyncio.run(_collect(refusing, CallStats()))
    assert "stream_options" not in seen[2]        # remembered for this address and model
    other_model = LLMSettings(base_url="https://gateway.example/v1", model="new-model", api_key="k")
    asyncio.run(_collect(other_model, CallStats()))
    assert "stream_options" in seen[3]            # same gateway, another model: still asked
    other_address = LLMSettings(base_url="https://other.example/v1", model="old-model", api_key="k")
    asyncio.run(_collect(other_address, CallStats()))
    assert "stream_options" in seen[4]


def test_usage_objects_merge_field_by_field(monkeypatch):
    """A later empty or partial usage object never erases a number already read; a later
    number replaces an earlier one (a running total ends at its last value)."""
    chunks = [
        {"choices": [{"delta": {"content": "a"}}], "usage": {"prompt_tokens": 900, "completion_tokens": 1}},
        {"choices": [{"delta": {"content": "b"}}], "usage": {"completion_tokens": 7}},
        {"choices": [], "usage": {"prompt_tokens_details": {"cached_tokens": 512}}},
        {"choices": [], "usage": {}},
        {"choices": [], "usage": {"prompt_tokens": None, "completion_tokens": "x"}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": transport}))
    stats = CallStats()
    assert "".join(asyncio.run(_collect(LLM, stats))) == "ab"
    assert stats.usage == Usage(input=900, output=7, cache_read=512, cache_write=None)
    assert Usage(1, 2, 3, 4).merged(Usage()) == Usage(1, 2, 3, 4)
    assert Usage(1, 2, None, None).merged(Usage(None, 5, 0, None)) == Usage(1, 5, 0, None)


def test_a_split_usage_with_the_total_first_still_counts_the_thinking():
    """Gemini's thinking is in total_tokens only: the rule needs all three numbers of one
    object, so a total without the others changes nothing."""
    assert parse_usage({"total_tokens": 1725}) == Usage()
    assert parse_usage({"prompt_tokens": 758, "total_tokens": 1725}) == Usage(input=758)
