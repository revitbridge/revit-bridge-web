"""Error responses of the v1 contract: ``{"error": <code>, "message"?: ..., ...}``.

The codes are the MCP tools' (``invalid_category``, ``revit_unreachable``,
``invalid_spec``, ``confirmation_required``, ``invalid_pack``...) so a page
and a model see the same vocabulary. The status says what kind of failure it
is: a request that cannot be honoured as written is 4xx, a Revit that cannot
be reached is 503, an execution the package refused or that failed its
validation is 200 with ``success: false`` (the payload carries the reason).
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.language import prefers_chinese

BRIDGE_PREFIX = "/api/v1/bridge"


class ErrorBody(BaseModel):
    """What every non-2xx bridge response carries (further fields depend on the code:
    ``hint``, ``problems``, ``errors``, ``warnings``, ``kind``, ``tool``, ``endpoint``...)."""
    model_config = ConfigDict(extra="allow")

    error: str
    message: str | None = None


_DESCRIPTIONS = {
    400: "Refused as requested (confirmation_required, invalid_category, unknown_kind, blocked, no_validator)",
    403: "Slot header missing or wrong (missing_slot, invalid_slot_token)",
    404: "Not found (unknown_tool, unknown_evidence)",
    409: "Would change what must stay (builtin_read_only: built-in packs are read-only; "
         "pack_exists: a user pack of that name exists, import with overwrite=true to replace it)",
    422: "Body or query parsed but not valid (invalid_args, invalid_spec, invalid_snapshot, invalid_pack)",
    429: "Too many requests from this address (rate_limited)",
    500: "The package failed (snapshot_failed, query_failed)",
    502: "The model did not return a usable draft (invalid_draft)",
    503: "No Revit add-in answers (revit_unreachable)",
    504: "Revit did not answer in time (revit_timeout)",
}


def responses(*statuses: int) -> dict:
    """The ``responses=`` entry of a bridge route: ``ErrorBody`` for each status it returns.
    Declaring 422 replaces FastAPI's ``HTTPValidationError`` for that route, so every
    route with a path, query or body parameter declares it (the handler answers
    ``invalid_args`` there)."""
    return {status: {"model": ErrorBody, "description": _DESCRIPTIONS[status]} for status in statuses}


# The routes that change capability packs: 403 and 503 come from the admin check first.
_ADMIN_DESCRIPTIONS = {
    403: "X-Admin-Token missing or wrong (admin_required), or a device key that does not verify (invalid_device_key)",
    503: "No ADMIN_PASSWORD on this host: packs cannot be changed (admin_disabled)",
}


def admin_responses(*statuses: int) -> dict:
    """``responses()`` for a route that needs the admin password: 403 and 503 say why."""
    declared = responses(*statuses)
    for status in (403, 503):
        declared[status] = {"model": ErrorBody, "description": _ADMIN_DESCRIPTIONS[status]}
    return declared


# The routes that write the device store (pair, redeem, revoke). revit-bridge raises
# OSError when devices.json cannot be read for about half a second (a scanner, another
# process holding it) - before anything is written - instead of writing an empty store.
DEVICE_STORE_UNAVAILABLE = "device_store_unavailable"
_DEVICE_STORE_DESCRIPTION = "The device store could not be read for a moment; nothing changed, try again (device_store_unavailable)"


def device_store_responses(*statuses: int) -> dict:
    """``responses()`` for a route that writes the device store, plus its 503."""
    declared = responses(*statuses)
    declared[503] = {"model": ErrorBody, "description": _DEVICE_STORE_DESCRIPTION}
    return declared


_SELECTION_MESSAGES = {
    "selection_changed": ("The Revit selection changed. Check it and send again.", "Revit 中的选择已变化，请核对后重新发送。"),
    "selection_too_large": ("Narrow the selection or remove it from this message.", "请缩小选择范围或从消息中移除当前选择。"),
    "selection_busy": ("Selection is paused during a conversation or watch.", "对话或观察期间已暂停读取选择。"),
    "selection_unavailable": ("The Revit selection is unavailable.", "暂时无法读取 Revit 选择。"),
    "selection_question_changed": ("This selection question is no longer active.", "此选择问题已失效。"),
    "watch_in_progress": ("A selection watch is already running on this device.", "该设备已在观察选择。"),
}


class ApiError(Exception):
    """Raise anywhere under a route; the handler turns it into the JSON response."""

    def __init__(self, status: int, error: str, message: str | None = None, **extra):
        super().__init__(message or error)
        self.status = status
        self.payload: dict = {"error": error}
        if message is not None:
            self.payload["message"] = message
        self.payload.update(extra)


def device_store_unavailable() -> ApiError:
    """503 for an OSError from a DeviceStore write (see ``device_store_responses``)."""
    return ApiError(503, DEVICE_STORE_UNAVAILABLE, "The device store could not be read just now; try again in a moment")


def revit_unreachable(exc: BaseException | None = None, **extra) -> ApiError:
    message = (str(exc) or type(exc).__name__) if exc is not None else "Revit add-in not reachable"
    return ApiError(503, "revit_unreachable", message, **extra)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        message = _SELECTION_MESSAGES.get(exc.payload["error"])
        language = request.headers.get("accept-language")
        if language and message:
            payload = {**exc.payload, "message": message[1 if prefers_chinese(language) else 0]}
        else:
            payload = exc.payload
        return JSONResponse(status_code=exc.status, content=payload)

    @app.exception_handler(RequestValidationError)
    async def _invalid_args(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A body or query that does not fit a bridge route: the MCP tools call it
        invalid_args. Every other route keeps FastAPI's ``{detail: [...]}``."""
        if request.url.path == "/api/chat" and isinstance(exc.body, dict) and (
                "capture" in exc.body or "hidden_captures" in exc.body or "sketch" in exc.body):
            return JSONResponse(status_code=422, content={"error": "invalid_args"})
        if request.url.path == "/api/v1/settings/models":
            from backend.config import get_settings
            settings = get_settings()
            if settings.cloud and not settings.admin_token_ok(request.headers.get("x-admin-token")):
                return JSONResponse(status_code=403, content={"detail": "Unauthorized"})
            return JSONResponse(status_code=422, content={"detail": "Invalid model settings"})
        if request.url.path.startswith("/api/v1/settings/remote-mcp"):
            return JSONResponse(status_code=422, content={"detail": "Invalid Remote MCP settings"})
        if not request.url.path.startswith(BRIDGE_PREFIX):
            return await request_validation_exception_handler(request, exc)
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body" and not isinstance(p, int))
        message = f"{where}: {first.get('msg')}" if where else str(first.get("msg", "invalid request"))
        return JSONResponse(status_code=422, content={
            "error": "invalid_args", "message": message,
            "detail": [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in errors],
        })
