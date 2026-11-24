"""A scripted OpenAI-compatible model behind ``httpx.MockTransport``.

``FakeModel(turns)`` answers each ``/chat/completions`` request with the next
turn of the script, streamed as SSE chunks the way OpenAI does it: content
in pieces, and ``tool_calls`` as fragments - the call's ``id`` and ``name``
once, its ``arguments`` split over several chunks, all keyed by ``index``.
Every request the host sends is kept in ``requests`` (messages, tools) so a
test can check what the model was shown.

Token usage: a turn may carry ``usage`` (a provider's raw usage object, e.g.
one of ``USAGE_SHAPES``). How it travels is the model's ``usage_delivery``:
``"final_chunk"`` (OpenAI: one more chunk with ``choices: []`` before
``[DONE]``, every other chunk ``usage: null``), ``"finish_chunk"`` (on the
chunk that carries ``finish_reason``, as DeepSeek documents it) or
``"every_chunk"`` (a running total on each chunk, the last one complete).
``usage_needs_option`` sends it only when the request asked with
``stream_options.include_usage``; ``refuse_usage_option`` answers such a
request 400 naming the option, as a provider that does not take it would.
"""
from __future__ import annotations

import json

import httpx


def text_turn(text: str, *, usage: dict | None = None) -> dict:
    turn: dict = {"content": text}
    if usage is not None:
        turn["usage"] = usage
    return turn


def tool_turn(*calls: tuple[str, dict], content: str = "", usage: dict | None = None) -> dict:
    """A turn that calls tools: ``tool_turn(("query", {"kind": "levels"}), ...)``."""
    turn: dict = {"content": content, "tool_calls": [{"name": name, "arguments": args} for name, args in calls]}
    if usage is not None:
        turn["usage"] = usage
    return turn


