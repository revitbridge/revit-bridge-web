"""What a long-running host keeps bounded: old logs, sessions, execution reports, model calls."""
from __future__ import annotations

import asyncio
import contextlib
import time

import httpx
import pytest

from backend import llm as llm_module
from backend import log_store as log_store_module
from backend import session as session_module
from backend.api import chat as chat_module
from backend.llm import LLMError, LLMSettings
from backend.session import get_session_store
from tests.test_host_loop import HEADERS, model, sse, tokens  # noqa: F401 - the model fixture
from tests.fake_model import text_turn


# -- interaction logs: purged while the host runs, not only at startup ---------------------

def test_old_logs_are_purged_while_the_host_runs(tmp_path, monkeypatch):
    store = log_store_module.InteractionLogStore(tmp_path / "logs.db")
    with contextlib.closing(store._conn()) as conn, conn:
        conn.execute("INSERT INTO interaction_logs (timestamp, module) VALUES (?, ?)",
                     ("2000-01-01 00:00:00", "chat"))
    store.log(module="chat", user_input="today")
    assert store.query()["total"] == 2                      # the next purge is not due yet

    later = time.monotonic() + log_store_module.PURGE_INTERVAL_SECONDS + 1
    monkeypatch.setattr(log_store_module.time, "monotonic", lambda: later)
    store.log(module="chat", user_input="a day later")
    rows = store.query()["items"]
    assert [r["user_input"] for r in rows] == ["a day later", "today"]


# -- sessions: a cap, oldest first -----------------------------------------------------------

def test_the_session_store_has_a_cap_and_drops_the_idlest_first(monkeypatch):
    monkeypatch.setattr(session_module, "MAX_SESSIONS", 3)
    store = session_module.SessionStore()

    ids = [store.get_or_create().session_id for _ in range(3)]
    now = time.time()
    for age, session_id in zip((30, 20, 10), ids):                # created in this order
        store._sessions[session_id].last_active = now - age
    assert store.get_or_create(ids[0]).session_id == ids[0]     # the first one is used again
    newest = store.get_or_create().session_id

    assert store.get(ids[1]) is None                            # the idlest one went
    assert all(store.get(i) is not None for i in (ids[0], ids[2], newest))
    assert len(store._sessions) == 3


# -- execution reports: bounded before they enter the session and the prompt -------------------

def test_an_oversized_execution_report_is_refused(client, model):
    big = {"success": True, "result": "x" * (chat_module.MAX_EXECUTION_CHARS + 1)}
    resp = client.post("/api/chat", json={"execution": big}, headers=HEADERS)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_args" and "execution" in resp.json()["message"]
    assert model.requests == []

    model.turns.append(text_turn("reported"))
    fits = {"success": True, "result": "x" * 1000, "evidence_id": "ev_1"}
    resp = client.post("/api/chat", json={"execution": fits}, headers=HEADERS)
    assert resp.status_code == 200 and tokens(sse(resp.text)) == "reported"


# -- one model call has an overall deadline ----------------------------------------------------

def test_a_trickling_endpoint_cannot_hold_a_call_forever(monkeypatch):
    """Every chunk arrives well inside the per-read timeout; the call as a whole still ends."""
    monkeypatch.setattr(llm_module, "MODEL_CALL_DEADLINE", 0.5)

    async def trickle():
        yield b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        for _ in range(40):                                   # 4 s in all if nothing stops it
            await asyncio.sleep(0.1)
            yield b'data: {"choices":[{"delta":{"content":"."}}]}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=trickle())

    real_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", patched)

    async def collect():
        settings = LLMSettings(base_url="https://api.example/v1", model="m", api_key="k")
        got = []
        async for item in llm_module.stream_completion(settings, [{"role": "user", "content": "x"}]):
            got.append(item)
        return got

    t0 = time.monotonic()
    with pytest.raises(LLMError) as exc:
        asyncio.run(collect())
    assert time.monotonic() - t0 < 2.0
    assert exc.value.status == 502 and "0.5" in str(exc.value)
