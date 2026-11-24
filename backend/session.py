"""In-memory chat sessions: the model's message history plus what the host loop
knows about the task, per session id, two-hour idle TTL and at most
``MAX_SESSIONS`` at once (a new one pushes out the one idle the longest).

A session holds the messages the model saw (user, assistant with its
``tool_calls``, tool results), the ``snapshot_fingerprint`` of the last
snapshot the model took, the last ``spec`` it proposed and the most recent
``evidence_id`` the page reported back. It also keeps the system prompt its
first turn was sent with (per ``bridge`` value), so every later call of the
session starts with the same bytes and a provider's prefix cache keeps
matching, and counts its turns (``llm_calls`` and the trace events number
them), and keeps the designer's picks the package verified in any of its turns,
per device scope (``picks_for``: a spec may bind only those, see
``backend.mention_check``). It never holds a confirmation
token: the token lives in the browser's memory and the gate's ``pending/``
directory is the server-side record. Model settings travel with every
request as headers (see ``backend.llm``), so nothing secret is held here.
What a provider returned with a tool call that must go back with it (a
thought signature, encrypted reasoning) is kept per session in memory only
(``provider_memory``), never written anywhere.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from backend.llm import ProviderMemory
from backend.mention_check import mention_evidence

if TYPE_CHECKING:                     # backend.roles imports the model's tools, which import this
    from backend.roles import RoleTask

TTL_SECONDS = 2 * 60 * 60
MAX_HISTORY = 60
# Live sessions held at once. A session keeps at most MAX_HISTORY messages (tool results
# capped at 8-16 KB, execution reports at 16 KB), so this bounds the memory sessions
# take even when a page is reloaded in a loop; a demo host has a handful at a time.
MAX_SESSIONS = 300
# Verified picks a session keeps (at most 20 come with one message).
MAX_SESSION_PICKS = 200


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
    # bridge (True / False) -> the system prompt of this session's first turn in that mode
    system_prompts: dict[bool, str] = field(default_factory=dict)
    turns: int = 0
    # The designer's picks the package verified in this session (resolve_mentions items), by
    # (device scope, the evidence a spec gives for them, "mention:<kind>#<id>"); the latest
    # reading of each. An id means something only in the model it was read from.
    picks: dict[tuple[str, str], dict] = field(default_factory=dict)
    # The roles (backend.roles): each role's system prompt of the session's first turn that
    # used it; the task the next message may continue (tool, how often it was sent back,
    # the last message the designer got); the analyst's digest of the open model per device
    # scope (dropped when the page reports an execution) and, per scope, when the last read
    # failed and what the analyst was told; the supervisor's last verdict until the planner
    # has seen it; the spec the page confirmed (/spec/confirm with this session) until its
    # execution is reported - the supervisor reads it instead of the last proposed one.
    role_prompts: dict[str, str] = field(default_factory=dict)
    role_task: RoleTask | None = None
    digests: dict[str, str] = field(default_factory=dict)
    digest_failures: dict[str, tuple[float, str]] = field(default_factory=dict)
    pending_verdict: dict | None = None
    confirmed_spec: dict | None = None
    # What a provider returned with a tool call that goes back with it in the next requests
    # (Gemini's thought signature, the responses api's reasoning items): by call, for the
    # endpoint and model that returned it, in memory only (backend.llm.ProviderMemory).
    provider_memory: ProviderMemory = field(default_factory=ProviderMemory, repr=False)

    def role_prompt(self, role: str, build) -> str:
        """The session's system prompt of ``role``: built by ``build(role)`` on first use, then
        the same text for the rest of the session."""
        if role not in self.role_prompts:
            self.role_prompts[role] = build(role)
        return self.role_prompts[role]

    def remember_picks(self, scope: str, items: list[dict]) -> None:
        """Keep verified picks of the device ``scope`` for this turn and every later one; a pick
        verified again takes its new reading (a renamed level), and past ``MAX_SESSION_PICKS``
        the oldest goes."""
        for item in items:
            key = (scope, mention_evidence(item.get("kind"), item.get("id")))
            self.picks.pop(key, None)
            self.picks[key] = item
        while len(self.picks) > MAX_SESSION_PICKS:
            del self.picks[next(iter(self.picks))]

    def forget_picks(self, scope: str, entries: list[dict]) -> None:
        """Drop the picks of ``scope`` a later check found gone (``{kind, id}`` entries)."""
        for entry in entries:
            self.picks.pop((scope, mention_evidence(entry.get("kind"), entry.get("id"))), None)

    def picks_for(self, scope: str) -> dict[str, dict]:
        """The verified picks of the device ``scope``, by evidence string."""
        return {evidence: item for (owner, evidence), item in self.picks.items() if owner == scope}

    def system_prompt(self, bridge: bool, build) -> str:
        """The session's system prompt for ``bridge``: built by ``build(bridge)`` on first
        use, then the same text for the rest of the session (a skill edited meanwhile
        reaches the next session)."""
        if bridge not in self.system_prompts:
            self.system_prompts[bridge] = build(bridge)
        return self.system_prompts[bridge]

    def next_turn(self) -> int:
        self.turns += 1
        return self.turns

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
            while len(self._sessions) >= MAX_SESSIONS:
                idlest = min(self._sessions.values(), key=lambda s: s.last_active)
                del self._sessions[idlest.session_id]
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
