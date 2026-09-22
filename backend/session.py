"""In-memory chat sessions: the model's message history plus what the host loop
knows about the task, per session id, two-hour idle TTL.

A session holds the messages the model saw (user, assistant with its
``tool_calls``, tool results), the ``snapshot_fingerprint`` of the last
snapshot the model took, the last ``spec`` it proposed and the most recent
``evidence_id`` the page reported back. It never holds a confirmation
token: the token lives in the browser's memory and the gate's ``pending/``
directory is the server-side record. Model settings travel with every
request as headers (see ``backend.llm``), so nothing secret is held here.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

TTL_SECONDS = 2 * 60 * 60
MAX_HISTORY = 60


@dataclass
class Session:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # OpenAI-shaped messages: {"role": "user"|"assistant"|"tool", "content": ..., ...}
    history: list[dict] = field(default_factory=list)
    snapshot_fingerprint: str | None = None
    spec: dict | None = None
    evidence_id: str | None = None

    def touch(self) -> None:
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > TTL_SECONDS

    def add(self, message: dict) -> None:
        self.history.append(message)
        if len(self.history) > MAX_HISTORY:
            self.history = trim_at_user_turn(self.history[-MAX_HISTORY:])
        self.touch()

    def add_message(self, role: str, content: str) -> None:
        self.add({"role": role, "content": content})

    def window(self, limit: int) -> list[dict]:
        """The last ``limit`` messages, cut at a user turn so no tool result is
        sent without the assistant call it answers."""
        return trim_at_user_turn(self.history[-limit:]) if limit > 0 else []


def trim_at_user_turn(messages: list[dict]) -> list[dict]:
    """Drop leading messages until the first ``user`` turn (an empty list if none)."""
    for i, message in enumerate(messages):
        if message.get("role") == "user":
            return messages[i:]
    return []


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_or_create(self, session_id: str | None = None) -> Session:
        with self._lock:
            self._cleanup()
            if session_id and session_id in self._sessions:
                session = self._sessions[session_id]
                session.touch()
                return session
            # Unknown or missing id: always mint a server-side id (no fixation).
            session = Session(session_id=uuid.uuid4().hex)
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session and not session.is_expired():
                session.touch()
                return session
            return None

    def _cleanup(self) -> None:
        for key in [k for k, v in self._sessions.items() if v.is_expired()]:
            del self._sessions[key]


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store
