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

A multi-step task emits ``event: plan`` when the planner calls ``set_plan`` and when a
step changes state. The snapshot carries ``{plan_id, steps: [{id, title, status,
evidence_id?}]}``. A completed step may be followed by a new planner stage and
the next card in this same stream. Each card still requires a separate confirmation.

The designer's picks: a message may carry ``mentions`` (``[{kind, id, label?}]``,
at most 20; ``@level``, ``@type``, ``@link``, ``@element`` as ``GET /api/v1/bridge/mentions``
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
  last one (before ``error`` or ``done``); one pair per role segment of the turn. The
  turn's first one also has ``mode``, the chat mode the turn runs in.
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

The chat's mode (``mode`` in the body; absent or ``auto``: the host's default,
``WebSettings.chat_mode_default`` - ``roles``, ``single`` with ``LLM_SINGLE_MODEL=1``,
``roles_analyst`` with ``ROLES_ANALYST=on``): ``single`` is the one loop below,
``roles`` the planner and the supervisor, ``roles_analyst`` the analyst too. A local
host honours a request's mode; a cloud host only with the right ``X-Admin-Token``
(else the default runs); without the bridge the request runs as ``single``. The mode
run is a field of the turn's first ``stage`` event (``{role, model, status, mode}``)
and of every ``llm_calls`` row.

The roles (``backend.roles`` has the table, the hand-offs and the parsing). In mode
``roles_analyst`` a message with the bridge goes to the **analyst** first: no tools, one JSON
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
(at most ``roles.MAX_RETRIES`` times per task). In mode ``roles`` a message goes straight
to the planner with route ``tool``. A role's output that is not its JSON gets one repair
call, then the host's fallback. Mode ``single`` instead runs the one loop of the planner as
before the split, request for request and frame for frame (the first ``stage`` event has
the ``mode`` field).

Each role's model is ``backend.models_file.resolve(role, headers)``: ``LLM_*`` and the
``X-LLM-*`` headers as before, or, with a models file, the role's entry there (the
headers replace the planner's entry, and every role that shares it) with the entry's
provider options (the responses api, the output limit's name, the reasoning effort, the
read timeout: ``backend.llm``). What a provider returns with a tool call to have it back
(a thought signature, encrypted reasoning) is kept with the session
(``Session.provider_memory``). A reply that used up its output limit on thinking, with
no text and no tool call, ends the turn - for every role, with no repair call and no
fallback - with ``event: error`` whose detail is ``Turn.limit_notice``: the role and its
model, the limit as it was sent, what of the task is still there (the last card this turn
sent, the reported run - and, for the supervisor, that the planner has not been told of
the run) and how to raise it (``limit_advice``: an entry's ``max_tokens``, or the host's
default - ``LLM_MAX_TOKENS``, else ``backend.llm.DEFAULT_MAX_TOKENS`` - which is all a
browser-chosen model gets). Events
and ``llm_calls`` carry the role and the model the call actually went to; ``round``
counts the model calls of the turn across its roles.

Within one session each prompt is the one its first turn was sent with
(``Session.system_prompt``, ``Session.role_prompt``) and the tool definitions of a
role and route are a constant: the prefix a provider caches stays byte for byte the
same from call to call.

The system prompt of the one loop is the package's host instructions for a host
whose model writes no code, with the rule for the designer's selections
(``host_instructions(code=False, mentions=True)``), plus the host's ``WORKBENCH_LINE``.
The roles use the package's role texts directly. The planner adds ``WORKBENCH_LINE``
and the analyst adds ``roles.ANALYST_WORKBENCH_LINE``.
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
import unicodedata
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from revit_bridge import host_instructions
from revit_bridge.mentions import MAX_MENTIONS, mention_block, resolve_mentions
from revit_bridge.snapshot import RevitQueryExecutor, snapshot_digest
from revit_bridge.snapshot.query import RevitQueryError

from backend import roles
from backend.api.auto_run import AutoRun
from backend.api.bridge import current_scope, get_revit_client, list_packs, set_device_context, snapshot_now
from backend.api.errors import ApiError
from backend.api.model_tools import (
    TOOL_DEFINITIONS, WORKBENCH_LINK, ToolOutcome, call_tool, parse_arguments,
)
from backend.config import ROLES_ANALYST as MODE_ROLES_ANALYST, SINGLE as MODE_SINGLE, get_settings
from backend import llm as llm_client
from backend.llm import (
    SSE_DONE, CallStats, LLMError, OutputLimitError, assistant_message, format_sse, format_sse_event,
    stream_completion, tool_message,
)
from backend.log_store import get_client_ip, get_log_store, log_and_stream
from backend.models_file import NO_MODEL_FROM_FILE, ModelTarget, get_models, resolve as resolve_model
from backend.ratelimit import chat_limiter, client_key
from backend.session import Session, get_session_store
from backend.questions import DesignerAnswers, DesignerForm

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
    f"- When no tool fits, describe it with exactly this Markdown link: {WORKBENCH_LINK} to try and save it for list_tools. "
    "In form answers, other: true marks the designer's own words."
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
    """A model item the designer picked in the message (``@level``, ``@type``, ``@link``, ``@element``), as
    ``GET /api/v1/bridge/mentions`` listed it. Only ``kind`` and ``id`` are checked and used:
    the model gets the item as the package reads it from Revit now, never the page's
    ``label``."""
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=32, description="level, type, link or element")
    id: StrictInt = Field(description="the item's element id")
    document_id: str | None = Field(default=None, max_length=200, description="Document identity recorded by the element picker")
    label: str | None = Field(default=None, max_length=500, description="what the page showed; not sent to the model")


