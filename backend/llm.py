"""The one model client of the host: OpenAI-compatible chat completions, streamed.

Bring your own model. The browser sends ``X-LLM-Base-Url`` / ``X-LLM-Model``
/ ``X-LLM-Key`` with each request; when a header is absent the server-side
``LLM_BASE_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY`` fill in - except that the
server's key only ever goes to the server's own ``LLM_BASE_URL``: a request
that names its own endpoint must bring its own key. A browser-supplied
endpoint must be an https URL on a public address (``LLM_ALLOW_HTTP=1`` lifts
both rules for a model on the operator's own network). Settings live for
one request only: nothing is written to disk and the key never appears in a
log line, an error message or a ``repr``.

``stream_completion`` speaks the OpenAI-compatible function-calling
protocol: ``tools`` go out with the request, streamed ``tool_calls`` deltas
(one fragment per chunk, keyed by ``index``) are assembled and handed back
as one ``list[ToolCall]`` once the model is done, and ``tool`` role messages
carry the results back in the next request.

Token usage: every request asks for it (``stream_options.include_usage``) and
the stream's ``usage`` objects are read into a ``CallStats`` the caller passes
in (``parse_usage``: four numbers, ``None`` for what the provider did not
report, never 0; merged field by field). An endpoint that refuses the option
with a 4xx naming it is asked again once without it; when that is accepted the
endpoint and model are remembered for the life of the process.

Ported from the streaming half of the former ``pipeline/llm_client.py``,
rewritten on ``httpx.AsyncClient`` so the event loop is never blocked.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import socket
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field

import httpx

from backend.config import WebSettings

_log = logging.getLogger("backend.llm")

HEADER_BASE_URL = "x-llm-base-url"
HEADER_MODEL = "x-llm-model"
HEADER_KEY = "x-llm-key"

DEFAULT_TIMEOUT = 120.0
# One completion from request to last byte. 4096 tokens at a slow 7 tokens a second
# take under 10 minutes; DEFAULT_TIMEOUT still bounds every single read.
MODEL_CALL_DEADLINE = 600.0


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
    custom_endpoint: bool = False  # base_url came from X-LLM-Base-Url

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    @classmethod
    def resolve(cls, headers: Mapping[str, str], settings: WebSettings) -> LLMSettings:
        """Browser headers first, then the server defaults - but never the server's key
        with the browser's endpoint.

        ``X-LLM-Base-Url`` present: the request goes there with ``X-LLM-Key`` (400 when
        it is missing - even when the URL is the server's own, written differently) and
        ``X-LLM-Model`` or the server's model name. Absent: the request goes to
        ``LLM_BASE_URL`` with ``X-LLM-Key`` or ``LLM_API_KEY`` and ``X-LLM-Model`` or
        ``LLM_MODEL``; a visitor's own key or model choice may use the server's endpoint.
        No network here: a name in the URL is checked by ``resolve_llm``.
        """
        base_url = (headers.get(HEADER_BASE_URL) or "").strip().rstrip("/")
        model = (headers.get(HEADER_MODEL) or "").strip()
        api_key = (headers.get(HEADER_KEY) or "").strip()
        from_browser = bool(base_url or model or api_key)
        if base_url:
            _check_base_url(base_url, settings)
            if not api_key:
                raise LLMError(
                    "X-LLM-Key is required with X-LLM-Base-Url: the server's key is only "
                    "sent to the server's own model endpoint",
                    status=400,
                )
            return cls(base_url=base_url, model=model or settings.llm_model, api_key=api_key,
                       source="browser", custom_endpoint=True)
        return cls(
            base_url=settings.llm_base_url,
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


_REFUSED_ADDRESS = (
    "X-LLM-Base-Url must not point at a loopback, private, link-local, multicast or "
    "reserved address (set LLM_ALLOW_HTTP=1 on the server for a model on its own network)"
)


_INVALID_URL = "X-LLM-Base-Url is not a valid URL"
_HOST_LABEL = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")


def completions_url(base_url: str) -> str:
    """The address one completion is POSTed to."""
    return f"{base_url}/chat/completions"


def _valid_host(host: str) -> bool:
    """An IP literal or a DNS name (labels of letters, digits, ``-`` and ``_``)."""
    if ":" in host:                                  # IPv6, without its brackets
        return _literal_address(host) is not None
    name = host[:-1] if host.endswith(".") else host
    return 0 < len(name) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in name.split("."))


def _endpoint(base_url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of the URL httpx will request, parsed by httpx itself, so
    the host checked is the host connected to; 400 for anything httpx would refuse
    (control characters), a host that is no IP literal or DNS name (a space, ``%``,
    an empty label) or a port out of range."""
    try:
        url = httpx.URL(completions_url(base_url))
        host = url.raw_host.decode("ascii")
    except (httpx.InvalidURL, ValueError, UnicodeError):
        raise LLMError(_INVALID_URL, status=400) from None
    port = url.port
    if not host or not _valid_host(host) or (port is not None and not 0 < port < 65536):
        raise LLMError(_INVALID_URL, status=400)
    return url.scheme, host, port or (443 if url.scheme == "https" else 80)


_LEGACY_IPV4_PART = re.compile(r"0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*")
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """The IPv4 address a numeric host means to the system resolver (inet_aton), which
    is what the connection would use: one to four parts, each decimal, octal (leading
    0) or hex (0x), the last filling the remaining bytes - ``127.1``, ``0x7f.1``,
    ``2130706433``. None for anything else."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(_LEGACY_IPV4_PART.fullmatch(p) for p in parts):
        return None
    values = [int(p, 16) if p[:2] in ("0x", "0X") else int(p, 8) if p.startswith("0") else int(p)
              for p in parts]
    *head, last = values
    if any(v > 255 for v in head) or last >= 256 ** (4 - len(head)):
        return None
    return ipaddress.IPv4Address(sum(v << (24 - 8 * i) for i, v in enumerate(head)) + last)


def _literal_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """``host`` as an IP address (an IPv6 zone id and a trailing dot dropped, inet_aton's
    short forms read as IPv4), or None for a name."""
    bare = host.split("%", 1)[0]
    if bare.endswith(".") and ":" not in bare:
        bare = bare[:-1]
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        return _legacy_ipv4(bare)


def _is_localhost(host: str) -> bool:
    """``localhost`` and every name under it are loopback by definition (RFC 6761)."""
    name = host.rstrip(".").lower()
    return name == "localhost" or name.endswith(".localhost")


def _routable(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not (ip.is_loopback or ip.is_private or ip.is_link_local
                                 or ip.is_multicast or ip.is_unspecified or ip.is_reserved)


def is_public_address(address: str) -> bool:
    """Whether a browser-supplied endpoint may reach ``address``: a globally routable
    unicast address. Loopback, private, link-local, multicast, unspecified, reserved and
    shared (100.64/10) addresses are not; an IPv4-mapped IPv6 address is judged as the
    IPv4 address it carries, and an address that carries one (6to4, Teredo, NAT64
    64:ff9b::/96) only passes when that IPv4 address passes too; anything that does not
    parse is not."""
    ip = _literal_address(address)
    if ip is None:
        return False
    candidates: list = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates = [ip.ipv4_mapped]
        else:
            if ip.sixtofour is not None:
                candidates.append(ip.sixtofour)
            if ip.teredo is not None:
                candidates.extend(ip.teredo)
            if ip in _NAT64:
                candidates.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return all(_routable(c) for c in candidates)


def _check_base_url(base_url: str, settings: WebSettings) -> None:
    """A browser-supplied endpoint must be an absolute https URL on a public address.

    The server would otherwise POST to whatever address a visitor names
    (metadata services, internal hosts). Plain http, and with it private and
    loopback addresses, are opt-in for local setups via ``LLM_ALLOW_HTTP=1``.
    A literal address is judged here; a name is resolved by ``resolve_llm``.
    """
    scheme, host, _ = _endpoint(base_url)
    if scheme == "https" or (scheme == "http" and settings.llm_allow_http):
        if not settings.llm_allow_http and (
                _is_localhost(host) or (_literal_address(host) is not None and not is_public_address(host))):
            raise LLMError(_REFUSED_ADDRESS, status=400)
        return
    raise LLMError(
        "X-LLM-Base-Url must be an absolute https:// URL "
        "(set LLM_ALLOW_HTTP=1 on the server to allow http://)",
        status=400,
    )


async def resolve_addresses(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to; the resolver runs off the event loop."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def resolve_llm(headers: Mapping[str, str], settings: WebSettings) -> LLMSettings:
    """``LLMSettings.resolve``, then the name in a browser-supplied endpoint resolved:
    every address it answers must be public (``is_public_address``), and a name that
    does not resolve is refused too (400). ``LLM_ALLOW_HTTP=1`` skips this, as it skips
    the literal-address check; the server's own ``LLM_BASE_URL`` is never checked.

    This does not defeat DNS rebinding: httpx resolves the name again when it
    connects, and a name that answers a public address now may answer a private one
    then. It stops the plain cases - a private address, ``localhost``, a name that
    points inside the network - which are what a visitor can type.
    """
    llm = LLMSettings.resolve(headers, settings)
    if not llm.custom_endpoint or settings.llm_allow_http:
        return llm
    _, host, port = _endpoint(llm.base_url)
    if _literal_address(host) is not None:
        return llm                                  # judged by _check_base_url already
    try:
        addresses = await resolve_addresses(host, port)
    except (OSError, UnicodeError):
        raise LLMError(f"X-LLM-Base-Url: cannot resolve host {host!r}", status=400) from None
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise LLMError(_REFUSED_ADDRESS, status=400)
    return llm


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


async def _before(deadline: float, awaitable):
    """``await awaitable``, but no later than ``deadline`` (event loop time)."""
    try:
        async with asyncio.timeout_at(deadline):
            return await awaitable
    except TimeoutError:
        raise LLMError(f"Model endpoint did not finish within {MODEL_CALL_DEADLINE:g}s",
                       status=502) from None


# -- token usage -----------------------------------------------------------------

@dataclass
class Usage:
    """The token counts of one model call, normalised across providers; ``None`` is a
    number the provider did not report (never read as 0).

    ``input`` is every prompt token of the call, cached ones included; ``cache_read`` is
    the part of it served from the provider's prompt cache and ``cache_write`` the part
    written to it. Which provider field feeds which number: ``parse_usage``."""
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None

    def as_dict(self) -> dict:
        return {"input": self.input, "output": self.output,
                "cache_read": self.cache_read, "cache_write": self.cache_write}

    def merged(self, later: Usage) -> Usage:
        """This usage updated by a later ``usage`` object of the same stream, field by field:
        a number in ``later`` replaces this one, a ``None`` there erases nothing. A provider
        that repeats a running total still ends with its last total."""
        return Usage(*(new if new is not None else old for old, new in zip(
            (self.input, self.output, self.cache_read, self.cache_write),
            (later.input, later.output, later.cache_read, later.cache_write))))


@dataclass
class CallStats:
    """What ``stream_completion`` learned about one call, filled in as the call goes.

    ``accepted``: the endpoint answered 2xx, so the call may have cost tokens even when
    the stream broke later. ``asked_usage``: the request that was answered carried
    ``stream_options.include_usage``. ``retried``: the first attempt was refused for that
    option and the call was sent again without it."""
    usage: Usage = field(default_factory=Usage)
    accepted: bool = False
    asked_usage: bool = False
    retried: bool = False


# The largest count taken as a number: beyond it a value is not a token count (and could
# not be summed safely in SQLite or represented exactly in the browser).
MAX_COUNT = 2 ** 53


def _count(value) -> int | None:
    """A token count as the JSON gave it: an integer from 0 to ``MAX_COUNT`` (``12.0`` is
    12), else None - negatives, fractions, booleans, text and absurd sizes are unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int) and 0 <= value <= MAX_COUNT:
        return value
    return None


