"""Interaction logs: what was asked and answered, in SQLite.

Only the conversation text, the model name, timing and the client address
are stored. Request headers - and therefore the model key - never reach
this module. Execution evidence (what changed in Revit) is the package's
``evidence`` ledger in a later phase, not this table.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

_log = logging.getLogger("backend.logs")

RETENTION_DAYS = 90
MAX_TEXT = 10_000


class InteractionLogStore:
    """Thread-safe SQLite store."""

    def __init__(self, db_path: Path | str):
        self._db_path = str(db_path)
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
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
        _log.info("interaction logs at %s", self._db_path)

    def _purge_old(self, retention_days: int) -> None:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            deleted = self.delete_before(cutoff.strftime("%Y-%m-%d %H:%M:%S"))
            if deleted:
                _log.info("purged %d log(s) older than %d days", deleted, retention_days)
        except Exception as exc:  # noqa: BLE001
            _log.warning("log retention purge failed: %s", exc)

    # -- write -----------------------------------------------------------------

    def log(self, *, module: str, session_id: str | None = None, client_ip: str | None = None,
            user_agent: str | None = None, user_input: str | None = None,
            assistant_output: str | None = None, model: str | None = None,
            duration_ms: int | None = None, status: str = "ok") -> None:
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
            where.append("timestamp <= ?"); params.append(end_date)
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
    model: str | None = None,
) -> AsyncGenerator[str, None]:
    """Pass an SSE stream through unchanged and log the assembled reply after it ends."""
    store = get_log_store()
    tokens: list[str] = []
    start = time.time()
    status = "ok"
    try:
        async for chunk in sse_generator:
            for line in chunk.split("\n"):
                if line.startswith("data: ") and "[DONE]" not in line:
                    try:
                        token = json.loads(line[6:])
                        if isinstance(token, str):
                            tokens.append(token)
                    except (json.JSONDecodeError, ValueError):
                        pass
            yield chunk
    except Exception as exc:
        status = f"error: {exc}"
        raise
    finally:
        await asyncio.to_thread(
            store.log,
            module=module, session_id=session_id, client_ip=client_ip,
            user_agent=user_agent, user_input=user_input,
            assistant_output="".join(tokens), model=model,
            duration_ms=int((time.time() - start) * 1000), status=status,
        )
