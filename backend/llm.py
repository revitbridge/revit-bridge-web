"""The one model client of the host: OpenAI-compatible chat completions, streamed.

Bring your own model. The browser sends ``X-LLM-Base-Url`` / ``X-LLM-Model``
/ ``X-LLM-Key`` with each request; when a header is absent the server-side
``LLM_BASE_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY`` fill in. Settings live for
one request only: nothing is written to disk and the key never appears in a
log line, an error message or a ``repr``.

``stream_completion`` speaks the OpenAI-compatible function-calling
protocol: ``tools`` go out with the request, streamed ``tool_calls`` deltas
(one fragment per chunk, keyed by ``index``) are assembled and handed back
as one ``list[ToolCall]`` once the model is done, and ``tool`` role messages
carry the results back in the next request.

Ported from the streaming half of the former ``pipeline/llm_client.py``,
rewritten on ``httpx.AsyncClient`` so the event loop is never blocked.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from backend.config import WebSettings

_log = logging.getLogger("backend.llm")

HEADER_BASE_URL = "x-llm-base-url"
HEADER_MODEL = "x-llm-model"
HEADER_KEY = "x-llm-key"

DEFAULT_TIMEOUT = 120.0


class LLMError(Exception):
    """The model endpoint refused or failed; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    source: str = "server"  # "browser" when any header supplied a value

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    @classmethod
    def resolve(cls, headers: Mapping[str, str], settings: WebSettings) -> LLMSettings:
        """Browser headers first, then the server defaults."""
        base_url = (headers.get(HEADER_BASE_URL) or "").strip().rstrip("/")
        model = (headers.get(HEADER_MODEL) or "").strip()
        api_key = (headers.get(HEADER_KEY) or "").strip()
        from_browser = bool(base_url or model or api_key)
        if base_url:
            _check_base_url(base_url, settings)
        return cls(
            base_url=base_url or settings.llm_base_url,
            model=model or settings.llm_model,
            api_key=api_key or settings.llm_api_key,
            source="browser" if from_browser else "server",
        )

    def describe(self) -> dict:
        """Safe to show or log: never the key itself."""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "set" if self.api_key else "not set",
            "source": self.source,
        }


def _check_base_url(base_url: str, settings: WebSettings) -> None:
    """A browser-supplied endpoint must be an absolute https URL.

    The server would otherwise POST to whatever address a visitor names
    (metadata services, internal hosts). Plain http is opt-in for local
    setups via ``LLM_ALLOW_HTTP=1``.
    """
    parsed = urlparse(base_url)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if parsed.scheme == "http" and parsed.netloc and settings.llm_allow_http:
        return
    raise LLMError(
        "X-LLM-Base-Url must be an absolute https:// URL "
        "(set LLM_ALLOW_HTTP=1 on the server to allow http://)",
        status=400,
    )


# -- function calling ------------------------------------------------------------

@dataclass
class ToolCall:
    """One function call the model asked for; ``arguments`` is the raw JSON text."""
    id: str
    name: str
    arguments: str

    def as_message_part(self) -> dict:
        """The entry of an assistant message's ``tool_calls`` list."""
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


def assistant_message(content: str, tool_calls: list[ToolCall] | None = None) -> dict:
    """The assistant turn to keep in the history: its text and/or the calls it made."""
    message: dict = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = [c.as_message_part() for c in tool_calls]
    return message


def tool_message(call: ToolCall, result) -> dict:
    """A tool result for the next request; ``result`` is JSON-encoded unless it is text."""
    content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": call.id, "content": content}


def _merge_tool_call(calls: dict[int, dict], fragment: dict) -> None:
    """Fold one streamed ``tool_calls`` delta into the call it belongs to."""
    if not isinstance(fragment, dict):
        return
    try:
        index = int(fragment.get("index", len(calls)))
    except (TypeError, ValueError):
        index = len(calls)
    call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
    if fragment.get("id"):
        call["id"] = str(fragment["id"])
    function = fragment.get("function") or {}
    if function.get("name"):
        call["name"] += str(function["name"])
    if function.get("arguments"):
        call["arguments"] += str(function["arguments"])


async def stream_completion(
    llm: LLMSettings,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = DEFAULT_TIMEOUT,
) -> AsyncIterator[str | list[ToolCall]]:
    """Stream one chat completion: content deltas as they arrive (``str``) and,
    when the model called tools, one ``list[ToolCall]`` at the end, assembled
    from the streamed fragments (each call's ``id`` and ``name`` arrive once,
    its ``arguments`` in pieces, all keyed by ``index``).

    Raises ``LLMError`` when the endpoint answers with an error status; the
    message carries the status and a short body excerpt, never the request.
    """
    if not llm.configured:
        raise LLMError(
            "No model configured: fill in Model settings in the browser "
            "or set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY on the server.",
            status=400,
        )
    url = f"{llm.base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-Title": "revit-bridge-web",
    }
    payload: dict = {
        "model": llm.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    calls: dict[int, dict] = {}          # index -> {id, name, arguments}
    client_timeout = httpx.Timeout(timeout, connect=15.0)
    try:
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:300]
                    raise LLMError(f"Model endpoint returned HTTP {resp.status_code}: {body}",
                                   status=502)
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        yield token
                    for fragment in delta.get("tool_calls") or []:
                        _merge_tool_call(calls, fragment)
    except httpx.HTTPError as exc:
        # httpx error text names the URL, not the headers.
        _log.warning("model request failed (%s): %s", llm.model, type(exc).__name__)
        raise LLMError(f"Model endpoint unreachable: {type(exc).__name__}", status=502) from None
    assembled = [ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"])
                 for i, c in sorted(calls.items()) if c["name"]]
    if assembled:
        yield assembled


async def stream_chat(
    llm: LLMSettings,
    messages: list[dict],
    *,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    timeout: float = DEFAULT_TIMEOUT,
) -> AsyncIterator[str]:
    """Content deltas only, no tools: the 0.1 surface, for callers that want plain text."""
    async for item in stream_completion(llm, messages, temperature=temperature,
                                        max_tokens=max_tokens, timeout=timeout):
        if isinstance(item, str):
            yield item


# -- SSE frames --------------------------------------------------------------------

def format_sse(data: str) -> str:
    """One SSE frame carrying a JSON-encoded string token."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def format_sse_event(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_DONE = "event: done\ndata: [DONE]\n\n"