# Provider usage objects as their documentation describes them (all values made up), and
# what backend.llm.parse_usage must read from each: (input, output, cache_read, cache_write).
USAGE_SHAPES: dict[str, tuple[dict, tuple]] = {
    # OpenAI chat completions: CompletionUsage, prompt_tokens includes cached_tokens.
    "openai": ({"prompt_tokens": 2006, "completion_tokens": 300, "total_tokens": 2306,
                "prompt_tokens_details": {"cached_tokens": 1920, "audio_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0,
                                              "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0}},
               (2006, 300, 1920, None)),
    # OpenAI with the cache write count the current reference lists.
    "openai_cache_write": ({"prompt_tokens": 12000, "completion_tokens": 80, "total_tokens": 12080,
                            "prompt_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000}},
                           (12000, 80, 4000, 7000)),
    # DeepSeek: prompt_tokens = prompt_cache_hit_tokens + prompt_cache_miss_tokens.
    "deepseek": ({"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                  "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200,
                  "prompt_tokens_details": {"cached_tokens": 800},
                  "completion_tokens_details": {"reasoning_tokens": 0}},
                 (1000, 50, 800, None)),
    # DeepSeek without prompt_tokens_details: the hit count alone.
    "deepseek_hit_only": ({"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050,
                           "prompt_cache_hit_tokens": 640, "prompt_cache_miss_tokens": 360},
                          (1000, 50, 640, None)),
    # Gemini's OpenAI compatibility endpoint: no cache fields are documented (seen without
    # them); completion_tokens leaves the thinking out, total_tokens counts it (1725 - 758 -
    # 102 = 865 thinking tokens, billed as output): output is total - prompt.
    "gemini": ({"completion_tokens": 102, "prompt_tokens": 758, "total_tokens": 1725}, (758, 967, None, None)),
    # ... the same without total_tokens: the thinking cannot be known, output is completion.
    "gemini_no_total": ({"completion_tokens": 102, "prompt_tokens": 758}, (758, 102, None, None)),
    # ... a Gemini that also lists the thinking: still total - prompt, nothing added twice.
    "gemini_reasoning_listed": ({"completion_tokens": 102, "prompt_tokens": 758, "total_tokens": 1725,
                                 "completion_tokens_details": {"reasoning_tokens": 865}},
                                (758, 967, None, None)),
    # OpenAI with reasoning: completion_tokens already includes reasoning_tokens and the total
    # is prompt + completion - output stays completion_tokens (not 300 + 256).
    "openai_reasoning": ({"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
                          "prompt_tokens_details": {"cached_tokens": 1024},
                          "completion_tokens_details": {"reasoning_tokens": 256}},
                         (1200, 300, 1024, None)),
    # A total that is not a count is ignored.
    "bad_total": ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": "999"}, (10, 5, None, None)),
    # Anthropic's OpenAI compatibility endpoint: prompt_tokens_details "always empty".
    "anthropic_compat": ({"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520,
                          "prompt_tokens_details": None, "completion_tokens_details": None},
                         (500, 20, None, None)),
    # OpenRouter: reads and writes in prompt_tokens_details.
    "openrouter": ({"prompt_tokens": 194, "completion_tokens": 2, "total_tokens": 196, "cost": 0.95,
                    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 100, "audio_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0}},
                   (194, 2, 0, 100)),
    # new-api relaying Claude: prompt_tokens = input + cache read + cache creation; the
    # write count both as cache_write_tokens and the older cached_creation_tokens; its
    # Responses fields come along as zeros.
    "newapi_claude": ({"prompt_tokens": 5300, "completion_tokens": 40, "total_tokens": 5340,
                       "prompt_tokens_details": {"cached_tokens": 3000, "cached_creation_tokens": 2000,
                                                 "cache_write_tokens": 2000, "text_tokens": 0,
                                                 "audio_tokens": 0, "image_tokens": 0},
                       "completion_tokens_details": {"text_tokens": 0, "audio_tokens": 0, "reasoning_tokens": 0},
                       "input_tokens": 0, "output_tokens": 0, "input_tokens_details": None,
                       "claude_cache_creation_5_m_tokens": 2000, "claude_cache_creation_1_h_tokens": 0},
                      (5300, 40, 3000, 2000)),
    # An older new-api: only cached_creation_tokens for the write.
    "newapi_legacy_write": ({"prompt_tokens": 2300, "completion_tokens": 10, "total_tokens": 2310,
                             "prompt_tokens_details": {"cached_tokens": 0, "cached_creation_tokens": 2000}},
                            (2300, 10, 0, 2000)),
    # LiteLLM: the Anthropic counts as top-level fields next to the OpenAI ones.
    "litellm_anthropic": ({"prompt_tokens": 2006, "completion_tokens": 300, "total_tokens": 2306,
                           "prompt_tokens_details": {"cached_tokens": 1920},
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1920},
                          (2006, 300, 1920, 0)),
    # Anthropic's Messages usage relayed as is: input_tokens is only the uncached tail.
    "anthropic_native": ({"input_tokens": 50, "cache_read_input_tokens": 1000,
                          "cache_creation_input_tokens": 200, "output_tokens": 30},
                         (1250, 30, 1000, 200)),
    # ... with one cache count missing: the total is unknown, not input_tokens.
    "anthropic_native_partial": ({"input_tokens": 50, "cache_read_input_tokens": 1000, "output_tokens": 30},
                                 (None, 30, 1000, None)),
    # Responses-style usage: input_tokens is the whole prompt.
    "responses": ({"input_tokens": 12000, "output_tokens": 100,
                   "input_tokens_details": {"cached_tokens": 4000, "cache_write_tokens": 7000}},
                  (12000, 100, 4000, 7000)),
    # Nothing usable: strings, negatives, booleans and floats with a fraction are not counts.
    "garbage": ({"prompt_tokens": "12", "completion_tokens": -1, "total_tokens": True,
                 "prompt_tokens_details": {"cached_tokens": 1.5}},
                (None, None, None, None)),
    # Counts beyond 2**53 are not token counts: unknown, never an overflow later.
    "huge": ({"prompt_tokens": 2 ** 62, "completion_tokens": 2 ** 53 + 1, "total_tokens": 2 ** 63,
              "prompt_tokens_details": {"cached_tokens": 1e300}},
             (None, None, None, None)),
    "edge": ({"prompt_tokens": 2 ** 53, "completion_tokens": 0}, (2 ** 53, 0, None, None)),
    "empty": ({}, (None, None, None, None)),
}


class FakeModel:
    def __init__(self, turns: list[dict], *, pieces: int = 3, usage_delivery: str = "final_chunk",
                 usage_needs_option: bool = True, refuse_usage_option: bool = False):
        self.turns = list(turns)
        self.pieces = max(1, pieces)
        self.usage_delivery = usage_delivery
        self.usage_needs_option = usage_needs_option
        self.refuse_usage_option = refuse_usage_option
        self.requests: list[dict] = []
        self.refused: list[dict] = []             # requests answered 400 for stream_options
        self.transport = httpx.MockTransport(self._handle)

    # -- httpx side --------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if self.refuse_usage_option and "stream_options" in body:
            self.refused.append(body)
            return httpx.Response(400, text='{"error": {"message": "Unrecognized request argument supplied: '
                                            'stream_options", "type": "invalid_request_error"}}')
        self.requests.append(body)
        if not self.turns:
            return httpx.Response(500, text='{"error": "script exhausted"}')
        turn = self.turns.pop(0)
        asked = bool((body.get("stream_options") or {}).get("include_usage"))
        return httpx.Response(200, text=self._stream(turn, asked), headers={"content-type": "text/event-stream"})

    def _stream(self, turn: dict, asked: bool = False) -> str:
        chunks: list[dict] = [{"role": "assistant"}]
        content = turn.get("content") or ""
        for piece in _split(content, self.pieces):
            chunks.append({"content": piece})
        for index, call in enumerate(turn.get("tool_calls") or []):
            # {"raw": "..."} sends the argument text as is (a model that emits bad JSON)
            arguments = call["raw"] if "raw" in call else json.dumps(call["arguments"], ensure_ascii=False)
            fragments = _split(arguments, self.pieces)
            chunks.append({"tool_calls": [{"index": index, "id": f"call_{index}", "type": "function",
                                           "function": {"name": call["name"], "arguments": fragments[0]}}]})
            for fragment in fragments[1:]:
                chunks.append({"tool_calls": [{"index": index, "function": {"arguments": fragment}}]})
        finish = "tool_calls" if turn.get("tool_calls") else "stop"
        events = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in chunks]
        events.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        usage = turn.get("usage")
        if usage is not None and (asked or not self.usage_needs_option):
            if self.usage_delivery == "final_chunk":
                for event in events:
                    event["usage"] = None
                events.append({"choices": [], "usage": usage})
            elif self.usage_delivery == "finish_chunk":
                events[-1]["usage"] = usage
            elif self.usage_delivery == "every_chunk":
                for event in events[:-1]:
                    event["usage"] = {**usage, "completion_tokens": 0}
                events[-1]["usage"] = usage
            else:
                raise ValueError(f"unknown usage_delivery {self.usage_delivery!r}")
        lines = [f"data: {json.dumps(event)}" for event in events]
        lines.append("data: [DONE]")
        return "\n\n".join(lines) + "\n\n"

    # -- what the host sent ---------------------------------------------------------

    def last_messages(self) -> list[dict]:
        return self.requests[-1]["messages"]

    def tool_results(self, name: str) -> list:
        """Every tool result for calls of ``name`` the model was shown, in order."""
        results = []
        for body in self.requests:
            messages = body["messages"]
            for i, message in enumerate(messages):
                if message.get("role") != "assistant":
                    continue
                for call in message.get("tool_calls") or []:
                    if call["function"]["name"] != name:
                        continue
                    for later in messages[i + 1:]:
                        if later.get("role") == "tool" and later.get("tool_call_id") == call["id"]:
                            results.append(json.loads(later["content"]))
                            break
        # the same result appears in every later request; keep first occurrences only
        unique: list = []
        for result in results:
            if result not in unique:
                unique.append(result)
        return unique


ROLE_OPENINGS = {"You are the analyst": "analyst", "You are the planner": "planner",
                 "You are the supervisor": "supervisor"}


def role_of(body: dict) -> str:
    """The role a request was made for, read off its system prompt (the package's role texts
    open with "You are the <role>"); "single" for the one loop's prompt (or none)."""
    messages = body.get("messages") or []
    system = messages[0].get("content", "") if messages and messages[0].get("role") == "system" else ""
    return next((role for opening, role in ROLE_OPENINGS.items() if system.startswith(opening)), "single")


class RoleScriptedModel(FakeModel):
    """A ``FakeModel`` that answers each role from its own script: ``scripts`` maps
    ``analyst`` / ``planner`` / ``supervisor`` (and ``single``, the one loop) to turns; a
    request takes the next turn of its role's script. ``roles`` lists the role of every
    request, in the order they came."""

    def __init__(self, scripts: dict[str, list[dict]] | None = None, **options):
        super().__init__([], **options)
        self.scripts: dict[str, list[dict]] = {role: list(turns) for role, turns in (scripts or {}).items()}
        self.roles: list[str] = []

    def script(self, role: str, *turns: dict) -> None:
        self.scripts.setdefault(role, []).extend(turns)

    def left(self) -> dict[str, int]:
        return {role: len(turns) for role, turns in self.scripts.items() if turns}

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if self.refuse_usage_option and "stream_options" in body:
            self.refused.append(body)
            return httpx.Response(400, text='{"error": {"message": "stream_options not supported"}}')
        role = role_of(body)
        self.requests.append(body)
        self.roles.append(role)
        turns = self.scripts.get(role) or []
        if not turns:
            return httpx.Response(500, text=f'{{"error": "script of {role} exhausted"}}')
        turn = turns.pop(0)
        asked = bool((body.get("stream_options") or {}).get("include_usage"))
        return httpx.Response(200, text=self._stream(turn, asked), headers={"content-type": "text/event-stream"})

    def requests_of(self, role: str) -> list[dict]:
        return [body for body, r in zip(self.requests, self.roles) if r == role]


def json_turn(value: dict, *, usage: dict | None = None, wrap: str = "{}") -> dict:
    """A role's JSON reply as text (``wrap``: ``"{}"`` as is, or ``"fence"`` in a Markdown fence)."""
    text = json.dumps(value, ensure_ascii=False)
    if wrap == "fence":
        text = f"```json\n{text}\n```"
    return text_turn(text, usage=usage)


TRACE_EVENTS = ("stage", "tool", "usage")


def drop_trace_frames(text: str) -> str:
    """An SSE response without its ``stage``, ``tool`` and ``usage`` frames: what the host
    streamed before those events existed."""
    blocks = [block for block in text.split("\n\n") if block]
    kept = [block for block in blocks
            if not any(block.startswith(f"event: {name}\n") for name in TRACE_EVENTS)]
    return "".join(block + "\n\n" for block in kept)


def _split(text: str, pieces: int) -> list[str]:
    if not text:
        return [""]
    size = max(1, -(-len(text) // pieces))
    return [text[i:i + size] for i in range(0, len(text), size)]
