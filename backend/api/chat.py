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

What the host did is visible in three more events (they add to the stream and
change nothing else in it):

- ``event: stage`` ``{role, model, status}``: ``start`` after the ``execution``
  echo and before the first model call, ``end`` after the last one (before
  ``error`` or ``done``). The role is always ``planner`` until the roles package.
- ``event: tool`` ``{id, role, name, args, status, ms?, size?, preview?, error?}``:
  ``running`` when a tool call starts, then ``ok`` or ``error`` with the same
  ``id`` when it ends (``error`` when the handler raised - ``error`` is then the
  exception's type name only - or its result carries an ``error``); ``args`` as the
  model gave them, or ``{_truncated, _chars, _head}`` when their JSON text is longer
  than ``TOOL_ARGS_LIMIT``; ``size`` the result's length in characters, ``preview``
  its first ``TOOL_PREVIEW`` characters. ``bridge: false`` runs no tools and sends
  none. ``stage`` ``end`` is sent on every way the turn ends that can still send.
- ``event: usage`` ``{role, model, round, input, output, cache_read, cache_write,
  ms}`` after every model call (``round`` counts the calls of the turn from 1);
  a number the provider did not report is ``null``. The same row goes to the
  ``llm_calls`` table with the request's ``X-Run-Label``.

The model is the planner's (``backend.models_file.resolve``): ``LLM_*`` and the
``X-LLM-*`` headers as before, or, with a models file, the planner's entry there,
which the headers replace for the request. Events and ``llm_calls`` carry the model
the call actually went to.

Within one session the system prompt is the one its first turn was sent with
(``Session.system_prompt``) and the tool definitions are a constant: the
prefix a provider caches stays byte for byte the same from call to call.

The system prompt with the bridge is the package's host instructions (this
host's protocol) and one paragraph of this host's own, ``HOST_PARAGRAPH``: the
model here never writes or proposes code, uses saved tools only and, when none
fits, says so and points to the workbench. The package's text still speaks of
``execute_code`` and the snippet contract (a variant without code comes with a
later release); the paragraph after it is what this host holds the model to.
Skills are no longer part of the prompt (the Skills page still lists and edits
them), and there is no ``get_guidance``.

A tool result stays in the session up to ``TOOL_RESULT_KEEP`` characters after
the turn that produced it (head plus a one-line note).

``bridge: false`` is the comparison mode of the task page: one fixed base
prompt, no tools, no skills, no execution feedback - what a model would do
without the bridge.

A round that offers no tools (``bridge: false``, or the last round of a turn)
runs none: ``tool_calls`` the model returns anyway are logged and dropped, and
the round ends with the model's text, or an empty assistant message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from revit_bridge import host_instructions

from backend.api.bridge import set_device_context
from backend.api.errors import ApiError
from backend.api.model_tools import (
    TOOL_DEFINITIONS, WORKBENCH_LINK, ToolOutcome, call_tool, parse_arguments,
)
from backend.config import get_settings
from backend.llm import (
    SSE_DONE, CallStats, LLMError, assistant_message, format_sse, format_sse_event, stream_completion,
    tool_message,
)
from backend.log_store import get_client_ip, get_log_store, log_and_stream
from backend.models_file import NO_MODEL_FROM_FILE, resolve as resolve_model
from backend.ratelimit import chat_limiter, client_key
from backend.session import get_session_store

router = APIRouter(prefix="/api", tags=["chat"])
_log = logging.getLogger("backend.chat")

HISTORY_WINDOW = 40          # messages sent to the model, cut at a user turn
MAX_TOOL_ROUNDS = 8          # tool-call rounds per turn before the model must answer in text
TOOL_RESULT_KEEP = 8192      # characters of a tool result kept in the session after its turn
ELISION_NOTE = "[tool result elided after {kept} characters; the model saw all {total} in the turn that produced it]"
# An execution report (an ExecutionResult the page got from /run or /execute), as JSON
# text, enters the session and every later prompt. Up to EXECUTION_KEEP characters go in
# as they are; a longer report keeps its verdict fields whole and its ``result`` (a pack
# listing hundreds of elements runs past 100 KB) is cut to its head with
# EXECUTION_ELISION_NOTE, in the style of a capped tool result. Only a report beyond
# MAX_EXECUTION_CHARS - no legitimate result, protection against abuse - is refused (422).
EXECUTION_KEEP = 16_384
MAX_EXECUTION_CHARS = 4_194_304
EXECUTION_ELISION_NOTE = ("[execution result elided after {kept} characters; the page received all "
                          "{total} and the evidence ledger keeps the record]")

# The user message that carries an execution result back to the model (spec 10.8).
EXECUTION_PREFIX = "Execution result from the host (the designer did not write this):"

# This host's own paragraph after the package's host instructions (which still describe
# execute_code and the snippet contract): the model here never writes code. The link is
# the workbench page's path as a relative Markdown link, which the chat renders as a link.
HOST_PARAGRAPH = (
    "## In this host: saved tools only, no code\n"
    "This host runs only saved tools (capability packs). Here you never write, show or propose C# "
    "code: there is no execute_code and no get_guidance, and propose_spec refuses an execute_code "
    "spec. Everything above about writing code, the snippet contract and execute_code does not "
    "apply here. Pick a tool from list_tools, fill its parameters (missing_params, get_tool_choices, "
    "reconcile) and propose a run_tool spec. When no saved tool fits the request, write no code: "
    "tell the designer that no tool here does this, describe in one sentence the tool they would "
    f"need, and point them to the workbench with exactly this Markdown link: {WORKBENCH_LINK} - "
    "a new tool is tried and saved there, then it is in list_tools."
)

# bridge: false - the comparison baseline: no bridge instructions, no tools, no skills.
BASE_PROMPT = (
    "You are an assistant to an architect who works in Autodesk Revit. Answer their "
    "questions and requests as helpfully as you can, in their language."
)

# The one role of the host loop until the roles package splits it (stage/tool/usage events).
ROLE = "planner"
NO_MODEL = ("No model configured: fill in Model settings on the Connect page "
            "or set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY on the server.")
TOOL_PREVIEW = 2000          # characters of a tool result in its closing ``tool`` event
TOOL_ERROR_TEXT = 500        # characters of the error text in a closing ``tool`` event
# Arguments whose JSON text is longer than TOOL_ARGS_LIMIT go out in both tool events as
# {"_truncated": true, "_chars": <length>, "_head": <first TOOL_ARGS_HEAD characters>}.
TOOL_ARGS_LIMIT = 8000
TOOL_ARGS_HEAD = 2000


def shown_arguments(raw: str, args: dict | None) -> dict:
    """A tool call's arguments as its ``tool`` events carry them: the object the model gave
    (``{"_raw": <text>}`` when that was not a JSON object), or, when its JSON text runs past
    ``TOOL_ARGS_LIMIT`` characters, the truncation marker with the length and the head."""
    shown = args if args is not None else {"_raw": raw}
    text = json.dumps(shown, ensure_ascii=False)
    if len(text) > TOOL_ARGS_LIMIT:
        return {"_truncated": True, "_chars": len(text), "_head": text[:TOOL_ARGS_HEAD]}
    return shown

# X-Run-Label: names a run of the token bench in llm_calls.
_RUN_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RUN_LABEL_DOC = ("Optional name of a measured run (the token bench): stored with every model call of "
                 "this turn in llm_calls, read back with GET /api/usage/runs. 1-64 characters of "
                 "A-Z a-z 0-9 _ . - (not . or ..); anything else is 422 invalid_args.")


def check_run_label(value: str | None) -> str | None:
    """``X-Run-Label`` as sent: absent is None; 1-64 of ``A-Z a-z 0-9 _ . -``, not ``.`` or
    ``..``, else 422 ``invalid_args``."""
    if value is None:
        return None
    if not _RUN_LABEL.fullmatch(value) or value in (".", ".."):
        raise ApiError(422, "invalid_args",
                       "X-Run-Label must be 1-64 characters of A-Z, a-z, 0-9, '_', '.', '-' (not '.' or '..')")
    return value


def tool_error_text(result) -> str | None:
    """The error a tool result reports (``{error, message?}``), as one line; None when it
    reports none."""
    if not isinstance(result, dict) or not result.get("error"):
        return None
    error = result["error"]
    text = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    message = result.get("message")
    if isinstance(message, str) and message:
        text = f"{text}: {message}"
    return text[:TOOL_ERROR_TEXT]


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
    """The package's host instructions and ``HOST_PARAGRAPH``; the base prompt without the
    bridge. No skills, no lessons: the model here writes no code."""
    if not bridge:
        return BASE_PROMPT
    return host_instructions().strip() + "\n\n" + HOST_PARAGRAPH


def execution_message(execution: dict, keep: int = EXECUTION_KEEP) -> str:
    """The fixed first line, then the ExecutionResult JSON as the page received it - at
    most ``keep`` characters of it. Past that the ``result`` field becomes its head plus
    ``EXECUTION_ELISION_NOTE`` (a string), so success, error, validation, evidence_id
    and warnings reach the model whole and the text is still JSON; only a report whose
    other fields alone are too long is cut as text."""
    text = json.dumps(execution, ensure_ascii=False)
    if len(text) > keep and "result" in execution:
        result = json.dumps(execution["result"], ensure_ascii=False)
        room = max(0, keep - (len(text) - len(result)) - len(EXECUTION_ELISION_NOTE) - 16)
        for _ in range(8):           # escaping the head as a JSON string makes it longer
            note = EXECUTION_ELISION_NOTE.format(kept=room, total=len(result))
            text = json.dumps({**execution, "result": f"{result[:room]} {note}"}, ensure_ascii=False)
            if len(text) <= keep or room == 0:
                break
            room = max(0, room - (len(text) - keep))
    if len(text) > keep:
        note = EXECUTION_ELISION_NOTE.format(kept=keep, total=len(text))
        text = f"{text[:keep]}\n{note}"
    return f"{EXECUTION_PREFIX}\n{text}"


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
async def chat(req: ChatRequest, request: Request,
               x_run_label: str | None = Header(None, description=RUN_LABEL_DOC)):
    if (req.message is None) == (req.execution is None):
        raise ApiError(422, "invalid_args", "send exactly one of message and execution")
    if req.message is not None and not req.message.strip():
        raise ApiError(422, "invalid_args", "message is empty")
    if req.execution is not None and not req.bridge:
        raise ApiError(422, "invalid_args", "execution feedback has no meaning without the bridge")
    if req.execution is not None:
        size = len(json.dumps(req.execution, ensure_ascii=False))
        if size > MAX_EXECUTION_CHARS:
            raise ApiError(422, "invalid_args",
                           f"execution is too large: {size} characters as JSON, at most {MAX_EXECUTION_CHARS}")
    run_label = check_run_label(x_run_label)
    if req.bridge:
        # The model's tools reach Revit through the selected device: the same header
        # checks as the bridge routes (403 on a wrong or revoked device key).
        await set_device_context(request)

    try:
        # The planner's model: LLM_* and the headers, or its entry in the models file
        # (the headers then replace that entry, as they replace LLM_* without it).
        target = await resolve_model(ROLE, request.headers)
    except LLMError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    llm = target.llm
    if not llm.configured:
        raise HTTPException(400, NO_MODEL if target.entry is None else NO_MODEL_FROM_FILE)

    session = get_session_store().get_or_create(req.session_id)
    turn = session.next_turn()
    text = req.message if req.message is not None else execution_message(req.execution)
    session.add({"role": "user", "content": text})
    if req.execution is not None and req.execution.get("evidence_id"):
        session.evidence_id = str(req.execution["evidence_id"])
    tools = TOOL_DEFINITIONS if req.bridge else None
    # The first turn's prompt for the whole session: the cached prefix stays the same.
    messages = [{"role": "system", "content": session.system_prompt(req.bridge, build_system_prompt)}]
    messages.extend(session.window(HISTORY_WINDOW))
    calls_log = get_log_store()

    def stage(status: str) -> str:
        return format_sse_event("stage", {"role": ROLE, "model": llm.model, "status": status})

    def tool_event(event_id: str, name: str, args: dict, status: str, **extra) -> str:
        return format_sse_event("tool", {"id": event_id, "role": ROLE, "name": name, "args": args,
                                         "status": status, **extra})

    async def usage(round_no: int, stats: CallStats, started: float) -> str:
        """The ``usage`` event of one model call; its ``llm_calls`` row is written first."""
        ms = int((time.monotonic() - started) * 1000)
        counts = stats.usage.as_dict()
        await asyncio.to_thread(calls_log.record_call, session_id=session.session_id, turn=turn,
                                round=round_no, role=ROLE, model=llm.model, usage=counts, ms=ms,
                                run_label=run_label)
        return format_sse_event("usage", {"role": ROLE, "model": llm.model, "round": round_no, **counts, "ms": ms})

    async def generate() -> AsyncGenerator[str, None]:
        if req.execution is not None:
            yield format_sse_event("execution", req.execution)
        yield stage("start")
        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                # The last round goes out without tools so the model has to answer in text.
                round_tools = tools if round_no < MAX_TOOL_ROUNDS else None
                parts: list[str] = []
                calls = None
                stats = CallStats()
                started = time.monotonic()
                try:
                    async for item in stream_completion(llm, messages, tools=round_tools, stats=stats,
                                                        **target.options):
                        if isinstance(item, str):
                            parts.append(item)
                            yield format_sse(item)
                        else:
                            calls = item
                except LLMError:
                    if stats.accepted:          # the endpoint took the call: it may have cost tokens
                        yield await usage(round_no + 1, stats, started)
                    raise
                yield await usage(round_no + 1, stats, started)
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
                for index, call in enumerate(calls, start=1):
                    event_id = f"{turn}.{round_no + 1}.{index}"     # unique in the session
                    args = parse_arguments(call.arguments)
                    shown_args = shown_arguments(call.arguments, args)
                    yield tool_event(event_id, call.name, shown_args, "running")
                    tool_started = time.monotonic()
                    try:
                        if args is None:
                            outcome = ToolOutcome({"error": "invalid_args",
                                                   "message": "arguments must be a JSON object"})
                        else:
                            outcome = await call_tool(call.name, args, session)
                        reply = tool_message(call, outcome.result)
                    except Exception as exc:
                        # The turn fails as before; the row still closes. The browser gets the
                        # exception's type only - its text (paths, internals) goes to the log.
                        _log.error("tool %s raised in session %s", call.name, session.session_id,
                                   exc_info=True)
                        yield tool_event(event_id, call.name, shown_args, "error",
                                         ms=int((time.monotonic() - tool_started) * 1000),
                                         error=type(exc).__name__)
                        raise
                    result_text = reply["content"]
                    error = tool_error_text(outcome.result)
                    closing = {"ms": int((time.monotonic() - tool_started) * 1000), "size": len(result_text),
                               "preview": result_text[:TOOL_PREVIEW]}
                    if error is not None:
                        closing["error"] = error
                    yield tool_event(event_id, call.name, shown_args, "error" if error else "ok", **closing)
                    if outcome.spec_event is not None:
                        yield format_sse_event("spec", outcome.spec_event)
                    # The session keeps at most the head; this turn sees all of it.
                    session.add(kept_tool_message(reply))
                    messages.append(reply)
        except LLMError as exc:
            yield stage("end")
            yield format_sse_event("error", {"detail": str(exc)})
            return
        except Exception:
            # Anything else ends the turn as before (the response breaks off), but the stage
            # is closed first. A generator closed by a client that went away (GeneratorExit,
            # cancellation) can send nothing more: the page marks what stayed open.
            yield stage("end")
            raise
        yield stage("end")
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
