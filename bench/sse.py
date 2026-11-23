"""SSE framing, independent of network chunk boundaries."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator


def parse_frame(lines: list[str]) -> dict | None:
    event = ""
    data = []
    for line in lines:
        field, separator, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data" and separator:
            data.append(value)
    raw = "\n".join(data)
    if event == "done" or raw.strip() == "[DONE]":
        return {"type": "done"}
    if event not in ("", "spec", "stage", "tool", "usage", "error") or not data:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Invalid JSON in SSE " + (event or "text") + " event") from exc
    if event == "" and isinstance(value, str):
        return {"type": "text", "text": value}
    if event and isinstance(value, dict):
        return {**value, "type": event}
    raise ValueError("Invalid payload in SSE " + (event or "text") + " event")


def iter_events(lines: Iterable[str]) -> Iterator[dict]:
    frame: list[str] = []
    for line in lines:
        line = line.rstrip("\r\n")
        if line:
            frame.append(line)
            continue
        event = parse_frame(frame)
        frame = []
        if event is not None:
            yield event
            if event["type"] == "done":
                return
    if frame:
        event = parse_frame(frame)
        if event is not None:
            yield event
