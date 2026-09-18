"""In-memory chat sessions: history per session id, two-hour idle TTL.

Model settings are not part of a session any more; they travel with every
request as headers (see ``backend.llm``), so nothing secret is held here.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

TTL_SECONDS = 2 * 60 * 60
MAX_HISTORY = 40


@dataclass
class Session:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # [{"role": "user" | "assistant", "content": str}, ...]
    history: list[dict] = field(default_factory=list)

    def touch(self) -> None:
        self.last_active = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.last_active) > TTL_SECONDS

    def add_message(self, role: str, content: str) -> None:
        self.history.append({"role": role, "content": content})
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        self.touch()


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
