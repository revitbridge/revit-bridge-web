"""The request guard of a local host (``DEPLOY_MODE=local``).

A local host has no password, so it answers only what looks like a browser on
this machine talking to it directly. Every request - API, static files,
``/config.json``, ``/health``, WebSocket upgrades - passes these checks in this
order, and the first that fails answers 403 ``{error, message}``:

1. ``local_mode_no_proxy``: no proxy forwarding header (``Forwarded``,
   ``X-Forwarded-For``, ``X-Forwarded-Host``, ``X-Forwarded-Proto``, ``X-Real-IP``,
   ``CF-Connecting-IP``). A reverse proxy or a tunnel in front of a loopback port
   is how a local host would end up reachable from elsewhere.
2. ``local_mode_host``: the ``Host`` header names loopback - ``127.0.0.1``,
   ``localhost`` or ``[::1]`` with any port, plus the loopback address the host
   was configured on when that is another one (``HOST=127.0.0.2``). Against DNS
   rebinding: a page on another origin cannot reach the host through a name of
   its own that resolves to 127.0.0.1. Outside a container the peer address must
   be loopback too, which also holds when a server was started some other way
   (``uvicorn --host 0.0.0.0``) and a client wrote ``Host: 127.0.0.1`` itself.
   Inside the image the peer is Docker's gateway, so that part is skipped; what
   reaches the container there is limited by the published BIND_ADDRESS.
3. ``local_mode_origin``, two ways:
   - any request - every method, WebSocket upgrades too - whose ``Sec-Fetch-Site``
     is ``cross-site`` or ``same-site``: a browser says so for a request another
     site's page started, including the GETs that reach Revit (``/snapshot``,
     ``/tools/{name}/choices``) and body-less POSTs (``/trigger-selection``,
     ``/evidence/{id}/validate``). ``same-origin``, ``none`` (typed address,
     bookmark) and no header at all (curl, the bench runner) pass;
   - a POST, PUT, PATCH or DELETE, or a WebSocket upgrade, that carries ``Origin``
     (every browser sends it on these) must come from the host's own origin,
     ``http://`` plus the Host header. Browsers apply no same-origin rule to
     WebSockets, and some state-changing routes take no body (``DELETE
     /tools/{name}``, ``DELETE /api/skills/{id}``...), so a cross-site form post
     would otherwise reach them; JSON bodies are refused as ``text/plain`` or form
     data anyway, and CORS stays same-origin (``CORS_ORIGINS`` refuses to start in
     local mode).

Requests without ``Origin`` and ``Sec-Fetch-Site`` (curl, scripts on this machine)
are not refused by 3; 1 and 2 still apply. A cloud host does not install this guard.
"""
from __future__ import annotations

import ipaddress

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from backend.config import is_loopback

NO_PROXY = "local_mode_no_proxy"
NOT_LOCAL = "local_mode_host"
FOREIGN_ORIGIN = "local_mode_origin"

FORWARDING_HEADERS = (b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-forwarded-proto",
                      b"x-real-ip", b"cf-connecting-ip")
LOOPBACK_HOST_NAMES = ("127.0.0.1", "localhost", "[::1]")
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Sec-Fetch-Site values that mean another site's page started the request.
FOREIGN_FETCH_SITES = frozenset({"cross-site", "same-site"})


def host_name(host_header: str) -> str | None:
    """The name of a ``Host`` header in lower case, IPv6 in brackets (``[::1]``);
    None when it is not ``name`` or ``name:port``."""
    value = host_header.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        name, rest = value[:end + 1], value[end + 1:]
    else:
        name, colon, port = value.partition(":")
        rest = colon + port
    port = rest[1:]
    # ASCII digits only: str.isdigit() alone also takes a superscript two (a latin-1 byte) or an Arabic-Indic three.
    if not name or (rest and not (rest.startswith(":") and port.isascii() and port.isdigit())):
        return None
    return name


def host_names_for(exposed: str) -> tuple[str, ...]:
    """The Host names a local host accepts: the loopback names, plus the loopback
    address it was configured on when that is a literal not among them."""
    names = list(LOOPBACK_HOST_NAMES)
    try:
        address = ipaddress.ip_address(exposed.strip().strip("[]"))
    except ValueError:
        return tuple(names)
    if address.is_loopback:
        name = f"[{address.compressed}]" if address.version == 6 else address.compressed
        if name not in names:
            names.append(name)
    return tuple(names)


class LocalModeGuard:
    """Pure ASGI middleware: the three checks above, for HTTP and WebSocket scopes."""

    def __init__(self, app: ASGIApp, host_names: tuple[str, ...] = LOOPBACK_HOST_NAMES,
                 check_peer: bool = True) -> None:
        self.app = app
        self.host_names = frozenset(host_names)
        self.check_peer = check_peer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refusal = self.refusal(scope)
        if refusal is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": refusal[0], "message": refusal[1]}, status_code=403)
        if scope["type"] == "http":
            await response(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        try:
            await websocket.send_denial_response(response)
        except RuntimeError:                   # a server without the denial extension: HTTP 403
            await websocket.close(code=1008, reason=refusal[0])

    def refusal(self, scope: Scope) -> tuple[str, str] | None:
        headers: dict[bytes, bytes] = {}
        for name, value in scope.get("headers") or ():
            headers.setdefault(name.lower(), value)

        forwarded = [h.decode() for h in FORWARDING_HEADERS if h in headers]
        if forwarded:
            return NO_PROXY, (f"This host runs in local mode and answers this machine directly; the request "
                              f"came through a proxy ({', '.join(forwarded)}). A host behind a proxy or a "
                              f"tunnel must run with DEPLOY_MODE=cloud and ADMIN_PASSWORD")

        host = headers.get(b"host", b"").decode("latin-1")
        if host_name(host) not in self.host_names:
            return NOT_LOCAL, (f"This host runs in local mode and answers only requests addressed to "
                               f"{', '.join(sorted(self.host_names))}; this one names {host or 'no host'!r}")
        client = scope.get("client")
        if self.check_peer and not (client and is_loopback(str(client[0]))):
            return NOT_LOCAL, "This host runs in local mode and answers this machine only"

        fetch_site = headers.get(b"sec-fetch-site", b"").decode("latin-1").strip().lower()
        if fetch_site in FOREIGN_FETCH_SITES:
            return FOREIGN_ORIGIN, (f"This host runs in local mode and answers only its own page; another "
                                    f"site's page started this request (Sec-Fetch-Site: {fetch_site})")

        websocket = scope["type"] == "websocket"
        if websocket or scope.get("method", "").upper() in STATE_CHANGING_METHODS:
            origin = headers.get(b"origin")
            if origin is not None and origin.decode("latin-1").strip().lower() != f"http://{host.strip().lower()}":
                what = "connections" if websocket else "changes"
                return FOREIGN_ORIGIN, (f"This host runs in local mode and takes {what} only from its own "
                                        f"page (http://{host}); this request came from "
                                        f"{origin.decode('latin-1')!r}")
        return None
