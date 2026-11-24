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

The designer's picks: a message may carry ``mentions`` (``[{kind, id, label?}]``,
at most 20; ``@level``, ``@type``, ``@link`` as ``GET /api/v1/bridge/mentions``
listed them). Before the model is called the host checks them against the open
model with the package's ``resolve_mentions`` and sends ``event: mentions``
``{resolved, not_found, invalid, unresolved, warnings?}`` as the turn's first
frame (see ``check_mentions``). The verified ones are appended to the
designer's message as the package's ``mention_block`` (the "Designer's
selections" the system prompt's rule speaks of) and kept with the session under
the device scope of the request: a spec may bind them in this turn or any later
one on that device, and nothing else (``backend.mention_check``); a pick a later
turn finds gone leaves the session. A pick that is not found or not valid is not
sent; with no Revit to ask, or no answer within ``MENTION_CHECK_TIMEOUT`` seconds,
every pick is ``unresolved``, no block is added and the turn runs as without
picks. A message without picks sends no such event.

What the host did is visible in three more events (they add to the stream and
change nothing else in it):

- ``event: stage`` ``{role, model, status}``: ``start`` before a role's first model
  call (after the ``execution`` echo and the ``mentions`` report), ``end`` after its
  last one (before ``error`` or ``done``); one pair per role segment of the turn.
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

Three roles (the default; ``backend.roles`` has the table, the hand-offs and the
parsing). A message with the bridge goes to the **analyst** first: no tools, one JSON
routing ``{route, tool?, facts_needed, note}`` from the tool list, the digest of the
open model (the package's ``snapshot_digest``, taken by the host and kept with the
session until the page reports an execution), the open task and the message. Route
``chat`` or ``none``: the host streams ``note`` as the reply and the turn ends. Route
``query`` or ``tool``: the **planner** runs the loop with that route's tools, the
session's conversation and the message followed by the routing. An execution report
goes to the **supervisor** (``query`` only): the confirmed spec, the result's summary
and the validator's checks in, one JSON verdict ``{verdict, report, fix?, question?}``
out; the host streams ``report`` (and ``question``), holds the verdict to the
package's (``roles.enforce``) and, on ``retry``, runs the planner again with ``fix``
(at most ``roles.MAX_RETRIES`` times per task). A role's output that is not its JSON
gets one repair call, then the host's fallback. ``LLM_SINGLE_MODEL=1`` instead runs the
one loop of the planner as before the split, request for request and frame for frame.

Each role's model is ``backend.models_file.resolve(role, headers)``: ``LLM_*`` and the
``X-LLM-*`` headers as before, or, with a models file, the role's entry there (the
headers replace the planner's entry, and every role that shares it). Events and
``llm_calls`` carry the role and the model the call actually went to; ``round`` counts
the model calls of the turn across its roles.

Within one session each prompt is the one its first turn was sent with
(``Session.system_prompt``, ``Session.role_prompt``) and the tool definitions of a
role and route are a constant: the prefix a provider caches stays byte for byte the
same from call to call.

The system prompt of the one loop is the package's host instructions for a host
whose model writes no code, with the rule for the designer's selections
(``host_instructions(code=False, mentions=True)``), and one line of this host's
own, ``WORKBENCH_LINE``: when no saved tool fits, point to the workbench. The roles'
are the package's role texts (``host_instructions(role=..., code=False)``), the
planner's with ``WORKBENCH_LINE`` and the analyst's with ``roles.ANALYST_WORKBENCH_LINE``.
Skills are not part of any prompt (the Skills page still lists and edits them), and
there is no ``get_guidance``.

A tool result stays in the session up to ``TOOL_RESULT_KEEP`` characters after
the turn that produced it (head plus a one-line note).

``bridge: false`` is the comparison mode of the task page: one fixed base
prompt, no tools, no skills, no execution feedback, no roles - what a model would do
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
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from revit_bridge import host_instructions
from revit_bridge.mentions import MAX_MENTIONS, mention_block, resolve_mentions
from revit_bridge.snapshot import RevitQueryExecutor, snapshot_digest
from revit_bridge.snapshot.query import RevitQueryError

from backend import roles
from backend.api.bridge import current_scope, get_revit_client, list_packs, set_device_context, snapshot_now
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
from backend.models_file import NO_MODEL_FROM_FILE, ModelTarget, resolve as resolve_model
from backend.ratelimit import chat_limiter, client_key
from backend.session import Session, get_session_store

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

# The package's text for a host whose model writes no code (no execute_code, snippet
# contract or blocked_code; "Write code" is on its never-list), with the rule for the
# "Designer's selections" block: a session's prompt is fixed at its first turn and picks
# may come on any later one, so the rule is always in it.
INSTRUCTIONS = {"code": False, "mentions": True}

# This host's one line after the package's text, the last of its Notes: where a tool that
# does not exist yet is made. The link is the workbench page's path as a relative Markdown
# link, which the chat renders as a link.
WORKBENCH_LINE = (
    "- When no tool fits, describe in one sentence the tool the designer would need and point "
    f"them to the workbench with exactly this Markdown link: {WORKBENCH_LINK} - a new tool is "
    "tried and saved there, then it is in list_tools."
)

# bridge: false - the comparison baseline: no bridge instructions, no tools, no skills.
BASE_PROMPT = (
    "You are an assistant to an architect who works in Autodesk Revit. Answer their "
    "questions and requests as helpfully as you can, in their language."
)

# The role of the one loop (LLM_SINGLE_MODEL=1, and bridge: false): the planner's.
ROLE = roles.PLANNER
# The roles' texts: the package's, for a host whose model writes no code.
ROLE_INSTRUCTIONS = {"code": False}
# Seconds the analyst's digest may take to read (a snapshot); past that it is "not available".
DIGEST_TIMEOUT = 15.0
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


class ChatMention(BaseModel):
    """A model item the designer picked in the message (``@level``, ``@type``, ``@link``), as
    ``GET /api/v1/bridge/mentions`` listed it. Only ``kind`` and ``id`` are checked and used:
    the model gets the item as the package reads it from Revit now, never the page's
    ``label``."""
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=32, description="level, type or link")
    id: StrictInt = Field(description="the item's element id")
    label: str | None = Field(default=None, max_length=500, description="what the page showed; not sent to the model")


