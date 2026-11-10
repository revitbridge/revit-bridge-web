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

def test_a_large_execution_report_is_accepted_and_cut_with_a_note(client, model):
    """A pack that lists hundreds of elements is a legitimate result: the report is taken,
    and what goes into the session and the prompt is bounded - the result's head and a
    note in the style of a capped tool result, the verdict fields whole."""
    import json

    rows = [{"Id": i, "Name": f"element-{i:05d}", "Category": "OST_Walls"} for i in range(1600)]
    big = {"success": True, "error": None, "tool": "query_model_stats", "result": rows,
           "validation": {"validator": "count_delta", "passed": True, "checks": [{"detail": "ok", "passed": True}]},
           "evidence_id": "ev_big", "warnings": ["w1"]}
    assert len(json.dumps(big, ensure_ascii=False)) > 100_000

    model.turns.append(text_turn("reported"))
    resp = client.post("/api/chat", json={"execution": big}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    frames = sse(resp.text)
    assert frames[0] == ("execution", big)                       # the page gets its report back whole
    sent = model.last_messages()[-1]["content"]
    head, body = sent.split("\n", 1)
    assert head == chat_module.EXECUTION_PREFIX
    assert len(body) <= chat_module.EXECUTION_KEEP
    shown = json.loads(body)                                    # still JSON, the verdict intact
    assert {k: v for k, v in shown.items() if k != "result"} == {k: v for k, v in big.items() if k != "result"}
    assert shown["result"].startswith('[{"Id": 0, "Name": "element-00000"')
    assert "[execution result elided after" in shown["result"]
    session = get_session_store().get(resp.headers["X-Session-Id"])
    assert session.history[0]["content"] == sent and session.evidence_id == "ev_big"

    # A report whose bulk is not its result is cut as text, with the same note.
    odd = chat_module.execution_message({"success": True, "warnings": ["w" * 50_000]})
    assert len(odd) < len(chat_module.EXECUTION_PREFIX) + chat_module.EXECUTION_KEEP + 200
    assert odd.endswith("the evidence ledger keeps the record]")


def test_an_absurd_execution_report_is_refused(client, model):
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