class ChatRequest(BaseModel):
    run: Literal["confirm", "auto"] = "confirm"
    answers: DesignerAnswers | None = None
    message: str | None = Field(default=None, max_length=8000)
    execution: dict | None = None      # an ExecutionResult the page got from /run or /execute
    session_id: str | None = None
    bridge: bool = True
    mentions: list[ChatMention] | None = Field(
        default=None, max_length=MAX_MENTIONS,
        description="The designer's picks in this message (with message and the bridge only), checked "
                    "against the open model before the model is called: the event mentions reports "
                    "each, and the verified ones reach the model as the package's selections block.")
    mode: Literal["auto", "single", "roles", "roles_analyst"] | None = Field(
        default=None,
        description="How the chat runs this request: single (one model loop), roles (planner + supervisor), "
                    "roles_analyst (analyst + planner + supervisor), or auto (absent or null too): the host's "
                    "default, /config.json features.chatModeDefault. A local host honours it; a cloud host "
                    "only with the right X-Admin-Token, else it runs the default. Without the bridge the "
                    "request runs as single. The mode run is on the turn's first stage event.")


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


async def model_digest(session: Session, scope: str) -> str:
    """The analyst's digest of the open model of the device ``scope``: the package's
    ``snapshot_digest`` of a snapshot the host takes (no model call). Kept with the session per
    scope and read again only after the page has reported an execution (which drops it) - a new
    task on the page is a new session; changes made in Revit outside the chat reach the analyst
    only then (the planner reads the model itself). When the model cannot be read (no add-in,
    no answer within ``DIGEST_TIMEOUT`` seconds) the analyst is told so, and the host does not
    try again for ``roles.DIGEST_RETRY_SECONDS``."""
    if scope in session.digests:
        return session.digests[scope]
    failed = session.digest_failures.get(scope)
    if failed is not None and time.monotonic() - failed[0] < roles.DIGEST_RETRY_SECONDS:
        return failed[1]

    async def read():
        return await snapshot_now(await get_revit_client())

    try:
        snapshot = await asyncio.wait_for(read(), DIGEST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - the analyst still routes, without the digest
        if isinstance(exc, TimeoutError):                  # before OSError: TimeoutError is one
            why = "timeout"
        elif isinstance(exc, ApiError):
            why = str(exc.payload.get("error") or "revit_unreachable")
        elif isinstance(exc, OSError):
            why = "revit_unreachable"
        else:
            _log.error("reading the digest failed", exc_info=True)
            why = "snapshot_failed"
        text = f"not available ({why})"
        session.digest_failures[scope] = (time.monotonic(), text)
        return text
    session.digest_failures.pop(scope, None)
    session.digests[scope] = snapshot_digest(snapshot, roles.DIGEST_MAX_CHARS)
    return session.digests[scope]


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
    documents: dict[int, set[str]] = {}
    for pick in mentions:
        if pick.kind == "element" and pick.document_id is not None:
            documents.setdefault(pick.id, set()).add(pick.document_id)
    matched = []
    for item in report["resolved"]:
        expected = documents.get(item["id"]) if item["kind"] == "element" else None
        if expected and expected != {item.get("document_id")}:
            report["not_found"].append({"kind": "element", "id": item["id"], "reason": "document_changed"})
        else:
            matched.append(item)
    report["resolved"] = matched
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


def limit_advice(target: ModelTarget) -> str:
    """How the output limit of ``target``'s calls can be raised - what is true for where the call
    went: an entry's own ``max_tokens``; an entry without one, or no models file, takes the host's
    default (``LLM_MAX_TOKENS``, else ``backend.llm.DEFAULT_MAX_TOKENS``); a model the browser's
    settings chose gets no entry option, only the host's default."""
    settings = get_settings()
    default = settings.llm_max_tokens or llm_client.DEFAULT_MAX_TOKENS
    if target.browser_model:
        return (f"This model was chosen by the browser's model settings, so the host's default output limit "
                f"({default}) applies to it, and only the host can raise it: with LLM_MAX_TOKENS, or with "
                f"max_tokens in an entry of the host's models file for this model, used without the browser's "
                f"model settings.")
    if target.entry is not None and target.entry_max_tokens:
        return "To raise it, set a larger max_tokens in this model's entry in the host's models file."
    if target.entry is not None:
        return (f"To raise it, set max_tokens in this model's entry in the host's models file (it sets none, so "
                f"the host's default, {default}, applies), or LLM_MAX_TOKENS on the host for every entry "
                f"without one.")
    return (f"To raise it, set LLM_MAX_TOKENS on the host (the default now is {default}), or give this model an "
            f"entry with max_tokens in the host's models file.")


class Turn:
    """One ``/api/chat`` request: the session, its turn number, the run label, the chat mode it
    runs in, and the model calls made so far (``round`` numbers them across the roles of the
    turn, from 1). Every ``stage``, ``tool`` and ``usage`` event and every ``llm_calls`` row
    names the role and the model of the call; the turn's first ``stage`` event and every row
    name the mode too."""

    def __init__(self, session: Session, turn: int, run_label: str | None, bridge: bool, mode: str = MODE_SINGLE,
                 execution: dict | None = None):
        self.session = session
        self.turn = turn
        self.run_label = run_label
        self.bridge = bridge
        self.mode = mode
        self.routing = roles.FALLBACK_ROUTING
        self.mode_sent = False
        self.execution = execution                      # the run this turn reports, if it reports one
        # The last spec card this turn sent the page: "ready" (accepted: it can be confirmed),
        # "not_ready" (errors, or reconcile not ready), None (no spec event in this turn).
        self.card: str | None = None
        self.auto: AutoRun | None = None
        self.question_tools = False
        self.calls_log = get_log_store()
        self.round = 0
        self.open: tuple[str, str] | None = None        # (role, model) of the stage now open
        self.ran: dict[str, str] = {}                   # role -> model, in the order the roles ran

    def limit_notice(self, role: str, target: ModelTarget, exc: OutputLimitError) -> str:
        """The ``error`` event's text when ``role``'s reply used up its output limit: who and which
        model, the limit as it was sent, what of the task is still there (``limit_state``) and how
        the limit can be raised (``limit_advice``). The turn ends there: no repair call, no
        fallback."""
        head = (f"Output limit used up: the {role} ({target.model}) spent its whole output limit ({exc.param} "
                f"{exc.limit}) before it wrote a reply or called a tool - most likely on thinking, which "
                f"counts toward the limit.")
        return f"{head} {self.limit_state(role)} {limit_advice(target)}"

    def limit_state(self, role: str) -> str:
        """What the designer still has after a used-up limit, read from what this turn did: the last
        card it sent (ready to confirm or not), the run it reports; the page keeps both as they are."""
        cards = {"ready": "The spec card proposed in this turn is kept: it can still be confirmed and run; only "
                          "the text after it is missing.",
                 "not_ready": "The last spec card proposed in this turn cannot be confirmed as it stands (it has "
                              "errors, or its reconcile is not ready)."}
        card = cards.get(self.card or "", "")
        if self.execution is not None:
            evidence = self.execution.get("evidence_id")
            where = f" (evidence {evidence})" if isinstance(evidence, str) and evidence else ""
            run = f"The run you reported and its result{where} stay as the page received them."
            if role == roles.SUPERVISOR:
                # execution_roles: the confirmed spec is used up, no verdict is pending and the run
                # is not in the planner's history - nothing of it reaches the planner.
                return (f"{run} The supervisor's report on it is missing, and the planner has not been told "
                        f"of this run: say what it did in your next message.")
            if roles.SUPERVISOR in self.ran:
                # The supervisor sent the task back (its report is above) and the planner ran again.
                new = {"ready": "the planner's new card is kept: it can still be confirmed and run.",
                       "not_ready": "the planner proposed a new card, but it cannot be confirmed as it stands (it "
                                    "has errors, or its reconcile is not ready)."}
                return f"{run} The supervisor's report is above; {new.get(self.card or '', 'the planner proposed no new card.')}"
            return f"{run} Only the report on it is missing.{' ' + card if card else ''}"
        if card:
            return card
        earlier = " Cards and results from earlier turns stay as they were." if self.session.spec else ""
        if role == roles.ANALYST:
            return (f"Your message was not passed on to the planner and nothing was proposed; send it again "
                    f"once the limit is raised.{earlier}")
        return f"No spec card was proposed in this turn.{earlier}"

    def stage(self, role: str, model: str, status: str) -> str:
        self.open = (role, model) if status == "start" else None
        if status == "start":
            self.ran.pop(role, None)
            self.ran[role] = model
        payload = {"role": role, "model": model, "status": status}
        if not self.mode_sent:
            # The turn's first event with run metadata carries the mode it runs in.
            payload["mode"] = self.mode
            self.mode_sent = True
        return format_sse_event("stage", payload)

    def models_ran(self) -> str | None:
        """The interaction log's model of a turn with the roles: each role that ran and its
        model (``analyst m1, planner m2``); None when none ran."""
        return ", ".join(f"{role} {model}" for role, model in self.ran.items()) or None

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
        plan = self.session.plan if self.bridge else None
        step = plan.current if plan else None
        await asyncio.to_thread(self.calls_log.record_call, session_id=self.session.session_id, turn=self.turn,
                                round=round_no, role=role, model=model, usage=counts, ms=ms,
                                run_label=self.run_label, mode=self.mode,
                                plan_id=plan.id if step else None, step_id=step.id if step else None)
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
                context = messages
                plan = self.session.plan if self.bridge else None
                if plan and plan.current:
                    context = list(messages)
                    index = next(i for i in range(len(context) - 1, -1, -1) if context[i]["role"] == "user")
                    data = json.dumps(plan.context(), ensure_ascii=False)
                    if has_host_marker(data):
                        data = quote_marked_json(plan.context())
                    instruction = ("Judge only the current step. If it completes the final unfinished step, "
                                   "summarize every step in the final report." if role == roles.SUPERVISOR else
                                   "Use the original request, verified form answers and previous results throughout the plan.")
                    context[index] = {**context[index], "content": context[index]["content"] +
                                      f"\n\nPlan {roles.FROM_HOST}: {instruction}\n{data}"}
                if self.auto and role in (roles.PLANNER, roles.SUPERVISOR, ROLE):
                    context = [dict(m) for m in context]
                    context[0]["content"] += ("\nThe host automatically runs eligible ready cards. Do not ask for a click "
                                              "unless the host requires manual confirmation. Ask whenever information is missing.")
                    if role != roles.SUPERVISOR:
                        context[0]["content"] += (
                            " For a value from a read in this message or a completed step in this plan, cite "
                            "result:<evidence_id>#<path> (e.g. result:ev_123#Items.0.Mid.X). Use the exact leaf "
                            "value and source tool. Designer and answer need the designer's words or form answer.")
                async for item in stream_completion(llm, context, tools=round_tools, stats=stats,
                                                    memory=self.session.provider_memory, **target.options):
                    if isinstance(item, str):
                        parts.append(item)
                        if stream:
                            yield format_sse(item)
                    else:
                        calls = item
            except LLMError as exc:
                if stats.accepted:          # the endpoint took the call: it may have cost tokens
                    yield await self.usage(role, llm.model, this_round, stats, started)
                if isinstance(exc, OutputLimitError):
                    # The turn ends with the designer's notice: no repair call, no fallback.
                    raise LLMError(self.limit_notice(role, target, exc), status=exc.status) from None
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
                    if self.bridge and role == roles.PLANNER and not (self.session.plan and self.session.plan.current):
                        # A text clarification can precede the plan, including one grounded in
                        # reconcile.questions. Preserve the brief until its designer answers.
                        open_task = self.mode != MODE_SINGLE and self.routing.route == roles.TOOL
                        if self.card != "ready" and (open_task or self.question_tools or "?" in content or "？" in content):
                            self.session.clarification = content
                break
            assistant = assistant_message(content, calls)
            if keep is not None:
                keep(assistant)
            messages.append(assistant)
            questions = None
            stop_reply = None
            for index, call in enumerate(calls, start=1):
                event_id = f"{self.turn}.{this_round}.{index}"     # unique in the session
                args = parse_arguments(call.arguments)
                shown_args = shown_arguments(call.arguments, args)
                yield self.tool_event(role, event_id, call.name, shown_args, "running")
                tool_started = time.monotonic()
                try:
                    if questions is not None:
                        outcome = ToolOutcome({"error": "awaiting_designer"})
                    elif args is None:
                        outcome = ToolOutcome({"error": "invalid_args",
                                               "message": "arguments must be a JSON object"})
                    elif offered is not None and call.name not in offered:
                        # Outside the role's subset: refused, never run.
                        outcome = ToolOutcome({"error": "tool_not_offered", "tool": call.name,
                                               "message": f"the {role} has only: {', '.join(offered)}"})
                    else:
                        outcome = await call_tool(call.name, args, self.session)
                    auto_card = bool(self.auto and outcome.spec_event is not None)
                    if auto_card:
                        reason, detail = self.auto.inspect(outcome.spec_event)
                        if reason == "apply_approval":
                            updated = {**args, "spec": {**args["spec"], "interpretations": detail}}
                            outcome = await call_tool(call.name, updated, self.session)
                            reason, detail = self.auto.inspect(outcome.spec_event) if outcome.spec_event else ("not_ready", None)
                        if outcome.spec_event:
                            outcome.spec_event["run"] = "confirm" if reason else "auto"
                            if reason:
                                outcome.spec_event["run_reason"] = reason
                            if reason == "unconfirmed_interpretation" and detail:
                                outcome.questions_event = detail
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
                if call.name == "reconcile" and isinstance(outcome.result, dict) and outcome.result.get("questions"):
                    self.question_tools = True
                if call.name == "missing_params" and isinstance(outcome.result, list) and outcome.result:
                    self.question_tools = True
                error = tool_error_text(outcome.result)
                closing = {"ms": int((time.monotonic() - tool_started) * 1000), "size": len(result_text),
                           "preview": result_text[:TOOL_PREVIEW]}
                if outcome.read_tool is not None:
                    closing["read"] = {"name": outcome.read_tool, "result": outcome.result}
                if error is not None:
                    closing["error"] = error
                yield self.tool_event(role, event_id, call.name, shown_args, "error" if error else "ok", **closing)
                if outcome.plan_event is not None:
                    yield format_sse_event("plan", outcome.plan_event)
                if outcome.spec_event is not None:
                    # The page shows this card instead of any earlier one: the turn's card is the last.
                    # A refusal that sends no card (no spec event) leaves it as it was.
                    accepted = isinstance(outcome.result, dict) and outcome.result.get("accepted") is True
                    self.card = "ready" if accepted else "not_ready"
                    yield format_sse_event("spec", outcome.spec_event)
                # The session keeps at most the head; this turn sees all of it.
                if keep is not None:
                    keep(kept_tool_message(reply))
                messages.append(reply)
                if outcome.questions_event is not None:
                    questions = outcome.questions_event
                    stop_reply = outcome.stop_reply
                if auto_card:
                    # Close every call ID in this assistant message before handing off.
                    for remaining in calls[index:]:
                        skipped = tool_message(remaining, {"error": "card_pending"})
                        messages.append(skipped)
                        if keep is not None:
                            keep(kept_tool_message(skipped))
                    if outcome.spec_event and outcome.spec_event.get("run") == "auto":
                        if await self.auto.request.is_disconnected():
                            return
                        worker = self.auto.start(outcome.spec_event["spec"])
                        if self.run_label is None:
                            self.run_label = f"auto-{self.session.session_id}-{self.turn}"
                        await asyncio.to_thread(self.calls_log.mark_auto, self.session.session_id, self.turn, self.run_label)
                        self.auto.receipt = await asyncio.shield(worker)
                        yield format_sse_event("execution", self.auto.receipt)
                    break
            if questions is not None:
                self.session.pending_form = questions
                self.session.form_routing = self.routing
                self.session.form_stop_reply = stop_reply
                yield format_sse_event("questions", questions)
                return
            if auto_card:
                return

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
        under the planner's prompt, with the route's tools - or all planner tools on every route with
        ``ROLES_PLANNER_TOOLS=all``, every one of them offered and run."""
        session = self.session
        messages = [{"role": "system", "content": session.role_prompt(roles.PLANNER, build_role_prompt)}]
        messages.extend(session.window(HISTORY_WINDOW))
        if get_settings().roles_planner_tools == "all":
            route = roles.TOOL                    # the tool route offers the full planner tool list
        yield self.stage(roles.PLANNER, target.model, "start")
        async for frame in self.loop(roles.PLANNER, target, messages, roles.ROUTE_DEFINITIONS[route],
                                     max_rounds=MAX_TOOL_ROUNDS, stream=True, keep=session.add,
                                     offered=roles.ROUTE_TOOLS[route], out=out):
            yield frame
        yield self.stage(roles.PLANNER, target.model, "end")


async def message_roles(run: Turn, targets: dict[str, ModelTarget], text: str, scope: str,
                        *, form_routing: roles.Routing | None = None) -> AsyncGenerator[str, None]:
    """A message with the roles: in mode ``roles_analyst`` the analyst routes it (``scope``: the
    device the request drives, whose digest it reads); chat and none end with its note, query and
    tool go on to the planner, the routing after the message. In mode ``roles`` the message goes
    straight to the planner with route tool."""
    session = run.session
    if form_routing is not None:
        routing = form_routing
    elif run.mode == MODE_ROLES_ANALYST:
        analyst = targets[roles.ANALYST]
        yield run.stage(roles.ANALYST, analyst.model, "start")
        packs = await asyncio.to_thread(list_packs)
        names = [p["name"] for p in packs if isinstance(p, dict) and isinstance(p.get("name"), str)]
        digest = await model_digest(session, scope)
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
    else:
        routing = roles.FALLBACK_ROUTING
    run.routing = routing
    # Only a tool routing opens or continues the open task (roles.next_task).
    task = roles.next_task(session.role_task, routing) if routing.route == roles.TOOL else None
    if task is not None:
        session.role_task = task
    session.add({"role": "user", "content": roles.planner_message(text, routing, session.pending_verdict)})
    session.pending_verdict = None
    out = LoopResult()
    async for frame in run.planner(targets[roles.PLANNER], routing.route, out):
        yield frame
    if task is not None:
        task.last = out.text


async def execution_roles(run: Turn, targets: dict[str, ModelTarget], execution: dict,
                          scope: str, *, chinese: bool) -> AsyncGenerator[str, None]:
    """An execution report with the roles: the supervisor's verdict, held to the package's
    (``roles.enforce`` reads the raw result; the supervisor and the session get capped
    copies); retry runs the planner again with the supervisor's fix."""
    session = run.session
    # The open model may have changed: the analyst reads this device's model again.
    session.digests.pop(scope, None)
    session.digest_failures.pop(scope, None)
    task = session.role_task or roles.RoleTask(route=roles.TOOL)
    session.role_task = task
    plan = session.plan
    step = plan.current if plan else None
    if step:
        if execution.get("success") is not True or roles.validator_failed(execution) or roles.is_refused(execution):
            session.auto_paused = True
        task.retries = step.retries
        step.evidence_id = execution.get("evidence_id")
        step.result = roles.step_result(execution)
        step.recorded_result = execution.get("result")
    supervisor = targets[roles.SUPERVISOR]
    # The spec the page confirmed with this session, once: a report without one (the page did
    # not say, or the bench's fixed result) shows the last proposal as proposed, never confirmed.
    confirmed = session.confirmed_spec is not None
    spec = session.confirmed_spec if confirmed else session.spec
    if not step and session.pending_auto_receipt is None:
        session.confirmed_spec = None
    yield run.stage(roles.SUPERVISOR, supervisor.model, "start")
    messages = [{"role": "system", "content": session.role_prompt(roles.SUPERVISOR, build_role_prompt)},
                {"role": "user", "content": roles.supervisor_message(spec, execution, task.retries,
                                                                     confirmed=confirmed)}]
    parsed: list = []
    async for frame in run.answer_json(roles.SUPERVISOR, supervisor, messages, roles.parse_verdict, parsed=parsed,
                                       tools=roles.SUPERVISOR_DEFINITIONS, offered=roles.SUPERVISOR_TOOLS,
                                       max_rounds=roles.SUPERVISOR_TOOL_ROUNDS):
        yield frame
    verdict = roles.enforce(parsed[0] or roles.fallback_verdict(execution, chinese=chinese),
                            execution, task.retries, chinese=chinese)
    if step:
        if isinstance(step.evidence_id, str):
            session.reported_evidence.add(step.evidence_id)
        plan.confirmed = False
        plan.reporting = False
        plan.proposed = None
        plan.token_digest = None
        plan.receipt = None
        plan.auto_receipt_pending = False
        session.confirmed_spec = None
        session.spec = None
        failed = execution.get("success") is not True or roles.validator_failed(execution) or roles.is_refused(execution)
        step.status = {roles.DONE: "done", roles.RETRY: "pending", roles.ASK: "failed" if failed else "pending"}[verdict.verdict]
    if verdict.forced:
        _log.info("the supervisor's verdict became %s (%s) in session %s", verdict.verdict, verdict.forced,
                  session.session_id)
    yield run.stage(roles.SUPERVISOR, supervisor.model, "end")
    if step:
        yield format_sse_event("plan", plan.event())
    reply = verdict.reply()
    if verdict.verdict != roles.RETRY:
        yield format_sse(verdict.report)
        session.pending_verdict = verdict.note()
        if verdict.verdict == roles.DONE:
            session.role_task = None
            if step and plan.current:
                session.pending_verdict = None
                session.role_task = roles.RoleTask(route=roles.TOOL)
                session.add_message("user", f"Continue the plan with step {plan.current.id} {roles.FROM_HOST}.")
                out = LoopResult()
                async for frame in run.planner(targets[roles.PLANNER], roles.TOOL, out):
                    yield frame
                session.role_task.last = out.text
        else:
            task.last = reply
            question = verdict.question or ("接下来要怎么做？" if chinese else "How would you like to proceed?")
            options = [{"value": "revise", "label": "调整任务要求" if chinese else "Revise the task"},
                       {"value": "stop", "label": "结束此任务" if chinese else "End this task"}]
            if step:
                options.insert(0, {"value": "continue", "label": "跳过这一步并继续" if chinese else "Skip this step and continue"})
            form = DesignerForm(questions=[{"id": "next_step", "text": question[:500],
                                           "allow_other": True, "options": options}])
            session.pending_form = form.event()
            session.form_routing = roles.FALLBACK_ROUTING
            session.form_stop_reply = "此任务已结束。" if chinese else "This task has ended."
            yield format_sse_event("questions", session.pending_form)
        return
    yield format_sse(reply + "\n\n")
    task.retries += 1
    if step:
        step.retries = task.retries
    task.route = roles.TOOL
    session.pending_verdict = None                # the planner gets this verdict now, as the retry
    session.add({"role": "user", "content": roles.retry_message(verdict, task.retries)})
    out = LoopResult()
    async for frame in run.planner(targets[roles.PLANNER], roles.TOOL, out):
        yield frame
    task.last = f"{reply}\n\n{out.text}".strip()


def has_host_marker(text: str) -> bool:
    # Compare the visible letters and numbers. Whitespace, punctuation and Unicode
    # presentation characters are not meaningful distinctions in a host marker.
    def letters(value: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFKD", value).casefold() if c.isalnum())
    visible = letters(text)
    return any(letters(marker) in visible for marker in (roles.FROM_HOST, roles.FORM_MARKER))


def quote_marked_json(value: object) -> str:
    """Keep a marker in designer data inside one quoted JSON value, including lookalikes."""
    return json.dumps(value, ensure_ascii=True).replace("(", r"\u0028")


def selection_block(items: list[dict]) -> str:
    """Keep marked model names as JSON string literals, including normalized lookalikes."""
    def escape_marked(match: re.Match) -> str:
        literal = match.group()
        name = json.loads(literal)
        if not has_host_marker(name):
            return literal
        return quote_marked_json(name).replace("h", r"\u0068").replace("H", r"\u0048")

    return re.sub(r'"(?:\\.|[^"\\])*"', escape_marked, mention_block(items, MENTION_BLOCK_LANGUAGE))


def prefers_chinese(accept_language: str) -> bool:
    """Negotiate the two sets of host labels using the browser's language preferences."""
    best = 0.0
    preferred = "en"
    for entry in accept_language.lower().split(","):
        tag, _, weight = entry.strip().partition(";")
        lang = tag.split("-")[0]
        if lang not in ("zh", "en"):
            continue
        try:
            quality = float(weight.strip().removeprefix("q=")) if weight else 1.0
        except ValueError:
            continue
        if best < quality <= 1:
            best, preferred = quality, lang
    return preferred == "zh"


def no_model(target: ModelTarget, role: str) -> HTTPException:
    """400 for a role without a model. Without a models file every role is ``LLM_*`` and the
    browser's settings; with one, the browser's settings replace the planner's entry only, so a
    role on another entry needs the key variable its entry names, set on the server."""
    if target.entry is None:
        return HTTPException(400, NO_MODEL)
    models = get_models()
    if role == roles.PLANNER or (models is not None and models.roles.get(role) == models.roles.get(roles.PLANNER)):
        return HTTPException(400, NO_MODEL_FROM_FILE)
    return HTTPException(400, f"No model configured for the {role}: set, on the server, the environment variable "
                              f"the host's models file names for the {role}'s entry (api_key_env). The browser's "
                              f"model settings replace the planner's entry only.")


@router.post("/chat", dependencies=[Depends(rate_limit)], response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {}},
                              "description": "SSE conversation. The questions event carries ask_id and "
                                             "1-4 questions with id, text, options, allow_other and multiple. "
                                             "Reply with a message and optional answers. The plan event carries "
                                             "plan_id and steps with id, title, status and optional evidence_id. "
                                             "A new planner stage can propose the next step in the same stream."}})
