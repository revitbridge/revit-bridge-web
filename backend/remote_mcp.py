"""Host boundary of the package's stateless Streamable HTTP app."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response

from revit_bridge.paths import data_root
from revit_bridge.revit import RevitClientPool, RevitSettings

from backend import mcp_tokens
from backend.api.devices import get_device_store
from backend.config import is_loopback
from backend.localguard import host_names_for
from backend.model_settings import StoreUnavailable
from backend.ratelimit import RateLimiter, client_key
from backend.relay import WebSocketRevitClient, get_relay
from backend.remote_trials import CHANNEL, MAX_BYTES, RemoteTrials, ledger_for, trial_evidence_id


def transport_security(settings):
    loopback = host_names_for(settings.exposed)
    hosts = {value for host in loopback for value in (host, host + ":*")}
    origins = {"http://" + host for host in hosts}
    if settings.cloud:
        # Only concrete, configured authorities can extend the SDK's allowlist.
        urls = [*settings.cors_origins, settings.public_ws_base]
        bind = settings.host.strip("[]")
        if bind not in ("0.0.0.0", "::"):
            host = f"[{bind}]" if ":" in bind else bind
            urls.append(f"http://{host}:{settings.port}")
        for url in urls:
            try:
                parts = urlsplit(url)
                scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
                if (scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                        or "*" in parts.netloc or parts.query or parts.fragment):
                    continue
                _ = parts.port  # reject invalid ports
            except ValueError:
                continue
            hosts.add(parts.netloc.lower())
            origins.add(f"{scheme}://{parts.netloc.lower()}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=sorted(hosts), allowed_origins=sorted(origins))


class LocalClient:
    """Use the web host's pool only for operations that actually need the add-in."""

    async def ensure_connected(self):
        await RevitClientPool.get_client()

    async def send_command(self, method, params=None):
        client = await RevitClientPool.get_client()
        return await client.send_command(method, params)

    async def send_code(self, code, parameters=None, confirm=None):
        client = await RevitClientPool.get_client()
        return await client.send_code(code, parameters, confirm=confirm)


class RemoteMCP:
    def __init__(self, settings):
        self.settings = settings
        self.failures = RateLimiter()
        self.trials = RemoteTrials()
        self.local = LocalClient()
        self.app = None
        if settings.remote_mcp:
            from revit_bridge.mcp_app import build_asgi_app
            self.app = build_asgi_app(
                self.get_client, channel=CHANNEL, transport_security=transport_security(settings),
                resolve_scope_dir=None if settings.cloud else self.local_root,
            )

    @staticmethod
    def local_root(device_id):
        if device_id != "local":
            raise ValueError("Unexpected local device")
        return data_root()

    @asynccontextmanager
    async def lifespan(self, parent):
        if self.app is None:
            yield
            return
        async with self.app.router.lifespan_context(self.app):
            try:
                yield
            finally:
                self.trials.close()

    async def authenticate(self, request):
        auth = request.headers.getlist("authorization")
        if (not auth and not self.settings.cloud and (self.settings.in_container
                or (request.client and is_loopback(request.client.host)))):
            return "local", None
        if len(auth) != 1:
            raise mcp_tokens.InvalidToken
        scheme, _, bearer = auth[0].partition(" ")
        if scheme.lower() != "bearer":
            raise mcp_tokens.InvalidToken
        device_id, token_id = await run_in_threadpool(mcp_tokens.authenticate, bearer, self.settings.cloud)
        if self.settings.cloud:
            await self.check_paired_device(device_id)
        return device_id, token_id

    @staticmethod
    async def check_paired_device(device_id):
        devices = await run_in_threadpool(get_device_store().list)
        if not any(d.device_id == device_id and d.last_seen and not d.revoked_at for d in devices):
            raise mcp_tokens.InvalidToken

    async def get_client(self, request):
        # Only our authenticated HTTP entry point supplies this request-local scope.
        device_id = request.state.mcp_device
        if request.state.mcp_token_id is not None:
            # Revocation during upload must still stop dispatch.
            if self.settings.cloud:
                await self.check_paired_device(device_id)
            await run_in_threadpool(mcp_tokens.mark_used, device_id, request.state.mcp_token_id)
        if request.method == "POST":
            message = await request.json()  # the SDK has already bounded, read and parsed this body
            if message.get("method") == "tools/call":
                params = message.get("params", {})
                if params.get("name") == "dry_run_code":
                    code = (params.get("arguments") or {}).get("code")
                    if isinstance(code, str) and len(code.encode("utf-8")) <= MAX_BYTES:
                        request.state.trial_code = code
        client = (WebSocketRevitClient(get_relay(), device_id, RevitSettings.from_env().timeout)
                  if self.settings.cloud else self.local)
        return device_id, client

    async def __call__(self, scope, receive, send):
        if self.app is None:
            return await Response(status_code=404)(scope, receive, send)
        request = Request(scope)
        peer = client_key(request)
        if self.failures.count(peer) >= 10:
            return await Response(status_code=429, headers={"Retry-After": "60"})(scope, receive, send)
        try:
            device, token_id = await self.authenticate(request)
        except mcp_tokens.InvalidToken:
            self.failures.allow(peer, 10)
            return await Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        except (StoreUnavailable, OSError):
            return await Response(status_code=503)(scope, receive, send)

        # The SDK and its handlers only receive the verified identity. In particular,
        # a client-supplied device selector cannot override its token's device.
        private = {b"authorization", b"x-device-id", b"x-device-key", b"x-admin-token", b"x-llm-key", b"cookie"}
        state = {**scope.get("state", {}), "mcp_device": device, "mcp_token_id": token_id}
        bound = {**scope, "state": state, "headers": [(k, v) for k, v in scope["headers"] if k.lower() not in private]}

        async def observe(message):
            # build_asgi_app uses the SDK's single JSON response for a tool call.
            if message["type"] == "http.response.body" and "trial_code" in state:
                eid = trial_evidence_id(message.get("body", b""))
                if eid:
                    record = await run_in_threadpool(ledger_for(device).get, eid)
                    self.trials.remember(device, state["trial_code"], record)
            await send(message)

        await self.app(bound, receive, observe)
