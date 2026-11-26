"""Lightweight selection reads and device-scoped origin tracking."""
import asyncio
from time import monotonic

from pydantic import BaseModel, Field, ValidationError
from revit_bridge.revit.capabilities import METHOD_NOT_FOUND
from revit_bridge.snapshot.selection import STATE_METHOD

from backend.api.errors import ApiError


SELECTION_WRITERS = {"select_elements", "select_in_box", "select_by_filter", "clear_selection"}


class SelectionState(BaseModel):
    document_id: str = Field(min_length=1, max_length=200)
    stamp: str = Field(min_length=1, max_length=200)
    count: int = Field(strict=True, ge=0)

    def matches(self, other):
        return self.document_id == other.document_id and self.stamp == other.stamp


class SelectionReference(SelectionState):
    categories: dict[str, int]
    categories_partial: bool = False


def uses_current_selection(spec, pack):
    if not pack:
        return False
    params = spec.execution_projection()["params"]
    return pack.name == "delete_selected" or any(
        p["name"] in ("element_id", "element_ids") and p.get("default") == ""
        and params.get(p["name"]) in (None, "") for p in pack.parameters)


async def read(client) -> SelectionState | None:
    try:
        reply = await asyncio.wait_for(client.send_command(STATE_METHOD, {}), 3)
        if not reply.success and reply.error_code == METHOD_NOT_FOUND:
            return None
        if reply.success:
            return SelectionState.model_validate(reply.result)
    except (OSError, RuntimeError, ValidationError):
        pass
    raise ApiError(503, "selection_unavailable")


class DeviceSelection:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_read = 0.0
        self.cached = None
        self.poll_interval = 2
        self.streams = 0
        self.writes = 0
        self.generation = 0
        self.assistant = False
        self.after_write = None

    async def mark_write(self, client):
        self.generation += 1
        generation = self.generation
        self.assistant = True
        self.after_write = None
        self.last_read = 0.0
        try:
            state = await read(client)
            if self.generation == generation:
                self.after_write = state
        except ApiError:
            pass  # Without a stamp, require an observed designer change.

    def designer(self, state):
        if self.writes:
            return False
        if self.assistant and state is not None:
            if self.after_write is None:
                self.after_write = state
            elif self.after_write != state:
                self.assistant = False
        return not self.assistant

    async def verify(self, client, expected):
        state = await read(client)
        if state is None:
            raise ApiError(503, "selection_unavailable")
        if not state.matches(expected):
            self.last_read = 0.0  # The next composer poll must read the new selection.
            raise ApiError(409, "selection_changed")
        return state

    async def poll(self, client, busy):
        async with self.lock:
            if busy():
                raise ApiError(409, "selection_busy")
            if monotonic() - self.last_read >= self.poll_interval:
                generation = self.generation
                try:
                    state = await read(client)
                except ApiError:
                    self.last_read = monotonic()
                    self.poll_interval = min(self.poll_interval * 2, 30)
                    raise
                if generation != self.generation or busy():
                    raise ApiError(409, "selection_busy")
                self.cached = state
                self.poll_interval = 2
                self.last_read = monotonic()
            elif self.poll_interval > 2:
                raise ApiError(503, "selection_unavailable")
            if self.cached is None:
                return {"error": "unsupported_addin"}
            return {**self.cached.model_dump(), "assistant_set": not self.designer(self.cached)}


class WatchClient:
    """Pass the designer-facing prompt on the package's single focus request."""
    def __init__(self, client, prompt):
        self.client, self.prompt = client, prompt

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def send_command(self, method, params, **kwargs):
        if method == STATE_METHOD and params.get("focus"):
            params = {**params, "prompt": self.prompt}
        return await self.client.send_command(method, params, **kwargs)
