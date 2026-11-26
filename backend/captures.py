"""Bounded, ephemeral view context owned by a single chat session."""
from collections import deque
from dataclasses import dataclass, field
import secrets


@dataclass
class Capture:
    id: str
    scope: str
    summary: str
    png: str | None = field(repr=False)
    hidden: bool = False
    anchor: dict | None = field(default=None, repr=False)


class CaptureContext:
    def __init__(self):
        self.entries: deque[Capture] = deque(maxlen=32)

    def add(self, scope: str, image: dict) -> str:
        # The package has already validated dimensions, PNG signature and byte length.
        # Never copy encoded pixels from an untrusted view name into text.
        png = image["png_base64"]
        name = " ".join(image["view_name"].split())[:200]
        if any(name[i:i + 16] in png for i in range(len(name) - 15)):
            name = "Revit"
        item = Capture(secrets.token_hex(16), scope, f"{name} ({image['width']} x {image['height']})", png)
        self.entries.append(item)
        for previous in list(self.entries)[:-2]:
            previous.png = None
        return item.id

    def for_scope(self, scope: str):
        return [item for item in self.entries if item.scope == scope]

    def clear_scope(self, scope: str):
        self.entries = deque((item for item in self.entries if item.scope != scope), maxlen=self.entries.maxlen)
