# revit-bridge-web

Deploy-anywhere demo host for [revit-bridge](https://github.com/revitbridge/revit-bridge): it serves the
web UI, talks to the Revit add-in on your machine and streams a model you bring yourself; deployed as a
cloud host it also relays remote Revit add-ins. The host contains no Revit logic of its own - every
query, execution and capability pack goes through the `revit_bridge` package.

设计师与 AI 之间的意图桥演示站：连接 Revit、用自带模型提出任务、审查并执行 C#、把可复用的代码固化为能力包。

## Start on your machine

By default the host runs in **local mode** (`DEPLOY_MODE=local`): it is reachable from this machine only,
it talks to the Revit add-in on this machine over TCP (`127.0.0.1:18080`), and there is no pairing, no
device list and no admin password - editing packs and skills and reading the logs just work.

```bash
git clone https://github.com/revitbridge/revit-bridge-web.git
cd revit-bridge-web
docker compose up -d --build              # one command: http://127.0.0.1:7860
```

Without Docker (Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20 to build the UI):

```bash
git clone https://github.com/revitbridge/revit-bridge-web.git
cd revit-bridge-web
cd frontend && npm ci && npm run build && cd ..   # the UI; again after each pull
uv run revit-bridge-web                           # http://127.0.0.1:7860; --port 7871 for another port
```

`uv run revit-bridge-web` runs from this checkout: it serves the API and the built UI
(`frontend/dist`, next to `backend/`) on `127.0.0.1`; without a build it serves the API only and says
so. The wheel the project builds contains only the backend, so installing it with `pip` or `uvx` is
not a supported way to run the host (there would be no UI). Open it as `http://127.0.0.1:7860` or
`http://localhost:7860` - typed, from a bookmark, or from a link in a README or a chat tool: a link
opens the page itself, while everything else another site's page asks for is refused (see below).

The image on its own, without compose - publish on loopback and pass the same address in:

```bash
docker build -t revit-bridge-web .
docker run -d --name revit-bridge-web -p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1 \
  --add-host host.docker.internal:host-gateway -v "$(pwd)/data:/app/data" revit-bridge-web
```

A local host has no password, so it is strict about who it answers:

- It refuses to start, and prints why, when other machines could reach it (`HOST=0.0.0.0`, or with
  Docker a `BIND_ADDRESS` other than loopback), when `ADMIN_PASSWORD` is set (that is a cloud host
  missing `DEPLOY_MODE=cloud`) and when `CORS_ORIGINS` is set.
- It refuses requests that do not look like a browser on this machine talking to it directly. One
  carrying a proxy forwarding header (`Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`,
  `X-Forwarded-Proto`, `X-Real-IP`, `CF-Connecting-IP`) is 403 `local_mode_no_proxy`; one addressed
  to any name other than `127.0.0.1`, `localhost` or `[::1]` (any port), or - outside Docker - from
  another machine, is 403 `local_mode_host`; one that another site's page started
  (`Sec-Fetch-Site: cross-site` or `same-site`, any method), and a change (POST, PUT, PATCH, DELETE)
  or a WebSocket from a page on another origin, are 403 `local_mode_origin`. The one exception is a
  link: a top-level navigation (GET or HEAD, `Sec-Fetch-Mode: navigate`, `Sec-Fetch-Dest: document`)
  to the page or a static file - not to `/api/...`, `/config.json`, `/openapi.json`, `/docs`,
  `/redoc` or `/health` - opens it. That holds for every path, `/health` and the static files
  included, and keeps web pages elsewhere (DNS rebinding, cross-site forms and requests) away from a
  host without a password.
- No other page may frame it: every response carries `Content-Security-Policy: frame-ancestors
  'self'` and `X-Frame-Options: SAMEORIGIN` (a frame from another site is refused anyway), so the UI
  without a password cannot be used for clickjacking.

What the checks cannot see:

- A reverse proxy that adds no forwarding header and sends `Host` with the loopback address - nginx's
  default `location / { proxy_pass http://127.0.0.1:7860; }` - passes every check. A host behind a
  proxy or a tunnel must run as a cloud host (below); local mode does not detect it.
- A forward proxy running on the same machine (a system or browser proxy) looks like a direct
  browser: the peer is loopback, the proxy rewrites `Host` and sends no forwarding header. So a local
  proxy that accepts connections from other machines (an "allow LAN" setting) exposes the host to
  them. Keep such a proxy bound to loopback, or do not run the host beside it.
- In Docker the peer-address check is off (connections arrive from Docker's network), so the
  loopback rule rests on the port being published on the same loopback address as `BIND_ADDRESS`.
  `docker run -p 7860:7860 -e BIND_ADDRESS=127.0.0.1`, a compose override that publishes the port on
  another address, or `network_mode: host` publish a host without a password to the network while
  the startup check passes. Use exactly the `-p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1` form
  above (or compose as shipped).

To serve other machines, deploy it as a cloud host.

## Deploy as a cloud host

A cloud host (`DEPLOY_MODE=cloud`) is one other machines reach: designers pair their Revit with it over
the WebSocket relay, and the admin password guards changing packs and skills, the logs and the device
list. It refuses to start without `ADMIN_PASSWORD`.

```bash
cp .env.example .env        # set DEPLOY_MODE=cloud and ADMIN_PASSWORD; optional: server-side model, device limit
docker compose up -d --build
```

The UI is on `http://127.0.0.1:7860`. Put a reverse proxy with TLS in front of it for anything
beyond localhost; WebSocket upgrades on `/api/v1/bridge/ws/` must be forwarded. A host behind a
reverse proxy or a tunnel is a cloud host even though its port is published on loopback, and must
run with `DEPLOY_MODE=cloud`. Local mode does not detect every proxy: one that adds no forwarding
header and sends `Host` with the loopback address (nginx's default `proxy_pass
http://127.0.0.1:7860;`) passes its checks, and the host would then serve pack edits, skill edits and
the logs without a password to anyone the proxy lets in. The only protections are setting
`DEPLOY_MODE=cloud` and the startup refusal of a local host that finds `ADMIN_PASSWORD` set.

Split deployment (SPA on a static host, API on another machine): build `frontend/` and upload
`dist/`, then place a static `config.json` next to its `index.html` - copy
[`frontend/public/config.example.json`](frontend/public/config.example.json) and set `apiBase` to the
API origin (and `wsBase` if the relay differs). The SPA reads that file at startup; the backend's own
`/config.json` only describes a same-origin setup. Set `CORS_ORIGINS` on the API side to the static
host's origin. Either way the SPA must be served at the root of its origin: a path-prefix deployment
(`https://example.com/revit/`) is not supported, because the built assets (`/assets/...`),
`/config.json` and the API calls (`/api/...` after `apiBase`) are absolute paths.

Connect a designer's Revit to the host in three steps: press **Pair a Revit** on the Connect page,
run the install command it shows on the designer's machine with Revit closed (it carries the pairing
code, which is good for ten minutes), and watch the device come up as *online*. An add-in that is
already installed can be paired by typing the same code into its settings window. A Revit running on
the Docker host itself is reached over TCP (`host.docker.internal:18080`) without any of that.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/revitbridge/revit-bridge-addin/main/installer/install.ps1))) -Mode remote -Server https://<your-host> -Pair XXXX-XXXX
```

The add-in supports Revit 2020, 2021 and 2023-2026, and each Revit year is its own installation. A
pairing code pairs one of them: on a machine with several Revit years the installer asks which year
to pair (add `-RevitVersion <year>` to name it), and a second year needs a second code.

A **device** is one add-in installation. The add-in holds its own token, the browser that paired it
holds a key; neither can stand in for the other, and revoking the device invalidates both and closes
its connection.

## Use

1. **Connect** - on a local host: check the add-in on this machine (*Check*: state, latency, project
   units) and fill in *Model* (OpenAI-compatible base URL, model name, API key). On a cloud host
   additionally: pair a Revit (the code, the install command, the countdown) and pick the one this
   host talks to: a paired device or the local add-in over TCP. *My devices* lists what this browser
   tab holds keys for, with each device's state, last activity, request count and *Revoke*; with the
   admin password there is a list of every device on the host. Model settings and device keys stay in
   the browser tab (`sessionStorage`) and travel as request headers; the server never stores them.
2. **Task** - the demo sequence. The page opens with a snapshot of the model (units, levels, type
   summary, selection, fingerprint). Describe what you want in one sentence: the model works through
   the package's tools - it reads the lessons for writing Revit code it needs (`get_guidance`), takes
   a snapshot, asks the questions a capability pack still has open (with the real levels and types,
   as buttons you can click) and reconciles a draft - then proposes
   a TaskSpec, which the page shows as a spec card: every parameter with its source and evidence,
   the readings it made as checkboxes, conflicts in red. A value the reconciliation cannot find is a
   question too; each question has a badge for who can answer it (*designer*, or *model fact*: check
   the model first) and lists in its tooltip what it holds up. It cannot confirm or execute anything.
   *Confirm* lights up only when the reconciliation is ready; it issues a one-time token that stays
   in the page's memory (never `localStorage`), and the host runs exactly what was confirmed. You
   see the validator's checks, the evidence id, and `validation_failed` in red when Revit ran the
   code but the model did not change as claimed. The tamper demo changes one confirmed value and
   runs again under the same token: `confirmation_invalid`, nothing reaches Revit. The result then
   goes back to the model for a faithful report, and code that worked can be solidified into a v1
   pack (on a cloud host with the admin password). The *Compare with no bridge* switch sends the same sentence a second time with
   `bridge: false` (no tools, no skills, nothing executable) and shows both answers side by side.
3. **Capabilities** - *Packs*: a saved pack with its version, validator and preconditions; load the
   real choices from the model (levels, types, elements), fill the parameters, and *Confirm and run*
   takes the same route as the task page (a spec card, a token, the validator, the ledger); edit or
   delete it (on a cloud host with the admin password). *Skills*: Markdown protocols that are appended to the model's system prompt - the
   package's `revit-core` (the contract every snippet follows) is on; the `revit-bridge` protocol
   (written for the MCP server's tools; this host's protocol is the package's `host_instructions()`),
   the add-in phase skills (`revit-task-brief`, `revit-addin-dev`, `revit-addin-test`,
   `revit-addin-release`), `revit-journal-analysis` (reading Revit journal files), the references
   and the lessons are listed but off (the model asks for lessons with `get_guidance`); add, upload
   or import more from GitHub (a cloud host asks for the admin password).
4. **Evidence** - the ledger of every execution through this host or the MCP server: who confirmed
   it, the token prefix, what ran, what the validator found. Expand a record for all of its fields;
   *Validate* re-runs its assertion against the model as it is now.

The system prompt is the package's `host_instructions()` (this host's protocol), the enabled skills
(by default only `revit-core`) and one line that sends the model to `get_guidance` for the lessons
(revit-core's and every other category's) - about 6 KB in all. The task page's "without the
bridge" comparison sends the same message with `bridge: false` (one plain prompt, no tools, no
skills).

Upgrading from 0.1: the 0.1 host copied the package's built-in packs into `data/capabilities/`,
which is now the package's own directory for user packs, where a file of the same name would hide
the wheel's pack. On its first start 0.2 moves every 0.1 copy (a pack file without
`schema_version`) to `data/capabilities/legacy-0.1/` and logs one line per file; nothing is
deleted. A pack you solidified with 0.1 lands there too: solidify it again from the Capabilities
page (its `code_template` and `parameters` are still in the file).

Development without Docker:

```bash
uv sync                                  # backend deps, revit-bridge included; installs the revit-bridge-web script
cd frontend && npm ci && npm run build   # or `npm run dev` with the proxy to :7860 (it presents its pages with the host's origin)
cd .. && uv run revit-bridge-web         # http://127.0.0.1:7860 (python -m backend.main is the same)
uv run pytest                            # backend tests, no Revit needed
```

## Configure

Everything is an environment variable (`.env` for Docker). The browser gets what it needs from
`/config.json`, so the same build runs on any address.

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOY_MODE` | `local` | `local`: this machine only. The host refuses to start when other machines could reach it (see `HOST` and `BIND_ADDRESS`) or when `ADMIN_PASSWORD` or `CORS_ORIGINS` is set, and answers only a browser on this machine talking to it directly (403 `local_mode_no_proxy`, `local_mode_host`, `local_mode_origin`; see *Start on your machine*); the relay, pairing and device routes are not there (they answer 404 `cloud_mode_only`), device headers are not read, and every admin check passes without a password. `cloud`: the relay, pairing, the device list and `ADMIN_PASSWORD`, which is then required (no start without it). `/config.json` reports it as `features.mode`. |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | unset | Server-side model defaults; headers `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` override them per request. The server's key is only ever sent to `LLM_BASE_URL`: a request that sends its own `X-LLM-Base-Url` must send `X-LLM-Key` too (400 otherwise), while a visitor's key or model name alone goes to the server's endpoint. The server model counts as configured (`/config.json` `serverModel`) only with all three set. Unset = visitors must bring their own. |
| `LLM_ALLOW_HTTP` | `0` | Allow browser-supplied `http://` endpoints and endpoints on loopback or private addresses (a model on the operator's own machine or network). Without it a browser-supplied endpoint must be `https://` on a public address: a literal loopback, private, link-local, multicast or reserved address, or a name that resolves to one, is refused with 400. The name is resolved once per request, which does not stop DNS rebinding. `LLM_BASE_URL` itself is never checked. The check is best-effort: a host on the public internet should also restrict the container's outbound traffic (egress) to what it needs. Redirects from a model endpoint are never followed. |
| `CHAT_RATE_LIMIT` | `30` | `/api/chat` and `/api/v1/bridge/spec/confirm` requests per minute per client IP (separate counters). |
| `ADMIN_PASSWORD` | unset | Cloud only, and required there. Checked as `X-Admin-Token` (the password entered on the Connect page) for changing capability packs (edit, delete, solidify), skill edits, the list of every device and `/api/logs`. A local host refuses to start when it is set (that looks like a cloud host missing `DEPLOY_MODE=cloud`, which would otherwise run without any password): there all of these work without a password. |
| `REVIT_BRIDGE_HOST`, `REVIT_BRIDGE_PORT`, `REVIT_BRIDGE_TOKEN`, `REVIT_BRIDGE_TIMEOUT` | `host.docker.internal`, `18080`, unset, `60` | Local add-in over TCP (read by the package). |
| `REVIT_BRIDGE_CONFIRM_TTL` | `600` | Seconds a token from `POST /api/v1/bridge/spec/confirm` stays redeemable (read by the package). |
| `MAX_DEVICES` | `20` | Cloud only (a local host logs it as unused). How many paired add-ins may be connected at once. Pairing creates the credentials; only their hashes are stored (`REVIT_BRIDGE_DATA_DIR/auth/devices.json`), so nothing is mounted. The 0.1 slot variables (`MAX_SLOTS`, `MCP_BRIDGE_REQUIRE_SLOT_TOKEN`, `MCP_BRIDGE_SLOT_TOKEN_*`) are gone and the host refuses to start if one is still set. |
| `DATA_DIR` | `/app/data` | This host's files: skills, interaction logs (`./data` volume). |
| `REVIT_BRIDGE_DATA_DIR` | `/app/data` | The package's data root: user capability packs (solidified, edited or hidden built-ins), `usage.json`, the evidence ledger (read by the package; same volume). The built-in packs are read from the wheel. |
| `REVIT_BRIDGE_CAPABILITIES_DIR`, `REVIT_BRIDGE_EVIDENCE_DIR` | `REVIT_BRIDGE_DATA_DIR/capabilities`, `REVIT_BRIDGE_DATA_DIR/evidence` | Package overrides for the user pack and evidence directories. Leave them unset in the container: a path outside `/app/data` is not in the volume and the entrypoint does not make it writable. |
| `SKILLS_DIR` | unset | Optional read-only directory that replaces the built-in skill *directory* (the wheel's `revit_bridge/skills/`, laid out as `<skill>/SKILL.md` + `references/`): mount a checkout with the same layout to keep the `builtin:revit-bridge/...` ids, or point it at an empty directory to list no built-in skills at all. Every file in a mounted directory is on unless its front matter says `enabled: false`. |
| `REVIT_BRIDGE_GUIDANCE_COMPANY`, `REVIT_BRIDGE_GUIDANCE_PROJECT` | unset | Extra guidance layers for `get_guidance`, as the MCP server reads them (directories inside the container: mount them). The personal layer is `REVIT_BRIDGE_DATA_DIR/guidance`. Loaded on first use and kept until the host restarts. |
| `PUBLIC_WS_BASE` | unset | Cloud only (a local host logs it as unused). Relay address shown in the add-in command when it differs from the page origin (dedicated WebSocket host name). |
| `CORS_ORIGINS` | unset | Cloud only: a local host answers its own origin only and refuses to start when it is set. Origins allowed to call the API cross-origin (split deployment), comma-separated. `*` allows any origin but without credentials; an explicit list allows credentials for its own origins. |
| `BIND_ADDRESS`, `PORT` | `127.0.0.1`, `7860` | Published host interface and port (`docker-compose.yml`). Without Docker, `PORT` (or `--port`) is the port the server listens on. Compose also passes `BIND_ADDRESS` into the container: there the server listens on `0.0.0.0`, so the published address is what a local host checks - it must be loopback. Running the image with `docker run` in local mode, publish on loopback and pass the same value: `-p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1`. |
| `HOST` | `127.0.0.1` local, `0.0.0.0` cloud | Interface the server listens on without Docker (or `--host`); a local host accepts loopback only. `docker-compose.yml` pins it (and the container port, 7860). |
| `REVIT_BRIDGE_WEB_CONTAINER` | `1` in the image | Set by the Dockerfile: a local host checks `BIND_ADDRESS` instead of `HOST`. Not for `.env`. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,172.16.0.0/12` in the image, `127.0.0.1` outside it | Proxies whose `X-Forwarded-*` headers are trusted for the client address (per-address rate limits, logs). The image default fits the documented setup: the port published on loopback and a reverse proxy on the Docker host, which reaches the container from the Docker bridge gateway. When `BIND_ADDRESS` is not loopback, set it to the proxy's address - or to `127.0.0.1` with no proxy in front - so a visitor cannot choose their own address. If the compose network is in `192.168.0.0/16` (`docker network inspect revit-bridge-web_default`), list its gateway instead. An explicit value always wins. |

## API v1

`/api/v1/bridge/*` is a thin HTTP surface over the package, exported to
[`docs/api-v1.json`](docs/api-v1.json) (`/openapi.json` on a running host). The routes follow the
package's flow: `GET /snapshot` and `POST /query` read the model; `GET /tools`,
`POST /tools/{name}/missing-params` and `POST /spec/reconcile` prepare a TaskSpec; `POST /spec/confirm`
turns the designer's confirmation into a one-time token; `POST /tools/{name}/run` and `POST /execute`
run only with that token (sandbox review, preconditions, validator and the evidence ledger are the
package's; the ledger records `host: "web"`); `GET /evidence` and `POST /evidence/{id}/validate` show
and re-check what ran. A pack's code runs in a designer's Revit once they confirm its card, so the
routes that change packs - `PUT` and `DELETE /tools/{name}`, `POST /solidify` - take the admin
password as `X-Admin-Token` on a cloud host (403 `admin_required` without the right one; the 503
`admin_disabled` of a host without `ADMIN_PASSWORD` stays in the contract, but a cloud host no longer
starts without it); on a local host they, the skill writes and `/api/logs` need no token. Reading,
choosing, confirming and running a pack never do. A request the route refuses as such is 400, a body that parsed but is not
valid 422, a Revit that cannot be reached before the run is 503 (a transport failure during the run
is the package's `success: false` without consuming the token), and a refusal by the gate, a failed
precondition or a failed validation is 200 with `success: false`; error bodies are
`{error, message?, ...}` with the MCP tools' codes, declared per route in the export.

A local host puts one guard in front of every route (not in the export, which is a cloud host's): 403
`local_mode_no_proxy` for a request carrying a proxy forwarding header, 403 `local_mode_host` for a
`Host` other than `127.0.0.1`, `localhost` or `[::1]` (or a peer that is not this machine, outside
Docker), 403 `local_mode_origin` for any request with `Sec-Fetch-Site: cross-site` or `same-site`
(except a top-level navigation to the page shell or a static file, never to `/api/...`) and
for a POST, PUT, PATCH, DELETE or WebSocket upgrade whose `Origin` is not the host's own (`http://`
plus its `Host`); a request with neither header (curl on this machine) is not refused for that.

Pairing and devices exist on a cloud host only; on a local host `/devices*` and `/ws/{device_id}`
answer 404 `{"error": "cloud_mode_only"}` (the add-in socket before the upgrade), `X-Device-Id` /
`X-Device-Key` are ignored and every request drives the local add-in, and `GET /slots` reports
`{max_devices: 0, connected: 0}`. The export in `docs/api-v1.json` is a cloud host's, every route
present. On a cloud host: `POST /devices/pair` returns `{code, device_id, expires_at, browser_key,
install_command}`; the add-in calls `POST /devices/redeem` with the code (no headers) and receives
`{device_id, device_token, ws_url}`; `GET /devices/{id}` and `DELETE /devices/{id}` take the browser
key (or the admin password), `GET /devices` is admin only, and `GET /slots` publishes
`{max_devices, connected}`. When the device store (`auth/devices.json`) cannot be read for a moment,
pair, redeem and revoke answer 503 `device_store_unavailable`: nothing changed (a code is not
consumed), so the same call can simply be repeated. Pick a Revit with `X-Device-Id` + `X-Device-Key`; without them a request
drives the local add-in over TCP. Every confirmation and every ledger line belongs to that device
(`scope`), so a browser sees and re-validates only its own device's evidence, and a token confirmed
for one device is refused on another. Ad-hoc code (`POST /execute`) also asks the designer on the
device itself; a No there is `success: false, error: "declined_on_device"`.

`POST /api/chat` is the host loop: `{message, session_id, bridge?}` streams one turn over SSE -
reply text as `data: "<token>"` frames, `event: spec` (`{spec, card, errors, reconcile}`) when the
model proposed a spec, `event: done`. The model's tools are the package's read-only ones
(`get_project_snapshot`, `query`, `list_tools`, `get_tool_choices`, `missing_params`, `reconcile`,
`get_guidance`) plus `propose_spec`; it has no `confirm_spec`, `run_tool` or `execute_code`.
`get_guidance` is the package's lesson selection called in process (no MCP server), with the MCP
tool's arguments and JSON reply; its description is the MCP tool's followed by one note for this
host, which has no `check_code` (after a failed execution: `stage=fix` with the symbols you suspect).
The session keeps a tool result up to 8 KB after the turn that produced it (head plus a one-line
note), a `get_guidance` reply up to 16 KB, so the orient lessons reach later turns whole. After the
page ran something, `{execution: <ExecutionResult>, session_id}` (instead of `message`) hands the result back:
the turn starts with `event: execution` and the model reports what the validator found. Exactly one
of `message` / `execution` per request (422 `invalid_args` otherwise); with `bridge: false` there are
no tools, no skills and no execution feedback (tool calls a model sends anyway are dropped, never
run - as in the last round of a turn, which goes out without tools).

Secrets never enter this repository: `.env`, `.secrets/` and `*.token` are ignored; a device's token
and browser key exist in plain text only in the one response that creates them (the store keeps
hashes), and the model key comes only from request headers or the environment.

## License

MIT - see [LICENSE](LICENSE).
