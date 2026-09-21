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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    """Raise anywhere under a route; the handler turns it into the JSON response."""

    def __init__(self, status: int, error: str, message: str | None = None, **extra):
        super().__init__(message or error)
        self.status = status
        self.payload: dict = {"error": error}
        if message is not None:
            self.payload["message"] = message
        self.payload.update(extra)


def revit_unreachable(exc: BaseException | None = None, **extra) -> ApiError:
    message = (str(exc) or type(exc).__name__) if exc is not None else "Revit add-in not reachable"
    return ApiError(503, "revit_unreachable", message, **extra)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.payload)

    @app.exception_handler(RequestValidationError)
    async def _invalid_args(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A body or query that does not fit the route: the MCP tools call it invalid_args."""
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg')}" if where else str(first.get("msg", "invalid request"))
        return JSONResponse(status_code=422, content={
            "error": "invalid_args", "message": message,
            "detail": [{k: v for k, v in e.items() if k in ("loc", "msg", "type")} for e in errors],
        })