class ChatRequest(BaseModel):
    message: str | None = Field(default=None, max_length=8000)
    execution: dict | None = None      # an ExecutionResult the page got from /run or /execute
    session_id: str | None = None
    bridge: bool = True
    mentions: list[ChatMention] | None = Field(
        default=None, max_length=MAX_MENTIONS,
        description="The designer's picks in this message (with message and the bridge only), checked "
                    "against the open model before the model is called: the event mentions reports "
                    "each, and the verified ones reach the model as the package's selections block.")


# -- per-IP rate limit (protects a shared server-side key) ---------------------

def rate_limit(request: Request) -> None:
    if not chat_limiter.allow(client_key(request), get_settings().chat_rate_limit):
        raise HTTPException(429, "Too many requests")


def build_system_prompt(bridge: bool = True) -> str:
    """The package's host instructions without code and with the selections rule, then
    ``WORKBENCH_LINE``; the base prompt without the bridge. No skills, no lessons."""
    if not bridge:
        return BASE_PROMPT
    return host_instructions(**INSTRUCTIONS).rstrip("\n") + "\n" + WORKBENCH_LINE


def build_role_prompt(role: str) -> str:
    """A role's system prompt: the package's text for it and at most one host line - the
    planner's notes end with ``WORKBENCH_LINE``, the analyst's text with
    ``roles.ANALYST_WORKBENCH_LINE`` as a paragraph of its own; the supervisor's as it is."""
    text = host_instructions(role=role, **ROLE_INSTRUCTIONS)
    if role == roles.PLANNER:
        return text.rstrip("\n") + "\n" + WORKBENCH_LINE
    if role == roles.ANALYST:
        return text.rstrip("\n") + "\n\n" + roles.ANALYST_WORKBENCH_LINE
    return text


