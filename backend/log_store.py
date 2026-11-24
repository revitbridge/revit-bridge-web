"""Interaction logs: what was asked and answered, in SQLite.

Only the conversation text, the model name, timing and the client address
are stored. Request headers - and therefore the model key - never reach
this module. Execution evidence (what changed in Revit) is the package's
``evidence`` ledger in a later phase, not this table.

``llm_calls`` (same database) has one row per model call of the chat: session,
turn, round, role, model name, the four token counts (``NULL`` where the
provider reported none), duration and the optional run label of the request
(``X-Run-Label``). No message text, no endpoint address, no key. The table is
created on an existing database at startup like the first one; rows age out
with the interaction logs (``RETENTION_DAYS``).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger("backend.logs")

RETENTION_DAYS = 90
MAX_TEXT = 10_000
MAX_MODEL_NAME = 200
# The largest token count kept as a number (backend.llm.MAX_COUNT): a count beyond it, or a
# sum that passes it, is unknown - never an overflow, never a dropped row.
MAX_COUNT = 2 ** 53
_TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_write")


def _token_count(value) -> int | None:
    """``value`` as a count from 0 to ``MAX_COUNT`` (an integral float is fine), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    return value if 0 <= value <= MAX_COUNT else None
_CALL_COLUMNS = ("session_id, turn, round, role, model, input, output, cache_read, cache_write, "
                 "ms, created_at, run_label")
# Rows older than RETENTION_DAYS go at startup and then at most once a day, on the
# first write after the interval: a container that runs for months stays bounded.
PURGE_INTERVAL_SECONDS = 24 * 60 * 60