async def chat(req: ChatRequest, request: Request,
               x_run_label: str | None = Header(None, description=RUN_LABEL_DOC)):
    if (req.message is None) == (req.execution is None):
        raise ApiError(422, "invalid_args", "send exactly one of message and execution")
    if req.message is not None and not req.message.strip():
        raise ApiError(422, "invalid_args", "message is empty")
    if req.answers and not (req.bridge and req.message and req.session_id):
        raise ApiError(422, "invalid_answers", "answers require a bridge message and its session")
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

    # The mode: the request's when this caller may pick it (a cloud host: an admin), else the
    # host's default; without the bridge the one loop.
    settings = get_settings()
    admin = settings.admin_token_ok(request.headers.get("x-admin-token"))
    mode = settings.resolve_chat_mode(req.mode, admin) if req.bridge else MODE_SINGLE
    with_roles = mode != MODE_SINGLE
    targets = {}
    session = get_session_store().get_or_create(req.session_id)
    if req.bridge and (session.bridge_turn.locked() or session.auto_worker is not None):
        raise ApiError(409, "turn_in_progress", "Another turn is still running in this session.")
    plan = session.plan if req.bridge else None
    step = plan.current if plan else None
    if step and plan.scope != current_scope():
        raise ApiError(422, "invalid_step", "This plan belongs to another device.")
    if step and plan.reporting:
        raise ApiError(409, "step_in_progress", "The current execution report is already being reviewed.")
    resume_message = None
    single_receipt = session.pending_auto_receipt if req.bridge else None
    if single_receipt:
        scope, receipt = single_receipt
        if scope != current_scope():
            raise ApiError(422, "invalid_step", "The pending execution belongs to another device.")
        req.execution = receipt
        req.message = None
        req.answers = None
        req.mentions = []
        req.run = "confirm"
    elif step and plan.auto_receipt_pending and plan.receipt is not None and req.message and not req.answers:
        # A stopped stream may never have delivered the receipt. A new user turn resumes
        # supervision of the completed operation, with any subsequent card left for review.
        resume_message = req.message
        req.execution = plan.receipt
        req.message = None
        req.run = "confirm"
    pending = session.pending_form
    verified = req.answers.verified_items(pending) if req.answers else None
    if req.answers and verified is None:
        raise ApiError(422, "invalid_answers", "answers must match this session's open form")
    form_routing = session.form_routing if verified else None
    stop_reply = (session.form_stop_reply if verified and verified[0]["value"] == "stop"
                  and not verified[0].get("other") else None)
    decision = verified[0]["value"] if verified and session.form_stop_reply and not verified[0].get("other") else None
    if not stop_reply:
        try:
            target = await resolve_model(ROLE, request.headers)
        except LLMError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        llm = target.llm
        if not llm.configured:
            raise no_model(target, ROLE)
        targets[ROLE] = target
    if with_roles and not stop_reply:
        # The role that opens this turn: the analyst (a message, in mode roles_analyst) or the
        # supervisor (a report); the planner may follow either.
        first = (roles.SUPERVISOR if req.execution is not None
                 else roles.ANALYST if mode == MODE_ROLES_ANALYST and form_routing is None else None)
        if first is not None:
            try:
                targets[first] = await resolve_model(first, request.headers)
            except LLMError as exc:
                raise HTTPException(exc.status, str(exc)) from None
            if not targets[first].llm.configured:
                raise no_model(targets[first], first)
    elif not with_roles and (step or single_receipt) and req.execution is not None:
        # Single-model mode uses the same target to review this step, then to plan the next.
        targets[roles.SUPERVISOR] = target

    text = req.message if req.message is not None else execution_message(req.execution)
    if req.message and has_host_marker(text):
        text = "Designer wrote: " + quote_marked_json(text)
    if verified:
        block = {"ask_id": pending["ask_id"], "items": verified}
        serialized = json.dumps(block, ensure_ascii=False)
        if has_host_marker(serialized):
            serialized = quote_marked_json(block)
        text += f"\n\n{roles.ANSWERS_PREFIX} {serialized}"
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
        block = selection_block(picks["resolved"])
        if any(item["kind"] == "element" for item in picks["resolved"]):
            block += ("\nFor element_ids use a comma-separated string of selected element IDs and evidence "
                      "mention:element#<id>[,<id>...] with the same IDs.")
        if block:
            text = f"{text}\n\n{block}"
    # A device read can yield. Consume the form only after preflight succeeds
    # and only if another request has not already answered that form.
    if req.bridge and req.message and session.pending_form is not pending:
        raise ApiError(422, "invalid_answers", "the form was already answered or replaced")
    if req.bridge and (session.plan is not plan or (step and plan.current is not step)):
        raise ApiError(409, "plan_changed", "The plan changed while checking this request.")
    if req.bridge and (session.pending_auto_receipt is not single_receipt or session.auto_worker is not None):
        raise ApiError(409, "turn_in_progress", "The execution changed while checking this request.")
    if req.execution is not None:
        evidence = req.execution.get("evidence_id")
        if isinstance(evidence, str) and evidence in session.reported_evidence:
            raise ApiError(422, "invalid_step", "This execution report has already been used.")
        if step:
            if (session.plan is not plan or plan.current is not step or not plan.confirmed or plan.reporting
                    or plan.running or not plan.matches_report(req.execution)):
                raise ApiError(422, "invalid_step", "The report does not match this step's confirmed execution.")
            req.execution = plan.receipt
            text = execution_message(req.execution)
        if evidence:
            session.evidence_id = str(evidence)
    if req.bridge and not session.bridge_turn.acquire(blocking=False):
        raise ApiError(409, "turn_in_progress", "Another turn is still running in this session.")
    if step and req.execution is not None:
        plan.reporting = True
        if resume_message:
            plan.answers.append({"question": "After reconnecting", "answer": resume_message})
    changed_plan = None
    if req.bridge and req.message:
        session.direct_reads = 0
        session.read_results.clear()
        session.auto_count = 0
        if not step and pending is None and session.clarification is None:
            session.auto_paused = False
            session.request_text = req.message
            session.request_answers = []
            session.pending_interpretations.clear()
            session.proposed_in_request = False
        elif not step and pending is None and session.clarification:
            session.request_answers.append({"question": session.clarification, "answer": req.message})
        session.clarification = None
        if pending and (verified or req.message):
            answer = {"ask_id": pending["ask_id"], "questions": [q["text"] for q in pending["questions"]],
                      "answers": verified if verified else req.message}
            (plan.answers if step else session.request_answers).append(answer)
        if step and decision in ("revise", "continue"):
            step.status = "skipped" if decision == "continue" else "pending"
            changed_plan = plan.event()
            if not plan.current:
                stop_reply = "计划已结束。" if prefers_chinese(request.headers.get("accept-language", "")) else "The plan has ended."
    if req.bridge:
        session.pending_form = None
        session.form_routing = None
        session.form_stop_reply = None
    turn = session.next_turn()
    run = Turn(session, turn, run_label, req.bridge, mode, execution=req.execution)
    approved = None
    if session.auto_assumptions and verified and pending and pending["ask_id"] == session.auto_assumptions[0]:
        if verified[0].get("value") == "accept" and not verified[0].get("other"):
            approved = session.auto_assumptions[1]
    if req.bridge:
        session.auto_assumptions = None
    if req.bridge and req.run == "auto" and settings.auto_run and (admin or not settings.cloud):
        run.auto = AutoRun(session, request, settings, prefers_chinese(request.headers.get("accept-language", "")),
                           None if verified else req.message, approved)

    if stop_reply:
        log_model = None
        session.role_task = None
        session.pending_verdict = None
        session.spec = None
        session.confirmed_spec = None
        session.clarification = None
        session.request_text = ""
        session.request_answers = []
        if step:
            plan.stop()
            changed_plan = plan.event()
        session.add({"role": "user", "content": text})
        session.add({"role": "assistant", "content": stop_reply})

        async def generate() -> AsyncGenerator[str, None]:
            if changed_plan:
                yield format_sse_event("plan", changed_plan)
            if picks is not None:
                yield format_sse_event("mentions", picks)
            yield format_sse(stop_reply)
            yield SSE_DONE
    elif with_roles or ((step or single_receipt) and req.execution is not None):
        log_model = lambda: run.models_ran() or llm.model
        scope = current_scope()                   # the device this request drives
        async def generate() -> AsyncGenerator[str, None]:
            if changed_plan:
                yield format_sse_event("plan", changed_plan)
            if req.execution is not None:
                yield format_sse_event("execution", req.execution)
            if picks is not None:
                yield format_sse_event("mentions", picks)
            segments = (message_roles(run, targets, text, scope, form_routing=form_routing) if req.execution is None
                        else execution_roles(run, targets, req.execution, scope,
                                             chinese=prefers_chinese(request.headers.get("accept-language", ""))))
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
        log_model = llm.model
        session.add({"role": "user", "content": text})
        tools = TOOL_DEFINITIONS if req.bridge else None
        # The first turn's prompt for the whole session: the cached prefix stays the same.
        messages = [{"role": "system", "content": session.system_prompt(req.bridge, build_system_prompt)}]
        messages.extend(session.window(HISTORY_WINDOW))

        async def generate() -> AsyncGenerator[str, None]:
            if changed_plan:
                yield format_sse_event("plan", changed_plan)
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

    async def with_auto_reports():
        async with aclosing(generate()) as frames:
            async for frame in frames:
                if frame != SSE_DONE or not run.auto or run.auto.receipt is None:
                    yield frame
                    continue
                while run.auto.receipt is not None:
                    if await request.is_disconnected():
                        return
                    receipt, run.auto.receipt = run.auto.receipt, None
                    if receipt.get("success") is not True:
                        run.auto.paused = True
                    if roles.SUPERVISOR not in targets:
                        targets[roles.SUPERVISOR] = (await resolve_model(roles.SUPERVISOR, request.headers)
                                                     if with_roles else targets[ROLE])
                    targets.setdefault(roles.PLANNER, targets[ROLE])
                    async with aclosing(execution_roles(run, targets, receipt, current_scope(), chinese=run.auto.chinese)) as report:
                        async for part in report:
                            yield part
                yield SSE_DONE

    async def session_stream():
        try:
            async with aclosing(with_auto_reports()) as events:
                logged = log_and_stream(events, module="chat", session_id=session.session_id,
                                        client_ip=get_client_ip(request), user_agent=request.headers.get("user-agent", ""),
                                        user_input=text, model=log_model)
                async with aclosing(logged):
                    async for event in logged:
                        yield event
                        if req.bridge and event == SSE_DONE and session.pending_auto_receipt is not None:
                            evidence = session.pending_auto_receipt[1].get("evidence_id")
                            if isinstance(evidence, str):
                                session.reported_evidence.add(evidence)
                            session.pending_auto_receipt = None
                            session.confirmed_spec = None
        finally:
            if step and req.execution is not None:
                plan.reporting = False
            if req.bridge:
                session.bridge_turn.release()

    return StreamingResponse(
        session_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Session-Id": session.session_id},
    )
