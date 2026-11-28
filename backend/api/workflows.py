"""Workflow v1 over HTTP. The package owns the files, YAML and validation."""
import re
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from revit_bridge.capabilities import ToolStore
from revit_bridge.workflows import WorkflowError, WorkflowStore, parse_workflow_yaml, validate_workflow

from backend import workflow_text
from backend.api.bridge import bridge_write_rate_limit, require_admin, require_tool_name, set_device_context
from backend.api.errors import ApiError, ErrorBody, admin_responses, responses
from backend.workflow_validation import binding_errors


router = APIRouter(prefix="/api/v1/bridge/workflows", tags=["bridge"], dependencies=[Depends(set_device_context)])
WRITE = [Depends(require_admin)]
MAX_WORKFLOW_BYTES = 256 * 1024


def invalid(errors):
    return ApiError(422, "invalid_workflow", "\n".join(errors), errors=errors)


def saved_workflow(name, *, yaml=False):
    store = WorkflowStore(ToolStore())
    try:
        value = (store.read_yaml if yaml else store.load)(require_tool_name(name))
    except WorkflowError as exc:
        raise invalid(exc.problems) from None
    if value is None:
        raise ApiError(404, "unknown_workflow", f"Unknown workflow: {name}")
    return value


@router.get("", responses=responses(403))
def list_workflows() -> list[dict]:
    return WorkflowStore(ToolStore()).list_workflows()


@router.post("/validate", responses=responses(403, 422))
def validate(definition: Any = Body(None)) -> dict[str, list[str]]:
    return {"errors": definition_errors(definition, ToolStore())}


def definition_errors(definition, store):
    return validate_workflow(definition, store) or binding_errors(definition, store)


def save(definition, overwrite):
    store = ToolStore()
    if errors := definition_errors(definition, store):
        raise invalid(errors)
    try:
        WorkflowStore(store).save(definition, overwrite=overwrite)
    except WorkflowError as exc:
        raise invalid(exc.problems) from None
    except FileExistsError:
        raise ApiError(409, "workflow_exists", "Set overwrite=true to replace the saved workflow.") from None
    return definition


@router.post("/import", dependencies=WRITE, responses=admin_responses(409, 422), openapi_extra={
    "requestBody": {"required": True, "description": "UTF-8 workflow YAML, at most 256 KiB.",
                    "content": {"text/yaml": {"schema": {"type": "string", "contentMediaType": "application/octet-stream",
                                                         "title": "Yaml"}}}}
})
async def import_workflow(request: Request, overwrite: bool = False) -> dict:
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise invalid(["invalid Content-Length"]) from None
    if declared < 0:
        raise invalid(["invalid Content-Length"])
    if declared > MAX_WORKFLOW_BYTES:
        raise invalid(["workflow YAML exceeds 256 KiB"])
    yaml = bytearray()
    async for chunk in request.stream():
        if len(yaml) + len(chunk) > MAX_WORKFLOW_BYTES:
            raise invalid(["workflow YAML exceeds 256 KiB"])
        yaml.extend(chunk)
    return await run_in_threadpool(import_yaml, yaml, overwrite)


def import_yaml(yaml, overwrite):
    try:
        definition = parse_workflow_yaml(yaml.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise invalid(["invalid workflow YAML: expected UTF-8"]) from None
    except WorkflowError as exc:
        raise invalid(exc.problems) from None
    return save(definition, overwrite)


@router.put("/{name}", dependencies=WRITE, responses=admin_responses(409, 422))
def save_workflow(name: str, definition: dict = Body(), overwrite: bool = False) -> dict:
    if require_tool_name(name) != definition.get("name"):
        raise invalid(["name: must match the URL"])
    return save(definition, overwrite)


@router.get("/{name}", responses=responses(403, 404, 422))
def get_workflow(name: str) -> dict:
    return saved_workflow(name)


@router.get("/tools/{name}/result-paths", responses=responses(403, 404, 422))
def result_paths(name: str) -> list[str]:
    """Paths shown by fixture expectations or explicit fields in a simple returned object."""
    tool = ToolStore().load(require_tool_name(name))
    if tool is None:
        raise ApiError(404, "unknown_tool", f"Unknown tool: {name}")

    def leaves(value, path):
        if isinstance(value, (list, dict)):
            for key, item in (enumerate(value) if isinstance(value, list) else value.items()):
                if str(key) and "." not in str(key):
                    yield from leaves(item, [*path, str(key)])
        elif path and value is not None:
            yield ".".join(path)

    paths = {path for fixture in tool.fixtures for path in leaves(fixture.get("expect"), [])}
    # Flat anonymous return objects are common in packs without a result schema.
    # Offer only explicit field names. A custom/dynamic result remains free text.
    for fields in re.findall(r"^\s*return\s+new\s*\{([^{}();]*)\}\s*;", tool.code_template, re.M):
        for part in fields.split(","):
            if match := re.fullmatch(r'\s*([A-Za-z_]\w*)\s*=\s*(?:[\w.]+|"[^"\\,]*")\s*', part):
                paths.add(match[1])
    return sorted(paths)


@router.get("/{name}/export", response_class=Response, responses={
    200: {"content": {"text/yaml": {"schema": {"type": "string"}}}}, **responses(403, 404, 422)})
def export_workflow(name: str):
    return Response(saved_workflow(name, yaml=True), media_type="text/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'})


@router.delete("/{name}", dependencies=WRITE, responses=admin_responses(404, 422))
def delete_workflow(name: str) -> dict:
    if not WorkflowStore(ToolStore()).delete(require_tool_name(name)):
        raise ApiError(404, "unknown_workflow", f"Unknown workflow: {name}")
    return {"status": "deleted", "name": name}


class OptimizeTextRequest(BaseModel):
    definition: dict = Field(description="Current workflow definition, up to 64 KiB of UTF-8 JSON.")
    language: Literal["en", "zh"]


class TextSuggestion(BaseModel):
    summary: str = Field(max_length=120)
    description: str = Field(max_length=4000)
    model: str


@router.post("/optimize-text", dependencies=[*WRITE, Depends(bridge_write_rate_limit)], responses={
    **admin_responses(422, 429),
    400: {"model": ErrorBody, "description": "Invalid model settings (model_error)"},
    413: {"model": ErrorBody, "description": "The definition exceeds 64 KiB (definition_too_large)"},
    502: {"model": ErrorBody, "description": "Model call failed or output was unusable (model_error, invalid_suggestion)"},
    503: {"model": ErrorBody, "description": "No model configured (model_required) or admin access disabled"},
}, openapi_extra={"requestBody": {"required": True, "content": {
    "application/json": {"schema": OptimizeTextRequest.model_json_schema()}}}})
async def optimize_text(request: Request) -> TextSuggestion:
    """Suggest wording for an unsaved workflow using only its text, step metadata and parameter names."""
    try:
        body = OptimizeTextRequest.model_validate_json(await request.body())
    except ValidationError:
        # Answer in the bridge error shape without repeating the rejected input.
        raise ApiError(422, "invalid_args", "Supply a definition object and language en or zh.") from None
    return await workflow_text.optimize_text(body.definition, body.language, request.headers)
