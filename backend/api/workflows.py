"""Workflow v1 over HTTP. The package owns the files, YAML and validation."""
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Response
from revit_bridge.capabilities import ToolStore
from revit_bridge.workflows import WorkflowError, WorkflowStore, parse_workflow_yaml, validate_workflow

from backend.api.bridge import require_admin, require_tool_name, set_device_context
from backend.api.errors import ApiError, admin_responses, responses


router = APIRouter(prefix="/api/v1/bridge/workflows", tags=["bridge"], dependencies=[Depends(set_device_context)])
WRITE = [Depends(require_admin)]


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
    return {"errors": validate_workflow(definition, ToolStore())}


def save(definition, overwrite):
    try:
        WorkflowStore(ToolStore()).save(definition, overwrite=overwrite)
    except WorkflowError as exc:
        raise invalid(exc.problems) from None
    except FileExistsError:
        raise ApiError(409, "workflow_exists", "Set overwrite=true to replace the saved workflow.") from None
    return definition


@router.post("/import", dependencies=WRITE, responses=admin_responses(409, 422))
def import_workflow(yaml: bytes = Body(media_type="text/yaml"), overwrite: bool = False) -> dict:
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
