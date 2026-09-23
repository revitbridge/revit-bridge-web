"""Per-IP sliding-window rate limit, shared by the routes that must not be hammered.

Each guarded route keeps its own table, so a burst on one never blocks
another: ``/api/chat`` (a shared server-side model key), ``/spec/confirm`` and
``/devices/pair`` (each call writes to the data volume) use ``CHAT_RATE_LIMIT``
requests per minute per client address; ``/devices/redeem`` (guessing a pairing
code) has a low fixed limit of its own. Addresses whose every hit is older than
the window are forgotten on each check, so a table only ever holds the last
minute's visitors.
"""
from __future__ import annotations

import threading
import time

from backend.log_store import get_client_ip

WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(self, window: float = WINDOW_SECONDS, clock=time.time):
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int) -> bool:
        """Record one hit for ``key``; False when it already had ``limit`` in the window.
        A limit of 0 or less disables the check."""
        if limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            self._prune_idle(now)
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= limit:
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def _prune_idle(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            del self._hits[key]

    def keys(self) -> set[str]:
        with self._lock:
            return set(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


chat_limiter = RateLimiter()
confirm_limiter = RateLimiter()
pair_limiter = RateLimiter()
redeem_limiter = RateLimiter()


def client_key(request) -> str:
    return get_client_ip(request)


def reset_all() -> None:
    """Tests start every case with empty tables."""
    chat_limiter.reset()
    confirm_limiter.reset()
    pair_limiter.reset()
    redeem_limiter.reset()
