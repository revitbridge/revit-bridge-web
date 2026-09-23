"""Pairing and devices: ``/api/v1/bridge/devices*`` plus the request's device context.

A visitor pairs a Revit in three steps (phase 7 §1): the browser asks for a
pairing code (``POST /devices/pair``) and keeps the *browser key* it gets
back; the add-in redeems the code (``POST /devices/redeem``, no headers - the
code is the only credential it has) and keeps the *device token*; the add-in
connects to ``/ws/{device_id}`` and authenticates with that token. From then
on the browser drives that Revit with ``X-Device-Id`` / ``X-Device-Key``, and
``DELETE /devices/{id}`` ends it: the device is revoked and its socket closed.

All of the logic is the package's ``DeviceStore`` (hashes only, on the data
root); this module is the HTTP surface plus the per-request device context the
bridge routes read. Without ``X-Device-Id`` a request drives the local add-in
over TCP and its scope is ``"local"``.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar

from fastapi import Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from revit_bridge.auth import Device, DeviceError, DeviceStore

from backend.api.errors import ApiError, responses
from backend.config import get_settings
from backend.ratelimit import client_key, pair_limiter, redeem_limiter
from backend.relay import get_relay

_log = logging.getLogger("backend.devices")

HEADER_DEVICE_ID = "x-device-id"
HEADER_DEVICE_KEY = "x-device-key"
LEGACY_SLOT_HEADERS = ("x-slot-id", "x-slot-token")
REDEEM_LIMIT = 10                       # per address per minute, fixed (spec §3.1 item 4)

# Set per request by the device dependency; read where a Revit client or a scope is needed.
request_device: ContextVar[str | None] = ContextVar("device_id", default=None)


_store: DeviceStore | None = None


def get_device_store() -> DeviceStore:
    global _store
    if _store is None:
        _store = DeviceStore()
    return _store


def reset_device_store() -> None:
    """Drop the cached store (tests move the data root between cases)."""
    global _store
    _store = None


# -- the request's device -----------------------------------------------------------

async def set_device_context(connection: HTTPConnection) -> None:
    """Read ``X-Device-Id`` and verify ``X-Device-Key``; no header means the local add-in.

    A wrong key, an unknown device and a revoked one are the same 403
    ``invalid_device_key``: a browser learns nothing about devices it does not
    hold the key for. A valid ``X-Admin-Token`` is checked first and stands in
    for the key, so an operator drives and inspects any device. The add-in's
    WebSocket authenticates with its own token in the first message instead.
    """
    if any(header in connection.headers for header in LEGACY_SLOT_HEADERS):
        raise ApiError(422, "invalid_args",
                       "X-Slot-Id / X-Slot-Token are gone; pair the Revit and send "
                       "X-Device-Id / X-Device-Key (the add-in must be reinstalled)")
    device_id = connection.headers.get(HEADER_DEVICE_ID)
    request_device.set(device_id)
    if not device_id or admin_ok(connection):
        return
    key = connection.headers.get(HEADER_DEVICE_KEY) or ""
    if await run_in_threadpool(get_device_store().verify_browser, device_id, key) is None:
        raise ApiError(403, "invalid_device_key", "Unknown device, wrong X-Device-Key or revoked device")


def current_device() -> str | None:
    """The device this request drives, or None for the local add-in."""
    return request_device.get(None)


# -- request models ----------------------------------------------------------------

class PairRequest(BaseModel):
    label: str = Field(default="", max_length=120)


class RedeemRequest(BaseModel):
    code: str = Field(max_length=32)
    addin_version: str | None = Field(default=None, max_length=40)


# -- helpers -----------------------------------------------------------------------

INSTALLER_URL = ("https://raw.githubusercontent.com/revitbridge/"
                 "revit-bridge-addin/main/installer/install.ps1")


def public_origin(request: Request) -> str:
    """The address a visitor reached this host on: ``https://demo.example``.

    Behind a reverse proxy the request itself is plain http to 127.0.0.1, so
    ``X-Forwarded-Proto`` and ``X-Forwarded-Host`` decide when they are present
    (uvicorn is started with ``--proxy-headers`` and ``FORWARDED_ALLOW_IPS``).
    """
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = forwarded_host or request.url.netloc
    scheme = forwarded_proto or request.url.scheme
    scheme = {"wss": "https", "ws": "http"}.get(scheme, scheme)
    return f"{scheme}://{host}"


def install_command(request: Request, code: str) -> str:
    """The one line the designer runs on the Revit machine.

    The installer takes the *site* address and the pairing code; it redeems the
    code itself and learns the relay address from the answer, so no ws:// URL
    appears here (the installer refuses one).
    """
    return (f"& ([scriptblock]::Create((irm {INSTALLER_URL}))) "
            f"-Mode remote -Server {public_origin(request)} -Pair {code}")


def _ws_base_from(request: Request) -> str:
    """Where the add-in's socket lives, for the ``ws_url`` a redeemed pairing returns."""
    origin = public_origin(request)
    scheme = "wss" if origin.startswith("https://") else "ws"
    return f"{scheme}://{origin.split('://', 1)[1]}/api/v1/bridge/ws"


