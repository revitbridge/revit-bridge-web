"""The frames of the view captures a designer may draw on, kept on the host.

A sketch is converted to model coordinates with the frame the package read when the view was
captured. The page gets the picture and an id; the frame never leaves the host and is never
taken from a request, so what the page sends can only ever be pixels on a capture the host
made itself, for the same device. Bounded and short-lived: a capture nobody draws on is
forgotten.
"""
from __future__ import annotations

import base64
import binascii
import secrets
import threading
import time
from dataclasses import dataclass, field

MAX_CAPTURES = 64                          # on this host
MAX_CAPTURES_PER_SCOPE = 8                 # of one device: it cannot push the others' captures out
TTL_SECONDS = 30 * 60
# The capture with the designer's lines drawn on it. As base64 inside the chat request it must
# stay under the 2 MB body limit a reverse proxy in front of a cloud host commonly has.
MAX_IMAGE_BYTES = 1_300_000
# A stroke as the page sends it: the page thins a stroke to 600 points. Simplifying a stroke
# costs time that grows with the square of its points, on the event loop, so the request
# model refuses more before anything is computed.
MAX_STROKE_POINTS = 600
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass
class SketchCapture:
    id: str
    scope: str
    frame: dict = field(repr=False)
    width: int
    height: int
    view_name: str
    created: float


class SketchStore:
    def __init__(self):
        self._items: dict[str, SketchCapture] = {}
        self._lock = threading.Lock()

    def add(self, scope: str, reply: dict) -> SketchCapture:
        item = SketchCapture(secrets.token_hex(16), scope, reply["frame"], reply["width"], reply["height"],
                             reply["view_name"], time.monotonic())
        with self._lock:
            self._expire()
            mine = sorted((key for key, kept in self._items.items() if kept.scope == scope),
                          key=lambda key: self._items[key].created)
            for key in mine[:max(0, len(mine) - MAX_CAPTURES_PER_SCOPE + 1)]:
                del self._items[key]
            while len(self._items) >= MAX_CAPTURES:
                del self._items[min(self._items, key=lambda key: self._items[key].created)]
            self._items[item.id] = item
        return item

    def get(self, scope: str, capture_id: str) -> SketchCapture | None:
        with self._lock:
            self._expire()
            item = self._items.get(capture_id)
        return item if item and item.scope == scope else None

    def _expire(self):
        limit = time.monotonic() - TTL_SECONDS
        for key in [key for key, item in self._items.items() if item.created < limit]:
            del self._items[key]


_store = SketchStore()


def get_sketch_store() -> SketchStore:
    return _store


def reset_sketch_store() -> None:
    global _store
    _store = SketchStore()


def checked_png(encoded: str, width: int, height: int) -> str:
    """``encoded`` when it is a PNG of exactly this size within the byte limit; ValueError
    otherwise. Only the header is read: the pixels go to a model, never to this host."""
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("The sketch image is too large.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("The sketch image is not base64.") from None
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR" or len(data) < 33:
        raise ValueError("The sketch image is not a PNG.")
    if int.from_bytes(data[16:20], "big") != width or int.from_bytes(data[20:24], "big") != height:
        raise ValueError("The sketch image is not the size of the capture.")
    return encoded