async def model_digest(session: Session) -> str:
    """The analyst's digest of the open model: the package's ``snapshot_digest`` of a snapshot
    the host takes (no model call). Kept with the session and read again only when the page
    has reported an execution since (``Session.digest_stale``) - a new task on the page is a
    new session. When the model cannot be read (no add-in, no answer within
    ``DIGEST_TIMEOUT`` seconds) the analyst is told so, and the next turn tries again."""
    if session.digest is not None and not session.digest_stale:
        return session.digest

    async def read():
        return await snapshot_now(await get_revit_client())

    try:
        snapshot = await asyncio.wait_for(read(), DIGEST_TIMEOUT)
    except TimeoutError:                          # before OSError: TimeoutError is one
        return "not available (timeout)"
    except ApiError as exc:
        return f"not available ({exc.payload.get('error') or 'revit_unreachable'})"
    except OSError:
        return "not available (revit_unreachable)"
    except Exception:  # noqa: BLE001 - the analyst still routes, without the digest
        _log.error("reading the digest failed", exc_info=True)
        return "not available (snapshot_failed)"
    session.digest = snapshot_digest(snapshot, roles.DIGEST_MAX_CHARS)
    session.digest_stale = False
    return session.digest


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


# The selections block goes to the model in English: the system prompt is the package's English
# text, and its rule names the block by its English title ("Designer's selections").
MENTION_BLOCK_LANGUAGE = "en"


def unresolved(wanted: list[dict], reason: str) -> dict:
    """The ``mentions`` event when the model could not be asked: every pick, with why."""
    return {"resolved": [], "not_found": [], "invalid": [],
            "unresolved": [{"kind": m["kind"], "id": m["id"], "reason": reason} for m in wanted]}


# Seconds the picks of one message may take to check; past that the turn runs without them.
MENTION_CHECK_TIMEOUT = 15.0


async def check_mentions(mentions: list[ChatMention]) -> dict:
    """The designer's picks against the open model (the package's ``resolve_mentions``), as the
    ``mentions`` event carries them: ``{resolved, not_found, invalid, unresolved, warnings?}``.
    ``resolved``: the package's items; ``not_found``: no such item now, or a link that cannot be
    read (``{kind, id, reason}``, the package's reason); ``invalid``: not a kind and element id
    the package takes; ``unresolved``: the model could not be asked - no add-in
    (``revit_unreachable``), no answer within ``MENTION_CHECK_TIMEOUT`` seconds (``timeout``),
    an add-in that could not say (``revit_error``: never read as "not found"), or a failure
    here (``resolve_failed``)."""
    wanted = [{"kind": m.kind, "id": m.id} for m in mentions]

    async def resolve() -> dict:
        return await resolve_mentions(RevitQueryExecutor(await get_revit_client()), wanted)

    try:
        found = await asyncio.wait_for(resolve(), MENTION_CHECK_TIMEOUT)
    except TimeoutError:                          # before OSError: TimeoutError is one
        return unresolved(wanted, "timeout")
    except ApiError as exc:                       # 503: no add-in (or no device) to ask
        return unresolved(wanted, str(exc.payload.get("error") or "revit_unreachable"))
    except OSError:                               # the connection failed while asking
        return unresolved(wanted, "revit_unreachable")
    except RevitQueryError as exc:
        _log.warning("picks not checked: the add-in could not say (%s)", exc)
        return unresolved(wanted, "revit_error")
    except Exception:  # noqa: BLE001 - the turn still runs, without the picks
        _log.error("checking picks failed", exc_info=True)
        return unresolved(wanted, "resolve_failed")
    report = {"resolved": found["resolved"],
              "not_found": [e for e in found["not_found"] if e.get("reason") != "invalid"],
              "invalid": [e for e in found["not_found"] if e.get("reason") == "invalid"],
              "unresolved": []}
    if found.get("warnings"):
        report["warnings"] = list(found["warnings"])
    return report


def kept_tool_message(message: dict, keep: int = TOOL_RESULT_KEEP) -> dict:
    """The tool message as the session stores it: a snapshot or an element listing can
    run to hundreds of KB, and every later turn would carry it again. The turn that
    produced it still sends it in full; later turns see the head and a note."""
    content = message.get("content") or ""
    if len(content) <= keep:
        return message
    note = ELISION_NOTE.format(kept=keep, total=len(content))
    return {**message, "content": f"{content[:keep]}\n{note}"}


# -- one request: its model calls, events and llm_calls rows ---------------------------------

