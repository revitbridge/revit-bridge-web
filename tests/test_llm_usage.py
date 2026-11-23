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
    assert "refused stream_options" in caplog.text and "fake-key-not-real" not in caplog.text
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


def test_other_refusals_are_not_retried(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return httpx.Response(400, text='{"error": "max_tokens is too large"}')
        return httpx.Response(401, text='{"error": "stream_options needs a paid key"}')

    real_client = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    with pytest.raises(LLMError, match="HTTP 400"):
        asyncio.run(_collect(LLM, CallStats()))
    assert len(seen) == 1                         # a 4xx that does not name the option: no retry
    # A 401 naming it is still a 4xx naming it: retried once, then the refusal stands.
    with pytest.raises(LLMError, match="HTTP 4"):
        asyncio.run(_collect(LLM, CallStats()))
    assert "stream_options" in seen[1] and "stream_options" not in seen[2] and len(seen) == 3
    # The memory is per endpoint: another address still asks.
    other = LLMSettings(base_url="https://other.example/v1", model="m", api_key="k")
    with pytest.raises(LLMError):
        asyncio.run(_collect(other, CallStats()))
    assert "stream_options" in seen[3]
