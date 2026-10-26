"""``POST /api/chat`` - the host loop: the designer's own model, with tools.

Each turn streams one conversation step over SSE. The model may call the
package's read-only tools (snapshot, query, packs, missing_params,
reconcile) and ``propose_spec`` (see ``backend.api.model_tools``); the host
runs each call, feeds the result back as a ``tool`` message and streams the
next completion, until the model answers in text. Confirmation and
execution never happen here: the page confirms (``POST /spec/confirm``)
and runs with the token it holds, then reports the result back through
this route (``execution`` instead of ``message``) so the model can report
faithfully what the validator found.

Wire format: SSE frames ``data: "<json string token>"`` for the reply text,
``event: spec`` ``data: {spec, card, errors, reconcile}`` when the model
proposed a spec, ``event: execution`` ``data: <ExecutionResult JSON>`` when
the page reported an execution (first frame of that turn), ``event: error``
for a model failure, and a final ``event: done`` / ``[DONE]``; the response
header ``X-Session-Id`` names the server-side session.

The system prompt with the bridge is the package's host instructions, the
enabled skills (by default the wheel's ``revit-bridge`` protocol and
``revit-core``; see ``backend.skill_store``) and one line that points to
``get_guidance`` for every other lesson category.

``bridge: false`` is the comparison mode of the task page: one fixed base
prompt, no tools, no skills, no execution feedback - what a model would do
without the bridge.

A round that offers no tools (``bridge: false``, or the last round of a turn)
runs none: ``tool_calls`` the model returns anyway are logged and dropped, and
the round ends with the model's text, or an empty assistant message.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from revit_bridge import host_instructions
from revit_bridge.guidance import CATEGORIES

from backend.api.bridge import set_device_context
from backend.api.errors import ApiError
from backend.api.model_tools import TOOL_DEFINITIONS, ToolOutcome, call_tool, parse_arguments
from backend.config import get_settings
from backend.llm import (
    SSE_DONE, LLMError, LLMSettings, assistant_message, format_sse, format_sse_event,
    stream_completion, tool_message,
)
from backend.log_store import get_client_ip, log_and_stream
from backend.ratelimit import chat_limiter, client_key
from backend.session import get_session_store
from backend.skill_store import get_skill_store

router = APIRouter(prefix="/api", tags=["chat"])
_log = logging.getLogger("backend.chat")

HISTORY_WINDOW = 40          # messages sent to the model, cut at a user turn
MAX_TOOL_ROUNDS = 8          # tool-call rounds per turn before the model must answer in text
TOOL_RESULT_KEEP = 8192      # characters of a tool result kept in the session after its turn
ELISION_NOTE = "[tool result elided after {kept} characters; the model saw all {total} in the turn that produced it]"

# The user message that carries an execution result back to the model (spec 10.8).
EXECUTION_PREFIX = "Execution result from the host (the designer did not write this):"

# The lessons that are not in the prompt: one line instead of every category's text.
GUIDANCE_POINTER = (
    "The lessons are not in this prompt, neither revit-core's nor those of the other categories ("
    + ", ".join(c for c in CATEGORIES if c != "revit-core")
    + '): call get_guidance - stage "orient" at the start of a task, '
    '"write" before you write C# (symbols, revit_categories), "fix" after a failed execution '
    '(errors, symbols); mode "index" lists every lesson.'
)

# bridge: false - the comparison baseline: no bridge instructions, no tools, no skills.
BASE_PROMPT = (
    "You are an assistant to an architect who works in Autodesk Revit. Answer their "
    "questions and requests as helpfully as you can, in their language."
)


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=8000)
    execution: dict | None = None      # an ExecutionResult the page got from /run or /execute
    session_id: str | None = None
    bridge: bool = True


# -- per-IP rate limit (protects a shared server-side key) ---------------------

def rate_limit(request: Request) -> None:
    if not chat_limiter.allow(client_key(request), get_settings().chat_rate_limit):
        raise HTTPException(429, "Too many requests")


def build_system_prompt(bridge: bool = True) -> str:
    """The package's host instructions, every enabled skill and the get_guidance pointer;
    the base prompt without the bridge."""
    if not bridge:
        return BASE_PROMPT
    parts = [host_instructions().strip()]
    skills = get_skill_store().active_prompt()
    if skills:
        parts.append("## Skills\n\n" + skills)
    parts.append("## Guidance\n\n" + GUIDANCE_POINTER)
    return "\n\n".join(parts)


def execution_message(execution: dict) -> str:
    """The fixed first line, then the ExecutionResult JSON as the page received it."""
    return f"{EXECUTION_PREFIX}\n{json.dumps(execution, ensure_ascii=False)}"


def kept_tool_message(message: dict, keep: int = TOOL_RESULT_KEEP) -> dict:
    """The tool message as the session stores it: a snapshot or an element listing can
    run to hundreds of KB, and every later turn would carry it again. The turn that
    produced it still sends it in full; later turns see the head and a note."""
    content = message.get("content") or ""
    if len(content) <= keep:
        return message
    note = ELISION_NOTE.format(kept=keep, total=len(content))
    return {**message, "content": f"{content[:keep]}\n{note}"}


@router.post("/chat", dependencies=[Depends(rate_limit)])
async def chat(req: ChatRequest, request: Request):
    if (req.message is None) == (req.execution is None):
        raise ApiError(422, "invalid_args", "send exactly one of message and execution")
    if req.message is not None and not req.message.strip():
        raise ApiError(422, "invalid_args", "message is empty")
    if req.execution is not None and not req.bridge:
        raise ApiError(422, "invalid_args", "execution feedback has no meaning without the bridge")
    if req.bridge:
        # The model's tools reach Revit through the selected device: the same header
        # checks as the bridge routes (403 on a wrong or revoked device key).
        await set_device_context(request)

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
    text = req.message if req.message is not None else execution_message(req.execution)
    session.add({"role": "user", "content": text})
    if req.execution is not None and req.execution.get("evidence_id"):
        session.evidence_id = str(req.execution["evidence_id"])
    tools = TOOL_DEFINITIONS if req.bridge else None
    messages = [{"role": "system", "content": build_system_prompt(req.bridge)}]
    messages.extend(session.window(HISTORY_WINDOW))

    async def generate() -> AsyncGenerator[str, None]:
        if req.execution is not None:
            yield format_sse_event("execution", req.execution)
        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                # The last round goes out without tools so the model has to answer in text.
                round_tools = tools if round_no < MAX_TOOL_ROUNDS else None
                parts: list[str] = []
                calls = None
                async for item in stream_completion(llm, messages, tools=round_tools):
                    if isinstance(item, str):
                        parts.append(item)
                        yield format_sse(item)
                    else:
                        calls = item
                content = "".join(parts)
                if calls and round_tools is None:
                    # Nothing was offered, so nothing runs: the round ends with the text.
                    _log.warning("not run: %d tool call(s) (%s) from a round without tools "
                                 "(bridge=%s, session %s)", len(calls), ", ".join(c.name for c in calls),
                                 req.bridge, session.session_id)
                    session.add({"role": "assistant", "content": content})
                    break
                if not calls:
                    if content:
                        session.add(assistant_message(content))
                    break
                assistant = assistant_message(content, calls)
                session.add(assistant)
                messages.append(assistant)
                for call in calls:
                    args = parse_arguments(call.arguments)
                    if args is None:
                        outcome = ToolOutcome({"error": "invalid_args",
                                               "message": "arguments must be a JSON object"})
                    else:
                        outcome = await call_tool(call.name, args, session)
                    if outcome.spec_event is not None:
                        yield format_sse_event("spec", outcome.spec_event)
                    reply = tool_message(call, outcome.result)
                    session.add(kept_tool_message(reply))     # the session keeps at most the head
                    messages.append(reply)                    # this turn sees all of it
        except LLMError as exc:
            yield format_sse_event("error", {"detail": str(exc)})
            return
        yield SSE_DONE

    return StreamingResponse(
        log_and_stream(
            generate(),
            module="chat",
            session_id=session.session_id,
            client_ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent", ""),
            user_input=text,
            model=llm.model,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Session-Id": session.session_id},
    )