@dataclass
class LoopResult:
    """What a role's loop produced: the text of its last round."""
    text: str = ""


class Turn:
    """One ``/api/chat`` request: the session, its turn number, the run label, and the model
    calls made so far (``round`` numbers them across the roles of the turn, from 1). Every
    ``stage``, ``tool`` and ``usage`` event and every ``llm_calls`` row names the role and the
    model of the call."""

    def __init__(self, session: Session, turn: int, run_label: str | None, bridge: bool):
        self.session = session
        self.turn = turn
        self.run_label = run_label
        self.bridge = bridge
        self.calls_log = get_log_store()
        self.round = 0
        self.open: tuple[str, str] | None = None        # (role, model) of the stage now open

    def stage(self, role: str, model: str, status: str) -> str:
        self.open = (role, model) if status == "start" else None
        return format_sse_event("stage", {"role": role, "model": model, "status": status})

    def close(self) -> list[str]:
        """``stage end`` of the stage still open, if any: every way the turn ends sends it."""
        return [self.stage(*self.open, "end")] if self.open is not None else []

    def tool_event(self, role: str, event_id: str, name: str, args: dict, status: str, **extra) -> str:
        return format_sse_event("tool", {"id": event_id, "role": role, "name": name, "args": args,
                                         "status": status, **extra})

    async def usage(self, role: str, model: str, round_no: int, stats: CallStats, started: float) -> str:
        """The ``usage`` event of one model call; its ``llm_calls`` row is written first."""
        ms = int((time.monotonic() - started) * 1000)
        counts = stats.usage.as_dict()
        await asyncio.to_thread(self.calls_log.record_call, session_id=self.session.session_id, turn=self.turn,
                                round=round_no, role=role, model=model, usage=counts, ms=ms,
                                run_label=self.run_label)
        return format_sse_event("usage", {"role": role, "model": model, "round": round_no, **counts, "ms": ms})

    async def loop(self, role: str, target: ModelTarget, messages: list[dict], tools: list[dict] | None, *,
                   max_rounds: int, stream: bool, keep: Callable[[dict], None] | None,
                   offered: tuple[str, ...] | None, out: LoopResult) -> AsyncGenerator[str, None]:
        """A role's model calls until it answers in text: each call streamed (its text to the page
        only when ``stream``), each tool call run and fed back, at most ``max_rounds`` rounds with
        tools and one more without. ``keep`` stores what the session keeps (the assistant turns
        and the capped tool results); ``offered`` is the role's tool subset - a call of any other
        tool is answered ``tool_not_offered`` and never run (None: the one loop, every tool).
        The last round's text ends in ``out``. ``LLMError`` and a raising tool end the turn."""
        llm = target.llm
        for round_no in range(max_rounds + 1):
            # The last round goes out without tools so the model has to answer in text.
            round_tools = tools if round_no < max_rounds else None
            self.round += 1
            this_round = self.round
            parts: list[str] = []
            calls = None
            stats = CallStats()
            started = time.monotonic()
            try:
                async for item in stream_completion(llm, messages, tools=round_tools, stats=stats, **target.options):
                    if isinstance(item, str):
                        parts.append(item)
                        if stream:
                            yield format_sse(item)
                    else:
                        calls = item
            except LLMError:
                if stats.accepted:          # the endpoint took the call: it may have cost tokens
                    yield await self.usage(role, llm.model, this_round, stats, started)
                raise
            yield await self.usage(role, llm.model, this_round, stats, started)
            content = "".join(parts)
            out.text = content
            if calls and round_tools is None:
                # Nothing was offered, so nothing runs: the round ends with the text.
                _log.warning("not run: %d tool call(s) (%s) from a round without tools "
                             "(bridge=%s, session %s)", len(calls), ", ".join(c.name for c in calls),
                             self.bridge, self.session.session_id)
                if keep is not None:
                    keep({"role": "assistant", "content": content})
                break
            if not calls:
                if content and keep is not None:
                    keep(assistant_message(content))
                break
            assistant = assistant_message(content, calls)
            if keep is not None:
                keep(assistant)
            messages.append(assistant)
            for index, call in enumerate(calls, start=1):
                event_id = f"{self.turn}.{this_round}.{index}"     # unique in the session
                args = parse_arguments(call.arguments)
                shown_args = shown_arguments(call.arguments, args)
                yield self.tool_event(role, event_id, call.name, shown_args, "running")
                tool_started = time.monotonic()
                try:
                    if args is None:
                        outcome = ToolOutcome({"error": "invalid_args",
                                               "message": "arguments must be a JSON object"})
                    elif offered is not None and call.name not in offered:
                        # Outside the role's subset: refused, never run.
                        outcome = ToolOutcome({"error": "tool_not_offered", "tool": call.name,
                                               "message": f"the {role} has only: {', '.join(offered)}"})
                    else:
                        outcome = await call_tool(call.name, args, self.session)
                    reply = tool_message(call, outcome.result)
                except Exception as exc:
                    # The turn fails as before; the row still closes. The browser gets the
                    # exception's type only - its text (paths, internals) goes to the log.
                    _log.error("tool %s raised in session %s", call.name, self.session.session_id,
                               exc_info=True)
                    yield self.tool_event(role, event_id, call.name, shown_args, "error",
                                          ms=int((time.monotonic() - tool_started) * 1000),
                                          error=type(exc).__name__)
                    raise
                result_text = reply["content"]
                error = tool_error_text(outcome.result)
                closing = {"ms": int((time.monotonic() - tool_started) * 1000), "size": len(result_text),
                           "preview": result_text[:TOOL_PREVIEW]}
                if error is not None:
                    closing["error"] = error
                yield self.tool_event(role, event_id, call.name, shown_args, "error" if error else "ok", **closing)
                if outcome.spec_event is not None:
                    yield format_sse_event("spec", outcome.spec_event)
                # The session keeps at most the head; this turn sees all of it.
                if keep is not None:
                    keep(kept_tool_message(reply))
                messages.append(reply)

    async def answer_json(self, role: str, target: ModelTarget, messages: list[dict], parse, *,
                          parsed: list, tools: list[dict] | None = None, offered: tuple[str, ...] | None = None,
                          max_rounds: int = 0) -> AsyncGenerator[str, None]:
        """A role whose reply is one JSON object (the analyst, the supervisor): its loop, then
        ``parse`` on its text; on a ``RoleOutputError`` one repair call (its reply, then
        ``roles.REPAIR_INSTRUCTION``, no tools) and ``parse`` once more. ``parsed`` gets the
        result, or None when the repaired reply could not be read either. Never more calls."""
        out = LoopResult()
        async for frame in self.loop(role, target, messages, tools, max_rounds=max_rounds, stream=False,
                                     keep=None, offered=offered, out=out):
            yield frame
        try:
            parsed.append(parse(out.text))
            return
        except roles.RoleOutputError as exc:
            _log.info("%s reply not readable (%s): one repair call (session %s)", role, exc,
                      self.session.session_id)
        messages.append({"role": "assistant", "content": out.text})
        messages.append({"role": "user", "content": roles.REPAIR_INSTRUCTION})
        again = LoopResult()
        async for frame in self.loop(role, target, messages, None, max_rounds=0, stream=False,
                                     keep=None, offered=offered, out=again):
            yield frame
        try:
            parsed.append(parse(again.text))
        except roles.RoleOutputError as exc:
            _log.warning("%s reply not readable after the repair (%s): the host falls back (session %s)",
                         role, exc, self.session.session_id)
            parsed.append(None)

    async def planner(self, target: ModelTarget, route: str, out: LoopResult) -> AsyncGenerator[str, None]:
        """The planner's segment: the session's conversation (the caller added its user message)
        under the planner's prompt, with the route's tools."""
        session = self.session
        messages = [{"role": "system", "content": session.role_prompt(roles.PLANNER, build_role_prompt)}]
        messages.extend(session.window(HISTORY_WINDOW))
        yield self.stage(roles.PLANNER, target.model, "start")
        async for frame in self.loop(roles.PLANNER, target, messages, roles.ROUTE_DEFINITIONS[route],
                                     max_rounds=MAX_TOOL_ROUNDS, stream=True, keep=session.add,
                                     offered=roles.ROUTE_TOOLS[route], out=out):
            yield frame
        yield self.stage(roles.PLANNER, target.model, "end")


