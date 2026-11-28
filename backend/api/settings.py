"""Model settings. Only the boolean status is public on cloud deployments."""
from contextlib import contextmanager
import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from backend.api.errors import ApiError, ErrorBody
from backend.api.logs import verify_admin
from backend.config import get_settings, is_loopback
from backend.model_settings import (
    LIBRARY_LIMIT, SAVED_NAME, LibraryProfile, Profile, ProfileWrite, Scope, SelectionRole,
    SelectionWrite, StoreUnavailable, edit_profiles, read_profiles, replace_profile,
)
from backend.models_file import PLANNER, effective_model, get_models, has_entry, public_models, runtime_profiles

router = APIRouter(prefix="/api/v1/settings/models", tags=["settings"])
UNKNOWN_ENTRY = {"model": ErrorBody, "description": "The entry does not exist (unknown_entry)."}
INVALID_INPUT = {"model": ErrorBody, "description": "Invalid model settings (invalid_args)."}


class ModelSettingsCacheControl:
    """Also protect errors returned before the settings handler runs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = Request(scope).url.path if scope["type"] == "http" else ""
        if path != router.prefix and not path.startswith(router.prefix + "/"):
            return await self.app(scope, receive, send)

        async def no_store(message):
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, no_store)


class ModelStatus(BaseModel):
    configured: bool


class MaskedModel(BaseModel):
    base_url: str
    model: str
    key_set: bool
    key_tail: str | None


class LibraryModel(MaskedModel):
    name: str
    source: Literal["file", "saved"]
    api: Literal["chat", "responses"]
    vision: bool
    shadowed: bool
    selectable: bool


class RoleSelection(BaseModel):
    entry: str | None
    selected_missing: bool


class ModelDetails(ModelStatus):
    models: dict[str, MaskedModel]
    saved: dict[Scope, MaskedModel]
    library: list[LibraryModel]
    selection: dict[SelectionRole, RoleSelection]


def describe(store):
    return ModelDetails(**public_models(store, get_settings(), get_models()))


async def authorize_read(status_only: bool = False, x_admin_token: str | None = Header(None)):
    if not status_only:
        await verify_admin(x_admin_token)


async def authorize_write(request: Request, _=Depends(verify_admin)):
    settings = get_settings()
    if not settings.cloud and not settings.in_container and not (request.client and is_loopback(request.client.host)):
        raise HTTPException(403, "Model settings can only be changed from loopback in local mode")


def unavailable():
    return HTTPException(503, "Model settings could not be read or written. Check the settings store.")


@router.get("", dependencies=[Depends(authorize_read)])
def get_models_settings(status_only: bool = False) -> ModelDetails | ModelStatus:
    if status_only:
        settings, models = get_settings(), get_models()
        store = runtime_profiles(settings, models)
        return ModelStatus(configured=effective_model(PLANNER, store, settings, models).configured)
    try:
        return describe(read_profiles())
    except StoreUnavailable:
        raise unavailable() from None


@contextmanager
def edit(*, clear=False, scope_group=False):
    try:
        with edit_profiles(clear=clear) as store:
            yield store
    except ValueError:
        if scope_group:
            raise HTTPException(422, "Invalid model settings") from None
        raise ApiError(422, "invalid_args") from None
    except StoreUnavailable:
        raise unavailable() from None


@router.put("", dependencies=[Depends(authorize_write)])
def put_models_settings(body: ProfileWrite) -> ModelDetails:
    """Save one group. An omitted key retains the saved key at the same endpoint."""
    with edit(scope_group=True) as store:
        profile = Profile.model_validate(body.model_dump(exclude={"role"}))
        store.profiles[body.role] = replace_profile(profile, store.profiles.get(body.role))
    return describe(store)


@router.delete("", dependencies=[Depends(authorize_write)])
def delete_models_settings(role: Scope | None = None) -> ModelDetails:
    """Clear one saved group, or all groups, library entries and selections."""
    with edit(clear=role is None, scope_group=True) as store:
        store.profiles.pop(role, None)
    return describe(store)


@router.put("/library/{name}", dependencies=[Depends(authorize_write)], responses={
    409: {"model": ErrorBody, "description": "A file owns the name (file_entry) or the library is full (library_full)."},
    422: INVALID_INPUT})
def put_library_entry(name: str, body: LibraryProfile) -> ModelDetails:
    """Save a named entry. Omitting its key retains it only at the same endpoint."""
    models = get_models()
    with edit() as store:
        if models and name in models.entries:
            raise ApiError(409, "file_entry")
        if not re.fullmatch(SAVED_NAME, name):
            raise ApiError(422, "invalid_args")
        if name not in store.library and len(store.library) >= LIBRARY_LIMIT:
            raise ApiError(409, "library_full")
        store.library[name] = replace_profile(body, store.library.get(name))
    return describe(store)


@router.delete("/library/{name}", dependencies=[Depends(authorize_write)], responses={404: UNKNOWN_ENTRY, 422: INVALID_INPUT})
def delete_library_entry(name: str) -> ModelDetails:
    """Delete a saved entry. Selections using a file entry of the same name survive."""
    with edit() as store:
        if store.library.pop(name, None) is None:
            raise ApiError(404, "unknown_entry")
        if not has_entry(name, store, get_models()):
            store.selection = {r: n for r, n in store.selection.items() if n != name}
    return describe(store)


@router.put("/selection", dependencies=[Depends(authorize_write)], responses={
    404: UNKNOWN_ENTRY, 422: INVALID_INPUT})
def put_selection(body: SelectionWrite) -> ModelDetails:
    """Select a library entry for a role. A file entry wins a duplicate name. Null restores the default."""
    models = get_models()
    with edit() as store:
        name = body.entry
        if name is None:
            store.selection.pop(body.role, None)
        elif not has_entry(name, store, models):
            raise ApiError(404, "unknown_entry")
        else:
            store.selection[body.role] = name
    return describe(store)
