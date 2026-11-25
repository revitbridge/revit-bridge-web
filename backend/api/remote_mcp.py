"""Settings and transient authoring drafts for the requesting browser's device."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend import mcp_tokens
from backend.api import devices
from backend.api.errors import ApiError, device_store_responses, device_store_unavailable
from backend.config import get_settings
from backend.model_settings import StoreUnavailable
from backend.remote_trials import recent_calls

SETTINGS = "/api/v1/settings/remote-mcp"
TRIALS = "/api/v1/bridge/remote-trials"


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["remote-mcp"], dependencies=[Depends(no_store)])


async def selected_device(request: Request):
    if not get_settings().cloud:
        return "local"
    device_id = request.headers.get(devices.HEADER_DEVICE_ID)
    if not device_id:
        return None
    try:
        device = await devices.authorized_device(request, device_id)
    except OSError:
        raise device_store_unavailable() from None
    if device.revoked_at or not device.last_seen:
        raise ApiError(403, "invalid_device_key", "An active paired device is required")
    return device.device_id


def required_device(device: str | None = Depends(selected_device)) -> str:
    if device is None:
        raise ApiError(403, "device_required", "Select a paired device")
    return device


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)

    @field_validator("label")
    @classmethod
    def public_label(cls, value):
        if not value.isprintable():
            raise ValueError("Invalid token label")
        return value


class NewToken(BaseModel):
    token: str
    info: mcp_tokens.TokenInfo


class Call(BaseModel):
    time: str
    tool: str
    success: bool
    evidence_id: str


class Status(BaseModel):
    enabled: bool
    device_id: str | None
    tokens: list[mcp_tokens.TokenInfo]
    calls: list[Call]


def unavailable():
    return HTTPException(503, "Remote MCP settings store unavailable")


@router.get(SETTINGS)
def remote_status(device: str | None = Depends(selected_device)) -> Status:
    """Endpoint status, token metadata and the selected device's recent remote execution evidence."""
    try:
        return Status(enabled=get_settings().remote_mcp, device_id=device,
                      tokens=mcp_tokens.list_tokens(device) if device else [],
                      calls=recent_calls(device) if device else [])
    except StoreUnavailable:
        raise unavailable() from None


@router.post(SETTINGS + "/tokens", status_code=201)
def create_token(body: TokenRequest, device: str = Depends(required_device)) -> NewToken:
    """Issue a bearer once. Cloud requires a redeemed X-Device-Id and its X-Device-Key or X-Admin-Token."""
    try:
        token, info = mcp_tokens.create(device, body.label)
        return NewToken(token=token, info=info)
    except StoreUnavailable:
        raise unavailable() from None


@router.delete(SETTINGS + "/tokens/{token_id}")
def revoke_token(token_id: str, device: str = Depends(required_device)) -> dict[str, bool]:
    """Revoke a bearer by its public management ID for the selected device."""
    try:
        mcp_tokens.revoke(device, token_id)
    except mcp_tokens.InvalidToken:
        raise HTTPException(404, "Token not found") from None
    except StoreUnavailable:
        raise unavailable() from None
    return {"revoked": True}


class Trial(BaseModel):
    time: str
    evidence_id: str
    code_sha256: str
    rolled_back: bool


@router.get(TRIALS, responses=device_store_responses(403, 404))
async def list_trials(request: Request, device: str | None = Depends(selected_device)) -> dict[str, list[Trial]]:
    """Successful remote rollbacks with source still available in this device's draft cache."""
    drafts = request.app.state.remote_mcp.trials.devices.get(device, {})
    return {"trials": [draft.evidence for draft in reversed(drafts.values())]}


@router.get(TRIALS + "/{evidence_id}", responses=device_store_responses(403, 404, 422))
async def load_trial(evidence_id: str, request: Request, device: str = Depends(required_device)) -> dict[str, str]:
    """Load the selected device's draft source into the existing authoring form."""
    draft = request.app.state.remote_mcp.trials.devices.get(device, {}).get(evidence_id)
    if not draft:
        raise ApiError(404, "trial_not_found", "Trial draft not found or expired")
    return {"code": draft.code}