def _first(*values: int | None) -> int | None:
    return next((v for v in values if v is not None), None)


def _sub(usage: dict, key: str) -> dict:
    value = usage.get(key)
    return value if isinstance(value, dict) else {}


def parse_usage(raw) -> Usage:
    """One provider ``usage`` object as ``Usage``; anything absent or not a count is None.

    - OpenAI-compatible (OpenAI, DeepSeek, Gemini's and Anthropic's compatibility
      endpoints, gateways such as OpenRouter or new-api): ``prompt_tokens`` is the whole
      prompt, cached tokens included (DeepSeek: hit + miss). Cache reads come from
      ``prompt_tokens_details.cached_tokens``, else DeepSeek's ``prompt_cache_hit_tokens``,
      else a relayed ``cache_read_input_tokens``; cache writes from
      ``prompt_tokens_details.cache_write_tokens``, else new-api's
      ``prompt_tokens_details.cached_creation_tokens``, else a relayed
      ``cache_creation_input_tokens``. Output is ``completion_tokens``.
    - Anthropic's own shape relayed as is (no ``prompt_tokens``; ``input_tokens`` next to
      ``cache_read_input_tokens`` / ``cache_creation_input_tokens``): ``input_tokens``
      counts only the uncached tail, so ``input`` is the sum of the three - unknown when
      either cache count is missing.
    - Responses-style (``input_tokens`` with ``input_tokens_details``): ``input_tokens`` is
      the whole prompt, reads and writes in its details. Output is ``output_tokens``.

    Output, every shape: ``completion_tokens`` (``output_tokens``), unless ``total_tokens``
    is a valid count larger than input + that - then ``total_tokens - input``. Gemini's
    compatibility endpoint leaves the thinking tokens out of ``completion_tokens`` and
    counts them only in ``total_tokens``; they are billed as output. Reasoning tokens a
    provider lists in ``completion_tokens_details.reasoning_tokens`` are never added on
    top: OpenAI's ``completion_tokens`` already includes them (and its total is input +
    completion, so the rule changes nothing there).
    """
    if not isinstance(raw, dict):
        return Usage()
    completion = _first(_count(raw.get("completion_tokens")), _count(raw.get("output_tokens")))
    prompt_details, input_details = _sub(raw, "prompt_tokens_details"), _sub(raw, "input_tokens_details")
    # The cache counts, whichever names this provider uses (a chunk may carry them alone).
    read = _first(_count(prompt_details.get("cached_tokens")), _count(raw.get("prompt_cache_hit_tokens")),
                  _count(raw.get("cache_read_input_tokens")), _count(input_details.get("cached_tokens")))
    write = _first(_count(prompt_details.get("cache_write_tokens")),
                   _count(prompt_details.get("cached_creation_tokens")),
                   _count(raw.get("cache_creation_input_tokens")), _count(input_details.get("cache_write_tokens")))
    prompt = _count(raw.get("prompt_tokens"))
    if prompt is not None:
        whole = prompt
    elif "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
        # Anthropic's own shape: input_tokens is only the tail after the cached prefix.
        tail = _count(raw.get("input_tokens"))
        anthropic_read = _count(raw.get("cache_read_input_tokens"))
        anthropic_write = _count(raw.get("cache_creation_input_tokens"))
        parts = (tail, anthropic_read, anthropic_write)
        whole = _count(sum(parts)) if None not in parts else None
        read, write = anthropic_read, anthropic_write
    else:
        whole = _count(raw.get("input_tokens"))
    usage = Usage(input=whole, output=completion, cache_read=read, cache_write=write)
    total = _count(raw.get("total_tokens"))
    if None not in (total, usage.input, completion) and total > usage.input + completion:
        usage.output = total - usage.input              # thinking counted only in the total
    return usage


