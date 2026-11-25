"""Model settings. Only the boolean status is public on cloud deployments."""
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from backend.api.logs import verify_admin
from backend.config import get_settings, is_loopback
from backend.model_settings import ProfileWrite, Scope, StoreUnavailable, masked, read_profiles, update_profiles
from backend.models_file import PLANNER, ROLES, get_models, server_model

router = APIRouter(prefix="/api/v1/settings/models", tags=["settings"])


class ModelStatus(BaseModel):
    configured: bool


class MaskedModel(BaseModel):
    base_url: str
    model: str
    key_set: bool
    key_tail: str | None


class ModelDetails(ModelStatus):
    models: dict[str, MaskedModel]
    saved: dict[Scope, MaskedModel]


def describe(profiles, status_only=False):
    settings, models = get_settings(), get_models()
    targets = {r: server_model(settings, models.entry(r) if models else None,
                               profiles.get(r) or profiles.get("shared")) for r in ROLES}
    configured = targets[PLANNER].configured
    if status_only:
        return ModelStatus(configured=configured)
    return ModelDetails(configured=configured,
                        models={r: masked(t.base_url, t.model, t.api_key) for r, t in targets.items()},
                        saved={r: masked(p.base_url, p.model, p.key) for r, p in profiles.items()})


async def authorize_read(status_only: bool = False, x_admin_token: str | None = Header(None)):
    if not status_only:
        await verify_admin(x_admin_token)


async def authorize_write(request: Request, _=Depends(verify_admin)):
    if not get_settings().cloud and not (request.client and is_loopback(request.client.host)):
        raise HTTPException(403, "Model settings can only be changed from loopback in local mode")


def unavailable():
    return HTTPException(503, "Model settings could not be read or written. Check the settings store.")


@router.get("", dependencies=[Depends(authorize_read)])
def get_models_settings(response: Response, status_only: bool = False) -> ModelDetails | ModelStatus:
    response.headers["Cache-Control"] = "no-store"
    try:
        return describe(read_profiles(), status_only)
    except StoreUnavailable:
        if status_only:
            return ModelStatus(configured=False)
        raise unavailable() from None


@router.put("", dependencies=[Depends(authorize_write)])
def put_models_settings(body: ProfileWrite, response: Response) -> ModelDetails:
    """Save one group. An omitted key retains the saved key at the same endpoint."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return describe(update_profiles(body.role, body))
    except ValueError:
        raise HTTPException(422, "Invalid model settings") from None
    except StoreUnavailable:
        raise unavailable() from None


@router.delete("", dependencies=[Depends(authorize_write)])
def delete_models_settings(response: Response, role: Scope | None = None) -> ModelDetails:
    """Clear one saved group, or all saved groups when role is omitted."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return describe(update_profiles(role))
    except StoreUnavailable:
        raise unavailable() from None