async def message_roles(run: Turn, targets: dict[str, ModelTarget], text: str) -> AsyncGenerator[str, None]:
    """A message with the roles: the analyst routes it; chat and none end with its note, query
    and tool go on to the planner, the routing after the message."""
    session = run.session
    analyst = targets[roles.ANALYST]
    yield run.stage(roles.ANALYST, analyst.model, "start")
    packs = await asyncio.to_thread(list_packs)
    names = [p["name"] for p in packs if isinstance(p, dict) and isinstance(p.get("name"), str)]
    digest = await model_digest(session)
    messages = [{"role": "system", "content": session.role_prompt(roles.ANALYST, build_role_prompt)},
                {"role": "user", "content": roles.analyst_message(roles.tool_lines(packs), digest,
                                                                   session.role_task, text)}]
    parsed: list = []
    async for frame in run.answer_json(roles.ANALYST, analyst, messages,
                                       lambda reply: roles.parse_routing(reply, names), parsed=parsed):
        yield frame
    routing = parsed[0] or roles.FALLBACK_ROUTING
    yield run.stage(roles.ANALYST, analyst.model, "end")
    if routing.route in roles.ANSWERED_BY_ANALYST:
        yield format_sse(roles.analyst_reply(routing))
        return
    task = roles.next_task(session.role_task, routing)
    session.role_task = task
    session.add({"role": "user", "content": roles.planner_message(text, routing, session.pending_verdict)})
    session.pending_verdict = None
    out = LoopResult()
    async for frame in run.planner(targets[roles.PLANNER], routing.route, out):
        yield frame
    task.last = out.text