def device_view(device: Device, relay_status: dict) -> dict:
    """``GET /devices/{id}``: what the connect page shows (never a hash)."""
    return {
        "device_id": device.device_id,
        "label": device.label,
        "online": bool(relay_status["online"]),
        "last_seen": device.last_seen,
        "requests": int(relay_status["requests"]),
        "revoked": device.revoked_at is not None,
        "addin_version": device.addin_version,
    }


def admin_ok(connection: HTTPConnection | Request) -> bool:
    """A configured admin password, sent as ``X-Admin-Token``."""
    password = get_settings().admin_password
    return bool(password) and connection.headers.get("x-admin-token") == password


async def _device_or_404(device_id: str) -> dict:
    """The stored device dict, or 404 - the id exists or it does not."""
    devices = {d.device_id: d for d in await run_in_threadpool(get_device_store().list)}
    device = devices.get(device_id)
    if device is None:
        raise ApiError(404, "unknown_device", f"No device '{device_id}'", device_id=device_id)
    return {"device": device}


async def _authorized(request: Request, device_id: str) -> Device:
    """The device when the request holds its browser key, or the admin password.

    The admin token is checked first: with it the device headers are irrelevant
    (absent, empty or wrong alike).
    """
    found = (await _device_or_404(device_id))["device"]
    if admin_ok(request):
        return found
    key = request.headers.get(HEADER_DEVICE_KEY) or ""
    device = await run_in_threadpool(get_device_store().verify_browser, device_id, key)
    if device is None:
        raise ApiError(403, "invalid_device_key", "Wrong X-Device-Key or revoked device")
    return device


# -- routes --------------------------------------------------------------------------

def register(router) -> None:
    """Add the device routes to the bridge router (they share its error contract)."""

    def pair_rate_limit(request: Request) -> None:
        """A pairing writes a device and a code to the data root: the chat's limit, own table."""
        if not pair_limiter.allow(client_key(request), get_settings().chat_rate_limit):
            raise ApiError(429, "rate_limited", "Too many pairings from this address; try again in a minute")

    def redeem_rate_limit(request: Request) -> None:
        """Redeeming guesses a code: a fixed, low limit per address."""
        if not redeem_limiter.allow(client_key(request), REDEEM_LIMIT):
            raise ApiError(429, "rate_limited", "Too many redeem attempts from this address; try again in a minute")

    @router.post("/devices/pair", dependencies=[Depends(pair_rate_limit)],
                 responses=responses(422, 429))
    async def pair_device(req: PairRequest, request: Request):
        """``DeviceStore.create_pairing``: a code for the add-in and the browser key for this tab.

        The code and the key are returned once and stored only as hashes; the
        browser keeps the key (session storage) - it is what lets this tab
        drive and revoke that Revit.
        """
        pairing = await run_in_threadpool(get_device_store().create_pairing, req.label.strip())
        return {
            "code": pairing.code,
            "device_id": pairing.device_id,
            "expires_at": pairing.expires_at,
            "browser_key": pairing.browser_key,
            "install_command": install_command(request, pairing.code),
        }

    @router.post("/devices/redeem", dependencies=[Depends(redeem_rate_limit)],
                 responses=responses(404, 422, 429))
    async def redeem_pairing(req: RedeemRequest, request: Request):
        """``DeviceStore.redeem``: the add-in turns the code into its device token.

        Called by the add-in or its installer, so it carries no device headers -
        the code is the credential. Unknown, expired, already used: all
        ``invalid_code``, with no hint which.
        """
        try:
            redeemed = await run_in_threadpool(get_device_store().redeem, req.code, req.addin_version)
        except DeviceError:
            raise ApiError(404, "invalid_code", "This pairing code is not valid any more") from None
        _log.info("device %s redeemed a pairing (add-in %s)", redeemed.device.device_id,
                  redeemed.device.addin_version or "unknown")
        return {
            "device_id": redeemed.device.device_id,
            "device_token": redeemed.device_token,
            "ws_url": f"{get_settings().public_ws_base or _ws_base_from(request)}/{redeemed.device.device_id}",
        }

    @router.get("/devices", responses=responses(403, 503))
    async def list_devices(request: Request):
        """Every device, for an operator holding the admin password."""
        settings = get_settings()
        if not settings.admin_password:
            raise ApiError(503, "admin_disabled", "Set ADMIN_PASSWORD to use the device list")
        if not admin_ok(request):
            raise ApiError(403, "admin_required", "Wrong or missing X-Admin-Token")
        relay = get_relay()
        devices = await run_in_threadpool(get_device_store().list)
        return {"devices": [device_view(d, relay.device_status(d.device_id)) for d in devices]}

    @router.get("/devices/{device_id}", responses=responses(403, 404, 422))
    async def get_device(device_id: str, request: Request):
        """One device: is it online, when was it last seen, how many requests has it served."""
        device = await _authorized(request, device_id)
        return device_view(device, get_relay().device_status(device_id))

    @router.delete("/devices/{device_id}", responses=responses(403, 404, 422))
    async def revoke_device(device_id: str, request: Request):
        """Revoke the device and close its socket: no more requests, key and token dead."""
        await _authorized(request, device_id)
        await run_in_threadpool(get_device_store().revoke, device_id)
        closed = await get_relay().disconnect(device_id, code=4003, reason="device revoked")
        _log.info("device %s revoked (socket closed: %s)", device_id, closed)
        return {"status": "revoked", "device_id": device_id, "disconnected": closed}
