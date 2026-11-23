"""``/api/usage`` - the model calls the chat made, as ``llm_calls`` holds them; the same
admin rule as ``/api/logs`` (open on a local host, ``X-Admin-Token`` on a cloud one).

A token count the provider did not report is ``null`` in a row, and a sum over calls is
``null`` as soon as one of them lacks that count: unknown is never added up as 0.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from backend.api.logs import verify_admin
from backend.log_store import get_log_store

router = APIRouter(prefix="/api/usage", tags=["usage"])


class CallRow(BaseModel):
    """One model call."""
    session_id: str
    turn: int
    round: int
    role: str
    model: str | None
    input: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    ms: int
    created_at: str
    run_label: str | None


class SessionUsage(BaseModel):
    session_id: str
    items: list[CallRow]


class RunTotal(BaseModel):
    """The calls of one run label: each token sum is null when any call lacks it."""
    run_label: str
    calls: int
    input: int | None
    output: int | None
    cache_read: int | None
    cache_write: int | None
    ms: int
    first_at: str
    last_at: str


class RunList(BaseModel):
    items: list[RunTotal]


@router.get("", dependencies=[Depends(verify_admin)], response_model=SessionUsage)
async def session_usage(session_id: str = Query(..., min_length=1, max_length=128,
                                                description="the X-Session-Id of a chat")):
    """Every model call of one chat session, oldest first."""
    return {"session_id": session_id, "items": get_log_store().calls_of_session(session_id)}


@router.get("/runs", dependencies=[Depends(verify_admin)], response_model=RunList)
async def run_usage():
    """One line per ``X-Run-Label`` that reached the chat: calls, token sums, total time,
    first and last call (UTC, ``YYYY-MM-DD HH:MM:SS``)."""
    return {"items": get_log_store().runs()}