async def execution_roles(run: Turn, targets: dict[str, ModelTarget], execution: dict) -> AsyncGenerator[str, None]:
    """An execution report with the roles: the supervisor's verdict, held to the package's;
    retry runs the planner again with the supervisor's fix."""
    session = run.session
    session.digest_stale = True                   # the open model may have changed
    task = session.role_task or roles.RoleTask(route=roles.TOOL)
    session.role_task = task
    supervisor = targets[roles.SUPERVISOR]
    spec = session.confirmed_spec if session.confirmed_spec is not None else session.spec
    yield run.stage(roles.SUPERVISOR, supervisor.model, "start")
    messages = [{"role": "system", "content": session.role_prompt(roles.SUPERVISOR, build_role_prompt)},
                {"role": "user", "content": roles.supervisor_message(spec, execution, task.retries)}]
    parsed: list = []
    async for frame in run.answer_json(roles.SUPERVISOR, supervisor, messages, roles.parse_verdict, parsed=parsed,
                                       tools=roles.SUPERVISOR_DEFINITIONS, offered=roles.SUPERVISOR_TOOLS,
                                       max_rounds=roles.SUPERVISOR_TOOL_ROUNDS):
        yield frame
    verdict = roles.enforce(parsed[0] or roles.fallback_verdict(execution), execution, task.retries)
    if verdict.forced:
        _log.info("the supervisor's verdict became %s (%s) in session %s", verdict.verdict, verdict.forced,
                  session.session_id)
    yield run.stage(roles.SUPERVISOR, supervisor.model, "end")
    reply = verdict.reply()
    if verdict.verdict != roles.RETRY:
        yield format_sse(reply)
        session.pending_verdict = verdict.note()
        if verdict.verdict == roles.DONE:
            session.role_task = None
        else:
            task.last = reply
        return
    yield format_sse(reply + "\n\n")
    task.retries += 1
    task.route = roles.TOOL
    session.add({"role": "user", "content": roles.retry_message(verdict, task.retries)})
    out = LoopResult()
    async for frame in run.planner(targets[roles.PLANNER], roles.TOOL, out):
        yield frame
    task.last = f"{reply}\n\n{out.text}".strip()


def no_model(target: ModelTarget) -> HTTPException:
    return HTTPException(400, NO_MODEL if target.entry is None else NO_MODEL_FROM_FILE)