class InteractionLogStore:
    """Thread-safe SQLite store."""

    def __init__(self, db_path: Path | str):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._next_purge = 0.0
        self._init_db()
        self._purge_old(RETENTION_DAYS)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with contextlib.closing(self._conn()) as conn, conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interaction_logs (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp        TEXT NOT NULL,
                    module           TEXT NOT NULL,
                    session_id       TEXT,
                    client_ip        TEXT,
                    user_agent       TEXT,
                    user_input       TEXT,
                    assistant_output TEXT,
                    model            TEXT,
                    duration_ms      INTEGER,
                    status           TEXT DEFAULT 'ok'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON interaction_logs(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_module ON interaction_logs(module)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ip ON interaction_logs(client_ip)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT NOT NULL,
                    session_id  TEXT NOT NULL,
                    turn        INTEGER NOT NULL,
                    round       INTEGER NOT NULL,
                    role        TEXT NOT NULL,
                    model       TEXT,
                    input       INTEGER,
                    output      INTEGER,
                    cache_read  INTEGER,
                    cache_write INTEGER,
                    ms          INTEGER NOT NULL,
                    run_label   TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_label ON llm_calls(run_label)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(created_at)")
        _log.info("interaction logs at %s", self._db_path)

    def _purge_old(self, retention_days: int) -> None:
        self._next_purge = time.monotonic() + PURGE_INTERVAL_SECONDS
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
            deleted = self.delete_before(cutoff)
            if deleted:
                _log.info("purged %d log(s) older than %d days", deleted, retention_days)
            calls = self.delete_calls_before(cutoff)
            if calls:
                _log.info("purged %d model call record(s) older than %d days", calls, retention_days)
        except Exception as exc:  # noqa: BLE001
            _log.warning("log retention purge failed: %s", exc)

    # -- write -----------------------------------------------------------------

    def log(self, *, module: str, session_id: str | None = None, client_ip: str | None = None,
            user_agent: str | None = None, user_input: str | None = None,
            assistant_output: str | None = None, model: str | None = None,
            duration_ms: int | None = None, status: str = "ok") -> None:
        if time.monotonic() >= self._next_purge:
            self._purge_old(RETENTION_DAYS)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if user_input is not None:
            user_input = user_input[:MAX_TEXT]
        if assistant_output is not None:
            assistant_output = assistant_output[:MAX_TEXT]
        with self._lock:
            try:
                with contextlib.closing(self._conn()) as conn, conn:
                    conn.execute(
                        """INSERT INTO interaction_logs
                           (timestamp, module, session_id, client_ip, user_agent,
                            user_input, assistant_output, model, duration_ms, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ts, module, session_id, client_ip, user_agent, user_input,
                         assistant_output, model, duration_ms, status),
                    )
            except Exception as exc:  # noqa: BLE001
                _log.error("failed to write interaction log: %s", exc)

    # -- read ------------------------------------------------------------------

    def query(self, *, module: str | None = None, client_ip: str | None = None,
              keyword: str | None = None, start_date: str | None = None,
              end_date: str | None = None, status: str | None = None,
              limit: int = 50, offset: int = 0) -> dict:
        where: list[str] = []
        params: list = []
        if module:
            where.append("module = ?"); params.append(module)
        if client_ip:
            where.append("client_ip LIKE ?"); params.append(f"%{client_ip}%")
        if keyword:
            where.append("(user_input LIKE ? OR assistant_output LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if start_date:
            where.append("timestamp >= ?"); params.append(start_date)
        if end_date:
            # Timestamps are "YYYY-MM-DD HH:MM:SS"; a bare date must include
            # the whole of that day, not just its first second.
            bound = end_of_day(end_date)
            if bound != end_date.strip():
                where.append("timestamp < ?"); params.append(bound)
            else:
                where.append("timestamp <= ?"); params.append(bound)
        if status:
            where.append("status = ?"); params.append(status)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        with contextlib.closing(self._conn()) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM interaction_logs{where_sql}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM interaction_logs{where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}

    def stats(self) -> dict:
        with contextlib.closing(self._conn()) as conn:
            by_module = conn.execute(
                "SELECT module, COUNT(*) AS count FROM interaction_logs GROUP BY module ORDER BY count DESC"
            ).fetchall()
            by_ip = conn.execute(
                """SELECT client_ip, COUNT(*) AS count FROM interaction_logs
                   WHERE client_ip IS NOT NULL AND client_ip != ''
                   GROUP BY client_ip ORDER BY count DESC LIMIT 20"""
            ).fetchall()
            daily = conn.execute(
                """SELECT DATE(timestamp) AS day, COUNT(*) AS count FROM interaction_logs
                   GROUP BY DATE(timestamp) ORDER BY day DESC LIMIT 30"""
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM interaction_logs").fetchone()[0]
            errors = conn.execute("SELECT COUNT(*) FROM interaction_logs WHERE status != 'ok'").fetchone()[0]
        return {
            "total": total,
            "errors": errors,
            "by_module": [dict(r) for r in by_module],
            "by_ip": [dict(r) for r in by_ip],
            "daily": [dict(r) for r in daily],
        }

    def delete_before(self, date_str: str) -> int:
        with self._lock:
            with contextlib.closing(self._conn()) as conn, conn:
                return conn.execute("DELETE FROM interaction_logs WHERE timestamp < ?", (date_str,)).rowcount

    def delete_calls_before(self, date_str: str) -> int:
        """Model calls recorded before ``date_str`` (the retention purge; no route deletes them)."""
        with self._lock:
            with contextlib.closing(self._conn()) as conn, conn:
                return conn.execute("DELETE FROM llm_calls WHERE created_at < ?", (date_str,)).rowcount

    # -- model calls -------------------------------------------------------------

    def record_call(self, *, session_id: str, turn: int, round: int, role: str, model: str | None,
                    usage: dict, ms: int, run_label: str | None = None) -> None:
        """One model call; ``usage`` is ``{input, output, cache_read, cache_write}`` with
        ``None`` kept as ``NULL``; a value that is not a count from 0 to ``MAX_COUNT`` is
        stored as ``NULL`` too, so the row is kept. A failed write is logged, never raised."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        counts = [_token_count(usage.get(c)) for c in _TOKEN_COLUMNS]
        ms = _token_count(ms) or 0
        with self._lock:
            try:
                with contextlib.closing(self._conn()) as conn, conn:
                    conn.execute(
                        """INSERT INTO llm_calls
                           (created_at, session_id, turn, round, role, model,
                            input, output, cache_read, cache_write, ms, run_label)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ts, session_id, turn, round, role, (model or "")[:MAX_MODEL_NAME] or None,
                         *counts, ms, run_label),
                    )
            except Exception as exc:  # noqa: BLE001
                _log.error("failed to write a model call: %s", exc)

    def calls_of_session(self, session_id: str) -> list[dict]:
        """Every model call of one session, oldest first."""
        with contextlib.closing(self._conn()) as conn:
            rows = conn.execute(
                f"SELECT {_CALL_COLUMNS} FROM llm_calls WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def runs(self) -> list[dict]:
        """One line per run label: the number of calls, each token column's sum (``None``
        when any call of the run lacks that number, or when the sum passes ``MAX_COUNT``),
        the total time, first and last call, and ``by_role``: the same sums per role and
        model of the run, in the order of their first call. Summed with ``TOTAL`` (a float),
        which never raises SQLite's integer overflow the way ``SUM`` does."""
        sums = ", ".join(
            f"CASE WHEN COUNT({c}) = COUNT(*) THEN TOTAL({c}) END AS {c}" for c in _TOKEN_COLUMNS)
        with contextlib.closing(self._conn()) as conn:
            rows = conn.execute(
                f"""SELECT run_label, COUNT(*) AS calls, {sums}, TOTAL(ms) AS ms,
                           MIN(created_at) AS first_at, MAX(created_at) AS last_at
                    FROM llm_calls WHERE run_label IS NOT NULL
                    GROUP BY run_label ORDER BY MIN(id)"""
            ).fetchall()
            parts = conn.execute(
                f"""SELECT run_label, role, model, COUNT(*) AS calls, {sums}, TOTAL(ms) AS ms
                    FROM llm_calls WHERE run_label IS NOT NULL
                    GROUP BY run_label, role, model ORDER BY MIN(id)"""
            ).fetchall()
        by_run: dict[str, list[dict]] = {}
        for row in parts:
            part = _counted(dict(row))
            by_run.setdefault(part.pop("run_label"), []).append(part)
        result = []
        for row in rows:
            item = _counted(dict(row))
            item["by_role"] = by_run.get(item["run_label"], [])
            result.append(item)
        return result


def _counted(item: dict) -> dict:
    """A summed row as counts: each token sum a count or ``None``, the time an integer."""
    for column in _TOKEN_COLUMNS:
        item[column] = _token_count(item[column])
    item["ms"] = int(item["ms"])
    return item


def _sse_frames(chunk: str):
    """Yield ``(event, data)`` for every ``data:`` line in a chunk of SSE text."""
    event = ""
    for line in chunk.split("\n"):
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            yield event, line[6:]
            event = ""


def sse_tokens(chunk: str) -> list[str]:
    """String payloads of plain ``data:`` frames - the streamed reply text."""
    found: list[str] = []
    for event, data in _sse_frames(chunk):
        if event or data.strip() == "[DONE]":
            continue
        try:
            token = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(token, str):
            found.append(token)
    return found


def sse_error_status(chunk: str) -> str | None:
    """``"error: <detail>"`` when the chunk carries an ``event: error`` frame."""
    for event, data in _sse_frames(chunk):
        if event != "error":
            continue
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            payload = data
        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        return f"error: {detail}"
    return None


def end_of_day(end_date: str) -> str:
    """Upper bound for an ``end_date`` filter.

    ``"2026-09-18"`` becomes ``"2026-09-19"`` (the next day, used exclusively)
    so the end day is included; a value that already carries a time is
    returned unchanged and compared inclusively.
    """
    text = end_date.strip()
    if len(text) == 10:
        try:
            day = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return text
        return (day + timedelta(days=1)).strftime("%Y-%m-%d")
    return text


_store: InteractionLogStore | None = None


def get_log_store() -> InteractionLogStore:
    global _store
    if _store is None:
        from backend.config import get_settings
        _store = InteractionLogStore(get_settings().log_db_path)
    return _store


def reset_log_store() -> None:
    global _store
    _store = None


def get_client_ip(request) -> str:
    """Peer address as uvicorn reports it (``--proxy-headers`` rewrites it
    for trusted proxies, see ``FORWARDED_ALLOW_IPS``)."""
    return request.client.host if request.client else "unknown"


async def log_and_stream(
    sse_generator: AsyncGenerator[str, None],
    *,
    module: str,
    session_id: str | None = None,
    client_ip: str = "unknown",
    user_agent: str = "",
    user_input: str = "",
    model: str | Callable[[], str | None] | None = None,
) -> AsyncGenerator[str, None]:
    """Pass an SSE stream through unchanged and log the assembled reply after it ends.
    model may be a function, called then: the model (or models) the stream actually used.

    Frames are inspected by type: string ``data:`` payloads are the reply,
    an ``event: error`` frame (the model endpoint failed; no exception is
    raised for it) marks the record ``error: <detail>`` so ``/api/logs/stats``
    counts model failures alongside crashes.
    """
    store = get_log_store()
    tokens: list[str] = []
    start = time.time()
    status = "ok"
    try:
        async for chunk in sse_generator:
            event_status = sse_error_status(chunk)
            if event_status:
                status = event_status
            tokens.extend(sse_tokens(chunk))
            yield chunk
    except Exception as exc:
        status = f"error: {exc}"
        raise
    finally:
        await asyncio.to_thread(
            store.log,
            module=module, session_id=session_id, client_ip=client_ip,
            user_agent=user_agent, user_input=user_input,
            assistant_output="".join(tokens), model=model() if callable(model) else model,
            duration_ms=int((time.time() - start) * 1000), status=status,
        )
