"""A scripted OpenAI-compatible model behind ``httpx.MockTransport``.

``FakeModel(turns)`` answers each ``/chat/completions`` request with the next
turn of the script, streamed as SSE chunks the way OpenAI does it: content
in pieces, and ``tool_calls`` as fragments - the call's ``id`` and ``name``
once, its ``arguments`` split over several chunks, all keyed by ``index``.
Every request the host sends is kept in ``requests`` (messages, tools) so a
test can check what the model was shown.
"""
from __future__ import annotations

import json

import httpx


def text_turn(text: str) -> dict:
    return {"content": text}


def tool_turn(*calls: tuple[str, dict], content: str = "") -> dict:
    """A turn that calls tools: ``tool_turn(("query", {"kind": "levels"}), ...)``."""
    return {"content": content, "tool_calls": [{"name": name, "arguments": args} for name, args in calls]}


class FakeModel:
    def __init__(self, turns: list[dict], *, pieces: int = 3):
        self.turns = list(turns)
        self.pieces = max(1, pieces)
        self.requests: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    # -- httpx side --------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        if not self.turns:
            return httpx.Response(500, text='{"error": "script exhausted"}')
        turn = self.turns.pop(0)
        return httpx.Response(200, text=self._stream(turn), headers={"content-type": "text/event-stream"})

    def _stream(self, turn: dict) -> str:
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
        lines = [f"data: {json.dumps({'choices': [{'delta': delta, 'finish_reason': None}]})}" for delta in chunks]
        lines.append(f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': finish}]})}")
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


def _split(text: str, pieces: int) -> list[str]:
    if not text:
        return [""]
    size = max(1, -(-len(text) // pieces))
    return [text[i:i + size] for i in range(0, len(text), size)]