@router.post("/chat", dependencies=[Depends(rate_limit)])
async def chat(req: ChatRequest, request: Request,
               x_run_label: str | None = Header(None, description=RUN_LABEL_DOC)):
    if (req.message is None) == (req.execution is None):
        raise ApiError(422, "invalid_args", "send exactly one of message and execution")
    if req.message is not None and not req.message.strip():
        raise ApiError(422, "invalid_args", "message is empty")
    if req.execution is not None and not req.bridge:
        raise ApiError(422, "invalid_args", "execution feedback has no meaning without the bridge")
    if req.mentions and req.execution is not None:
        raise ApiError(422, "invalid_args", "mentions go with a message, not with an execution report")
    if req.mentions and not req.bridge:
        raise ApiError(422, "invalid_args", "mentions have no meaning without the bridge")
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
        raise no_model(target)
    # The roles with the bridge, unless LLM_SINGLE_MODEL=1; without the bridge the one loop.
    with_roles = req.bridge and not get_settings().single_model
    targets = {ROLE: target}
    if with_roles:
        # The role that opens this turn: the analyst (a message) or the supervisor (a report);
        # the planner may follow either.
        first = roles.ANALYST if req.execution is None else roles.SUPERVISOR
        try:
            targets[first] = await resolve_model(first, request.headers)
        except LLMError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        if not targets[first].llm.configured:
            raise no_model(targets[first])

    session = get_session_store().get_or_create(req.session_id)
    turn = session.next_turn()
    text = req.message if req.message is not None else execution_message(req.execution)
    picks = None
    if req.mentions:
        # Checked before any model is called, in the model of the device this request drives.
        # The verified picks stay with the session under that device's scope (a spec may bind
        # them in this turn or a later one), the ones found gone leave it; this turn's verified
        # picks - all of this scope - go to the model as the package's block after the
        # designer's words. Nothing else of the picks reaches it.
        scope = current_scope()
        picks = await check_mentions(req.mentions)
        session.remember_picks(scope, picks["resolved"])
        session.forget_picks(scope, picks["not_found"])
        block = mention_block(picks["resolved"], MENTION_BLOCK_LANGUAGE)
        if block:
            text = f"{text}\n\n{block}"
    if req.execution is not None and req.execution.get("evidence_id"):
        session.evidence_id = str(req.execution["evidence_id"])
    run = Turn(session, turn, run_label, req.bridge)

    if with_roles:
        async def generate() -> AsyncGenerator[str, None]:
            if req.execution is not None:
                yield format_sse_event("execution", req.execution)
            if picks is not None:
                yield format_sse_event("mentions", picks)
            segments = (message_roles(run, targets, text) if req.execution is None
                        else execution_roles(run, targets, req.execution))
            try:
                async for frame in segments:
                    yield frame
            except LLMError as exc:
                for frame in run.close():
                    yield frame
                yield format_sse_event("error", {"detail": str(exc)})
                return
            except Exception:
                # The turn breaks off as the one loop's does, the open stage closed first.
                for frame in run.close():
                    yield frame
                raise
            yield SSE_DONE
    else:
        session.add({"role": "user", "content": text})
        tools = TOOL_DEFINITIONS if req.bridge else None
        # The first turn's prompt for the whole session: the cached prefix stays the same.
        messages = [{"role": "system", "content": session.system_prompt(req.bridge, build_system_prompt)}]
        messages.extend(session.window(HISTORY_WINDOW))

        async def generate() -> AsyncGenerator[str, None]:
            if req.execution is not None:
                yield format_sse_event("execution", req.execution)
            if picks is not None:
                yield format_sse_event("mentions", picks)
            yield run.stage(ROLE, llm.model, "start")
            try:
                async for frame in run.loop(ROLE, target, messages, tools, max_rounds=MAX_TOOL_ROUNDS, stream=True,
                                            keep=session.add, offered=None, out=LoopResult()):
                    yield frame
            except LLMError as exc:
                yield run.stage(ROLE, llm.model, "end")
                yield format_sse_event("error", {"detail": str(exc)})
                return
            except Exception:
                # Anything else ends the turn as before (the response breaks off), but the stage
                # is closed first. A generator closed by a client that went away (GeneratorExit,
                # cancellation) can send nothing more: the page marks what stayed open.
                yield run.stage(ROLE, llm.model, "end")
                raise
            yield run.stage(ROLE, llm.model, "end")
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