# (completion URL, model) pairs that refused ``stream_options``: a 4xx naming it, and the
# same call accepted without it. Later calls of that pair go out without the option. In
# memory for the life of the process, bounded (oldest forgotten first). Keyed by model
# too: one gateway may put several providers behind one address.
USAGE_OPTION = {"include_usage": True}
_REFUSES_USAGE_OPTION: dict[tuple[str, str], None] = {}
_REFUSES_USAGE_OPTION_MAX = 256


def _refuses_usage_option(url: str, model: str) -> bool:
    return (url, model) in _REFUSES_USAGE_OPTION


def _remember_refusal(url: str, model: str) -> None:
    _REFUSES_USAGE_OPTION[(url, model)] = None
    while len(_REFUSES_USAGE_OPTION) > _REFUSES_USAGE_OPTION_MAX:
        del _REFUSES_USAGE_OPTION[next(iter(_REFUSES_USAGE_OPTION))]


def reset_usage_option_memory() -> None:
    _REFUSES_USAGE_OPTION.clear()


def _names_usage_option(status: int, body: str) -> bool:
    """A 4xx whose body names the option may be the endpoint not taking it - worth one
    retry without it. Never 401 or 403: those are about the key, whatever the body says."""
    text = body.lower()
    return 400 <= status < 500 and status not in (401, 403) and (
        "stream_options" in text or "include_usage" in text)


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
    stats: CallStats | None = None,
) -> AsyncIterator[str | list[ToolCall]]:
    """Stream one chat completion: content deltas as they arrive (``str``) and,
    when the model called tools, one ``list[ToolCall]`` at the end, assembled
    from the streamed fragments (each call's ``id`` and ``name`` arrive once,
    its ``arguments`` in pieces, all keyed by ``index``).

    ``stats``, when given, is filled in as the call goes: whether the endpoint
    accepted it, and the token usage the stream's ``usage`` objects carried
    (``parse_usage``, merged field by field; all ``None`` when there was none). The
    request asks for usage with ``stream_options.include_usage``; an endpoint that
    answers a 4xx naming that option (not 401 / 403) is asked once more without it
    (``stats.retried``), and only when that retry is accepted is the endpoint and
    model remembered and not asked again while the process runs.

    Raises ``LLMError`` when the endpoint answers with an error status; the
    message carries the status and a short body excerpt, never the request.
    """
    if not llm.configured:
        raise LLMError(
            "No model configured: fill in Model settings in the browser "
            "or set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY on the server.",
            status=400,
        )
    stats = stats if stats is not None else CallStats()
    url = completions_url(llm.base_url)
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
    ask_usage = not _refuses_usage_option(url, llm.model)
    calls: dict[int, dict] = {}          # index -> {id, name, arguments}
    client_timeout = httpx.Timeout(timeout, connect=15.0)
    # httpx's timeouts are per read: an endpoint that sends a byte now and then would
    # hold the call forever. Every wait below also stops at this deadline.
    deadline = asyncio.get_running_loop().time() + MODEL_CALL_DEADLINE
    try:
        # Redirects are never followed: a checked public endpoint must not be able to send
        # the request (and the key) on to an address nobody checked.
        async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
            while True:
                body_json = {**payload, "stream_options": USAGE_OPTION} if ask_usage else payload
                async with contextlib.AsyncExitStack() as stack:
                    resp = await _before(deadline, stack.enter_async_context(
                        client.stream("POST", url, headers=headers, json=body_json)))
                    if 300 <= resp.status_code < 400:
                        raise LLMError(f"Model endpoint returned HTTP {resp.status_code}: redirects are not followed",
                                       status=502)
                    if resp.status_code >= 400:
                        body = (await _before(deadline, resp.aread())).decode("utf-8", "replace")
                        if ask_usage and _names_usage_option(resp.status_code, body):
                            # Perhaps the provider does not take the option: once more without
                            # it. Remembered only if that attempt is accepted (below).
                            _log.info("model endpoint answered HTTP %d naming stream_options (%s): "
                                      "asking once more without it", resp.status_code, llm.model)
                            ask_usage = False
                            stats.retried = True
                            continue
                        raise LLMError(f"Model endpoint returned HTTP {resp.status_code}: {body[:300]}",
                                       status=502)
                    stats.accepted = True
                    stats.asked_usage = ask_usage
                    if stats.retried:
                        _remember_refusal(url, llm.model)
                        _log.info("model endpoint takes the call without stream_options (%s): not asking "
                                  "this endpoint and model for usage again; read only if it sends it anyway",
                                  llm.model)
                    lines = resp.aiter_lines()
                    while True:
                        try:
                            line = await _before(deadline, anext(lines))
                        except StopAsyncIteration:
                            break
                        if not line.startswith("data: "):
                            continue
                        data = line[6:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        if isinstance(chunk.get("usage"), dict):
                            # OpenAI sends it once, in a chunk with no choices; some providers
                            # repeat a running total, some split it over chunks. A later number
                            # replaces an earlier one; a missing one erases nothing.
                            stats.usage = stats.usage.merged(parse_usage(chunk["usage"]))
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        token = delta.get("content")
                        if token:
                            yield token
                        for fragment in delta.get("tool_calls") or []:
                            _merge_tool_call(calls, fragment)
                break
    except httpx.InvalidURL:
        # Not an HTTPError. A browser URL was checked at the gate (_endpoint); this is
        # the second line, and the only one for the server's own LLM_BASE_URL.
        raise LLMError("Model endpoint URL is not a valid URL", status=400) from None
    except httpx.HTTPError as exc:
        # httpx error text names the URL, not the headers.
        _log.warning("model request failed (%s): %s", llm.model, type(exc).__name__)
        raise LLMError(f"Model endpoint unreachable: {type(exc).__name__}", status=502) from None
    assembled = [ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=c["arguments"])
                 for i, c in sorted(calls.items()) if c["name"]]
    if assembled:
        yield assembled


# -- SSE frames --------------------------------------------------------------------

def format_sse(data: str) -> str:
    """One SSE frame carrying a JSON-encoded string token."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def format_sse_event(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SSE_DONE = "event: done\ndata: [DONE]\n\n"
