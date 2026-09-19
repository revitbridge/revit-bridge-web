"""``POST /api/chat`` - streamed conversation with the designer's own model.

The host loop of this phase is deliberately thin: system prompt = host
conventions + capability pack index + enabled skills; the model answers in
prose and, when asked to act, in one ```csharp block that the Task page
extracts, shows for review and sends to ``/api/v1/bridge/execute``.
Tool calling, snapshots and the spec gate arrive with the task page rewrite.

Wire format (unchanged from the previous host): SSE frames
``data: "<json string token>"`` and a final ``event: done`` / ``[DONE]``;
the response header ``X-Session-Id`` names the server-side session.
"""
from __future__ import annotations

import threading
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from revit_bridge.revit.settings import RevitSettings

from backend.capabilities import get_tool_store
from backend.config import get_settings
from backend.llm import SSE_DONE, LLMError, LLMSettings, format_sse, format_sse_event, stream_chat
from backend.log_store import get_client_ip, log_and_stream
from backend.session import get_session_store
from backend.skill_store import get_skill_store

router = APIRouter(prefix="/api", tags=["chat"])

HISTORY_TURNS = 20

HOST_INSTRUCTIONS = """\
You are the model behind revit-bridge-web, a demo host connected to a running
Autodesk Revit through the revit-bridge add-in. A designer is talking to you.

## What you may do

- Answer questions about the Revit model and the Revit API.
- Propose C# to run in Revit. Put the code in exactly one ```csharp block.
  The designer reviews it and runs it themselves; you cannot execute anything.
- When a capability pack below matches the request, say so and tell the
  designer to run it from the Capabilities page instead of writing new code.

## C# execution conventions (revit-bridge add-in)

- The code is the body of `public static object Execute(Document document, object[] parameters)`.
- `document` is in scope and a Transaction is already open: never open your own.
- End with `return <object>;` - anonymous objects and lists are serialised to JSON.
- Internal units are feet; convert millimetres with `/ 304.8`.
- Target the API of the running Revit (2026 unless the designer says otherwise).

## Parameter source protocol

Every value in the code must come from the designer's own words, from data
they pasted (levels, element ids, project units) or from a question you
asked and they answered. Never assume a level, a type name, a coordinate or
a dimension. If something is missing, ask - one short, concrete question per
missing value - before writing code.

## Faithful reporting

If the designer pastes an execution result, report exactly what it says.
An error is an error; do not soften it and do not claim success.
"""


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None


# -- per-IP sliding window rate limit (protects a shared server-side key) ------

_rate_lock = threading.Lock()
_rate_hits: dict[str, list[float]] = {}
_RATE_WINDOW = 60.0


def _prune_idle(now: float) -> None:
    """Forget addresses whose every hit is older than the window.

    Called under the lock on each check, so the table only ever holds
    addresses seen within the last minute instead of every visitor since
    the process started.
    """
    cutoff = now - _RATE_WINDOW
    for ip in [ip for ip, hits in _rate_hits.items() if not hits or hits[-1] <= cutoff]:
        del _rate_hits[ip]


def rate_limit(request: Request) -> None:
    limit = get_settings().chat_rate_limit
    if limit <= 0:
        return
    ip = get_client_ip(request)
    now = time.time()
    with _rate_lock:
        _prune_idle(now)
        hits = [t for t in _rate_hits.get(ip, []) if now - t < _RATE_WINDOW]
        if len(hits) >= limit:
            raise HTTPException(429, "Too many requests")
        hits.append(now)
        _rate_hits[ip] = hits


def build_system_prompt() -> str:
    """Host conventions, the capability pack index, then every enabled skill."""
    parts = [HOST_INSTRUCTIONS.strip()]
    tools = get_tool_store().list_tools()
    if tools:
        lines = ["## Capability packs on this host (run from the Capabilities page)"]
        for tool in tools:
            params = ", ".join(p.get("name", "?") for p in tool.parameters) or "none"
            lines.append(f"- `{tool.name}`: {tool.description} (params: {params})")
        parts.append("\n".join(lines))
    skills = get_skill_store().active_prompt()
    if skills:
        parts.append("## Skills\n\n" + skills)
    revit = RevitSettings.from_env()
    parts.append(f"The add-in endpoint of this host is {revit.host}:{revit.port} (TCP) "
                 f"or a remote slot; the designer chooses on the Connect page.")
    return "\n\n".join(parts)


@router.post("/chat", dependencies=[Depends(rate_limit)])
async def chat(req: ChatRequest, request: Request):
    settings = get_settings()
    try:
        llm = LLMSettings.resolve(request.headers, settings)
    except LLMError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    if not llm.configured:
        raise HTTPException(
            400,
            "No model configured: fill in Model settings on the Connect page "
            "or set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY on the server.",
        )

    session = get_session_store().get_or_create(req.session_id)
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages.extend(session.history[-HISTORY_TURNS:])
    messages.append({"role": "user", "content": req.message})

    async def generate() -> AsyncGenerator[str, None]:
        reply: list[str] = []
        try:
            async for token in stream_chat(llm, messages):
                reply.append(token)
                yield format_sse(token)
        except LLMError as exc:
            yield format_sse_event("error", {"detail": str(exc)})
            return
        finally:
            session.add_message("user", req.message)
            if reply:
                session.add_message("assistant", "".join(reply))
        yield SSE_DONE

    return StreamingResponse(
        log_and_stream(
            generate(),
            module="chat",
            session_id=session.session_id,
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            user_input=req.message,
            model=llm.model,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Session-Id": session.session_id},
    )
