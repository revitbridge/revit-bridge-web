# revit-bridge-web

Deploy-anywhere demo host for [revit-bridge](https://github.com/revitbridge/revit-bridge): it serves the
web UI, talks to the Revit add-in on your machine and streams a model you bring yourself; deployed as a
cloud host it also relays remote Revit add-ins. The host contains no Revit logic of its own - every
query, execution and capability pack goes through the `revit_bridge` package.

设计师与 AI 之间的意图桥演示站：连接 Revit、用自带模型从已保存的工具里选工具并填参数、确认后执行；对话里模型不写代码，新工具在工具台里检查、试跑、存成工具。

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

**Your tools.** The capability packs you save (your tools) live in the `revit_bridge` package's user
capabilities directory. Started with `uv run revit-bridge-web`, the host sets nothing: that is the
package's per-user directory (`%LOCALAPPDATA%\revit-bridge\capabilities` on Windows,
`~/.local/share/revit-bridge/capabilities` elsewhere), the same one the package's MCP server uses when
Claude Code or another coding framework runs it on this machine - a tool saved there shows up here on
the next request, and one saved here is a file the MCP server reads, no restart on either side.
`docker compose` keeps them in `./data/capabilities` instead; to share them with the MCP server,
uncomment the two lines in `docker-compose.yml` that mount the machine's directory
(`SHARED_CAPABILITIES_DIR` in `.env`) at `/shared/capabilities` and point
`REVIT_BRIDGE_CAPABILITIES_DIR` there.

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
   units), see *Models by role* (the model name of the analyst, the planner and the supervisor, and
   whether they come from the host's models file or from the environment and this browser; read-only)
   and fill in *Model* (OpenAI-compatible base URL, model name, API key). On a cloud host
   additionally: pair a Revit (the code, the install command, the countdown) and pick the one this
   host talks to: a paired device or the local add-in over TCP. *My devices* lists what this browser
   tab holds keys for, with each device's state, last activity, request count and *Revoke*; with the
   admin password there is a list of every device on the host. Model settings and device keys stay in
   the browser tab (`sessionStorage`) and travel as request headers; the server never stores them.
2. **Task** - the demo sequence. The page opens with a snapshot of the model (units, levels, type
   summary, selection, fingerprint). Describe what you want in one sentence: the model works through
   the package's tools - it picks a saved tool (a capability pack) from the list, takes a snapshot,
   asks the questions the pack still has open (with the real levels and types, as buttons you can
   click) and reconciles a draft - then proposes
   a TaskSpec, which the page shows as a spec card: every parameter with its source and evidence,
   the readings it made as checkboxes, conflicts in red. A value the reconciliation cannot find is a
   question too; each question has a badge for who can answer it (*designer*, or *model fact*: check
   the model first) and lists in its tooltip what it holds up. It cannot confirm or execute anything.
   *Confirm* lights up only when the reconciliation is ready; it issues a one-time token that stays
   in the page's memory (never `localStorage`), and the host runs exactly what was confirmed. You
   see the validator's checks, the evidence id, and `validation_failed` in red when Revit ran the
   code but the model did not change as claimed. The tamper demo changes one confirmed value and
   runs again under the same token: `confirmation_invalid`, nothing reaches Revit. The result then
   goes back to the model for a faithful report. **The model here never writes code**: it has no
   lessons for writing it and no way to propose it (an `execute_code` spec is refused and shows no
   card), so it runs saved tools only. When no saved tool fits, it says so, describes the tool you
   would need in one sentence and links the workbench (`/capabilities/workbench`), where a person
   tries code and saves it as a tool - code is written in the coding frameworks (Claude Code and
   others, with the `revit-bridge` plugin's skills). **Type `@` to pick from the open model:**
   `@level`, `@type` (a category first, then its types) or `@link` (a link that is not loaded is
   listed but cannot be picked); keep typing to narrow the list, arrows and Enter to choose, Esc to
   close. A picked item is one tag - Backspace removes it whole. An `@` right after a letter or a
   digit (an e-mail address) opens nothing; after a space, punctuation or a Chinese character it
   does. The host checks the picks against the model before the model is called and gives the model
   the ones it found, with their names as Revit has them, so it does not ask for them again; under
   the sent message each pick says what happened to it (checked, not found, not loaded, or not
   checked when Revit was not connected), and one found gone is struck through when it is in the
   box again. Each item goes once, at most 20 different items per message: with more the message
   stays in the box and the page says why. When Revit is not connected the list says so and Enter
   still sends; a list that does not come within 10 seconds gives up the same way. *Confirm* reads
   the picks of the card again: one deleted or renamed in Revit since is refused, and the model
   proposes again. The *Compare with no bridge* switch sends the same sentence a second time with
   `bridge: false` (no tools, no skills, nothing executable) and shows both answers side by side.
   Above each reply of the bridge transcript is its tool activity: one row per tool call the model
   made (name, arguments, status, time, result size in characters; open a row for the full
   arguments - the first 2000 characters and the size when they run past 8000 - and the first
   2000 characters of the result; errors in red). A row or stage still open when the turn's
   stream ended (stopped, broken off) shows as *interrupted*. The rows are grouped by role, each
   group headed by the role and the model it ran on (see *The chat's roles* below): the analyst
   (no tools), then the planner, or the supervisor after a run. A small-talk or "no tool" reply is
   the analyst's own, and after a run the supervisor's report is the reply. When the supervisor
   sends the task back, the planner's new card comes in the same reply, and above it, as *Previous
   attempt*, what the page itself received from the run (succeeded, failed with its error, or the
   validator's checks that did not pass) - the model's report is never the only record. Below the chat, the
   *Tokens* panel counts this turn and this session: input (cached tokens included), cache read,
   hit rate, cache write, output and the number of model calls, one row per role that ran and the
   total (the planner alone, with `LLM_SINGLE_MODEL=1`, has the total only). A number the provider
   did not report shows as `-` and makes every total it is part of unknown - never 0. No
   prices. The no-bridge comparison has its own panel.
3. **Capabilities** - *Packs*: a saved pack with its version, validator and preconditions; load the
   real choices from the model (levels, types, elements), fill the parameters, and *Confirm and run*
   takes the same route as the task page (a spec card, a token, the validator, the ledger); edit or
   delete your own (on a cloud host with the admin password). The built-in packs from the
   `revit_bridge` wheel are read-only everywhere: never edited, hidden or replaced (export one and
   import it under another name to make your own). *Workbench* (`/capabilities/workbench`): where code
   becomes a tool. Paste a snippet written in a coding framework (or write a small change), declare
   its parameters (`{name}` placeholders in the code, as in a pack), *Check* it (compile errors by
   line), *Dry run* it against the open model (the values first, real options for levels, types and
   elements; it shows what would be added, modified and deleted and the return value, and is always
   rolled back) and *Save as tool* - only after a successful dry run of that same code and those
   declarations, and it asks before replacing a tool of the same name. A value field left empty
   for a parameter with a default sends no value: the tool uses its default, and the field and the
   spec card say "default X will be used". A parameter with a default is never required. Saving an
   opened tool keeps what the workbench does not show but the save can carry (the source query,
   each parameter's own source such as floor types); what it cannot carry - preconditions,
   applies_when, not_for, the display name, the version (saved again as 1.0.0), and revit_versions,
   fixtures and the approval if the file has them - is named before you replace the tool, and
   needs its own click. *Run* runs the saved tool through the spec card and the confirmation, as
   *Packs* does; a draft that differs from the saved tool (code, parameters or validator) must be
   saved first. The card goes as soon as the draft or a value changes, *Confirm* reads the saved
   tool again and runs nothing if it changed meanwhile, and Run waits while a run is in progress.
   Below the editor: your own tools (with the directory they are in) to
   open, *Export* (the pack file), *Delete* and *Import* (a pack file; it asks before replacing), and
   the built-in ones, read-only (open to read or copy as yours, export). On a cloud host saving,
   deleting and importing need the admin password. *Skills*: the Markdown protocols of the package
   (`revit-core`, the `revit-bridge` protocol, the add-in phase skills, `revit-journal-analysis`,
   references and lessons) and your own; list, read, add, upload or import from GitHub (a cloud host
   asks for the admin password). They are guidance for writing code and no longer enter the chat's
   prompt.
4. **Evidence** - the ledger of every execution through this host or the MCP server: who confirmed
   it, the token prefix, what ran, what the validator found. Expand a record for all of its fields;
   *Validate* re-runs its assertion against the model as it is now.
5. **Token** (`/token`) - the measured runs: every chat sent with an `X-Run-Label` header (the token
   bench sends one per run) is counted under that label. One line per run - time, model calls,
   input, cache read, cache write, output, time - and one per role and model under it. Pick two runs
   to see them side by side with the difference (B - A) of each count, in total and per role. A
   count the provider did not report shows as `-`, and so does every total and difference it is
   part of. Read-only; on a cloud host it asks for the admin password (as `/api/logs` does).

The chat runs in three roles, each with its own prompt (see *The chat's roles* under
*Configure*): the analyst's is 1.8 KB, the planner's 3.5 KB with up to seven tool definitions
(4 KB; two, 1.6 KB, for a read), the supervisor's 1.6 KB with one (1 KB). With
`LLM_SINGLE_MODEL=1` it is one loop, as before the split: its system prompt is the package's host
instructions for a host whose model writes no code
(`host_instructions(code=False, mentions=True)`: no `execute_code`, snippet contract or
`get_guidance`; saved tools only; the rule for the designer's `@` selections) and one line of this
host's own: when no tool fits, link the workbench - about 3.7 KB, with seven tool definitions of
about 4 KB (`query` names the kinds `links` and `type_categories` and `args.link_id`, in the
package's MCP tool's words). Before: 4.3 KB with the package's full text and a host paragraph that
overrode its code parts, and 3.4 KB of tools; 6 KB and eight tools, 11.2 KB in all, while skills
and `get_guidance` were part of it.
The task page's "without the bridge" comparison sends the same message with `bridge: false`
(one plain prompt, no tools). A session keeps the system prompt of its first turn, so every call
starts with the same system message and tool definitions and a provider's prompt cache can serve
them.

Upgrading from 0.1: the 0.1 host copied the package's built-in packs into `data/capabilities/`,
which is now the package's own directory for user packs, where a file of the same name would hide
the wheel's pack. On its first start 0.2 moves every 0.1 copy (a pack file without
`schema_version`) to `data/capabilities/legacy-0.1/` and logs one line per file; nothing is
deleted. A pack you solidified with 0.1 lands there too: write it out as a v1 pack (its
`code_template` and `parameters` are still in the file) and import it on the workbench. Only the
host's own `DATA_DIR/capabilities` is repaired this way: a user pack directory shared with other
hosts (the package's per-user directory, or a mounted one) is never touched at startup.

Development without Docker:

```bash
uv sync                                  # backend deps, revit-bridge included; installs the revit-bridge-web script
cd frontend && npm ci && npm run build   # or `npm run dev` with the proxy to :7860 (it presents its pages with the host's origin)
cd .. && uv run revit-bridge-web         # http://127.0.0.1:7860 (python -m backend.main is the same)
uv run pytest                            # backend tests, no Revit needed
```

## Configure

Everything is an environment variable (`.env` for Docker), except the model of each role, which may
come from one file, `models.yml` (see *Models by role* below). The browser gets what it needs from
`/config.json`, so the same build runs on any address.

| Variable | Default | Purpose |
|---|---|---|
| `DEPLOY_MODE` | `local` | `local`: this machine only. The host refuses to start when other machines could reach it (see `HOST` and `BIND_ADDRESS`) or when `ADMIN_PASSWORD` or `CORS_ORIGINS` is set, and answers only a browser on this machine talking to it directly (403 `local_mode_no_proxy`, `local_mode_host`, `local_mode_origin`; see *Start on your machine*); the relay, pairing and device routes are not there (they answer 404 `cloud_mode_only`), device headers are not read, and every admin check passes without a password. `cloud`: the relay, pairing, the device list and `ADMIN_PASSWORD`, which is then required (no start without it). `/config.json` reports it as `features.mode`. |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | unset | Server-side model defaults; headers `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` override them per request. The server's key is only ever sent to `LLM_BASE_URL`: a request that sends its own `X-LLM-Base-Url` must send `X-LLM-Key` too (400 otherwise), while a visitor's key or model name alone goes to the server's endpoint. The server model counts as configured (`/config.json` `serverModel`) only with all three set. Unset = visitors must bring their own. |
| `LLM_SINGLE_MODEL` | `0` | `1`: the chat runs as one model loop - the planner's model with the whole host text and all seven tools, exactly as before the roles (the comparison baseline and the fallback). Unset or `0`: the three roles (see *The chat's roles*). `/config.json` reports it as `features.roles` (`false` with `1`). |
| `ROLES_PLANNER_TOOLS` | `route` | With the roles: `route` - the planner gets, and may call, the tools of the analyst's route only; `all` - all seven on every route, every one runs (the tool definitions then stay the same for a provider's prompt cache). Any other value refuses the start. For measuring both. |
| `ROLES_ANALYST` | `on` | With the roles: `off` sends every message straight to the planner with route `tool` (all seven tools); the supervisor still reports runs - planner and supervisor only, to measure whether the analyst pays for itself. Any other value refuses the start. |
| `MODELS_FILE` | `DATA_DIR/models.yml` | The models file: an endpoint, a model and the name of the key's variable per entry, an entry per role (see *Models by role*). No file at the default path: `LLM_*` and the headers, as before. Set it only to read another path; the host then refuses to start when there is no file there, as it does for any file that is not valid. `/config.json` reports each role's model name (`features.models`) and whether the file set them (`features.modelsFile`). |
| `LLM_ALLOW_HTTP` | `0` | Allow browser-supplied `http://` endpoints and endpoints on loopback or private addresses (a model on the operator's own machine or network). Without it a browser-supplied endpoint must be `https://` on a public address: a literal loopback, private, link-local, multicast or reserved address, or a name that resolves to one, is refused with 400. The name is resolved once per request, which does not stop DNS rebinding. `LLM_BASE_URL` itself is never checked. In the models file it lets an entry's `base_url` be `http://` to any host (`http://` to loopback is allowed there without it). The check is best-effort: a host on the public internet should also restrict the container's outbound traffic (egress) to what it needs. Redirects from a model endpoint are never followed. |
| `CHAT_RATE_LIMIT` | `30` | `/api/chat` and `/api/v1/bridge/spec/confirm` requests per minute per client IP (separate counters); on a cloud host `/api/v1/bridge/check` and `/dry-run` count against `/spec/confirm`'s. |
| `ADMIN_PASSWORD` | unset | Cloud only, and required there. Checked as `X-Admin-Token` (the password entered on the Connect page) for changing capability packs (edit, delete, solidify, import), skill edits, the list of every device and `/api/logs`. A local host refuses to start when it is set (that looks like a cloud host missing `DEPLOY_MODE=cloud`, which would otherwise run without any password): there all of these work without a password. |
| `REVIT_BRIDGE_HOST`, `REVIT_BRIDGE_PORT`, `REVIT_BRIDGE_TOKEN`, `REVIT_BRIDGE_TIMEOUT` | `host.docker.internal`, `18080`, unset, `60` | Local add-in over TCP (read by the package). |
| `REVIT_BRIDGE_CONFIRM_TTL` | `600` | Seconds a token from `POST /api/v1/bridge/spec/confirm` stays redeemable (read by the package). |
| `MAX_DEVICES` | `20` | Cloud only (a local host logs it as unused). How many paired add-ins may be connected at once. Pairing creates the credentials; only their hashes are stored (`REVIT_BRIDGE_DATA_DIR/auth/devices.json`), so nothing is mounted. The 0.1 slot variables (`MAX_SLOTS`, `MCP_BRIDGE_REQUIRE_SLOT_TOKEN`, `MCP_BRIDGE_SLOT_TOKEN_*`) are gone and the host refuses to start if one is still set. |
| `DATA_DIR` | `/app/data` | This host's files: skills, interaction logs (`./data` volume). |
| `REVIT_BRIDGE_DATA_DIR` | `/app/data` in the image; unset outside it | The package's data root: user capability packs (your tools), `usage.json`, the evidence ledger (read by the package). In the image it is the `./data` volume. Without a container leave it unset: the package then uses its per-user directory (`%LOCALAPPDATA%\revit-bridge` on Windows, `~/.local/share/revit-bridge` elsewhere), the one its MCP server uses, so the tools are shared. The built-in packs are read from the wheel. |
| `REVIT_BRIDGE_CAPABILITIES_DIR`, `REVIT_BRIDGE_EVIDENCE_DIR` | `REVIT_BRIDGE_DATA_DIR/capabilities`, `REVIT_BRIDGE_DATA_DIR/evidence` | Package overrides for the user pack and evidence directories. In the container leave them unset (a path outside `/app/data` is not in the volume and the entrypoint does not make it writable), except `REVIT_BRIDGE_CAPABILITIES_DIR=/shared/capabilities` with the commented mount in `docker-compose.yml`, which shares the machine's tool directory (`SHARED_CAPABILITIES_DIR`) with the MCP server; the entrypoint leaves that directory's owner as it is, so on Linux the app (uid 1000) needs write access to it. |
| `SKILLS_DIR` | unset | Optional read-only directory that replaces the built-in skill *directory* (the wheel's `revit_bridge/skills/`, laid out as `<skill>/SKILL.md` + `references/`): mount a checkout with the same layout to keep the `builtin:revit-bridge/...` ids, or point it at an empty directory to list no built-in skills at all. Every file in a mounted directory is on unless its front matter says `enabled: false`. |
| `REVIT_BRIDGE_GUIDANCE_COMPANY`, `REVIT_BRIDGE_GUIDANCE_PROJECT` | unset | Extra guidance layers for the lessons the workbench's compile check (`POST /check`) adds to its reply, as the MCP server reads them (directories inside the container: mount them). The personal layer is `REVIT_BRIDGE_DATA_DIR/guidance`. Loaded on first use and kept until the host restarts. |
| `PUBLIC_WS_BASE` | unset | Cloud only (a local host logs it as unused). Relay address shown in the add-in command when it differs from the page origin (dedicated WebSocket host name). |
| `CORS_ORIGINS` | unset | Cloud only: a local host answers its own origin only and refuses to start when it is set. Origins allowed to call the API cross-origin (split deployment), comma-separated. `*` allows any origin but without credentials; an explicit list allows credentials for its own origins. |
| `BIND_ADDRESS`, `PORT` | `127.0.0.1`, `7860` | Published host interface and port (`docker-compose.yml`). Without Docker, `PORT` (or `--port`) is the port the server listens on. Compose also passes `BIND_ADDRESS` into the container: there the server listens on `0.0.0.0`, so the published address is what a local host checks - it must be loopback. Running the image with `docker run` in local mode, publish on loopback and pass the same value: `-p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1`. |
| `HOST` | `127.0.0.1` local, `0.0.0.0` cloud | Interface the server listens on without Docker (or `--host`); a local host accepts loopback only. `docker-compose.yml` pins it (and the container port, 7860). |
| `REVIT_BRIDGE_WEB_CONTAINER` | `1` in the image | Set by the Dockerfile: a local host checks `BIND_ADDRESS` instead of `HOST`. Not for `.env`. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,172.16.0.0/12` in the image, `127.0.0.1` outside it | Proxies whose `X-Forwarded-*` headers are trusted for the client address (per-address rate limits, logs). The image default fits the documented setup: the port published on loopback and a reverse proxy on the Docker host, which reaches the container from the Docker bridge gateway. When `BIND_ADDRESS` is not loopback, set it to the proxy's address - or to `127.0.0.1` with no proxy in front - so a visitor cannot choose their own address. If the compose network is in `192.168.0.0/16` (`docker network inspect revit-bridge-web_default`), list its gateway instead. An explicit value always wins. |

### The chat's roles

By default the chat splits each request into three roles, each with its own instructions (the
package's role texts, `host_instructions(role=..., code=False)`), its own tools and its own model
(see *Models by role*):

| Role | Does | Sees | Tools |
|---|---|---|---|
| analyst | routes the message: `chat` (talk, answer), `query` (read the model), `tool` (use a saved tool) or `none` (no tool does it); lists the facts still needed | the message with its `@` selections, a digest of the open model (the package's `snapshot_digest`, at most 1200 characters), the tool list (a name and one line each), the open task (its route and tool and the last message you got) | none |
| planner | reads the model, picks the tool, fills the parameters, asks, proposes the spec | the session's conversation, each message with the analyst's routing after it, its own tool results | `query` route: `query`, `get_project_snapshot`; `tool` route: those and `list_tools`, `get_tool_choices`, `missing_params`, `reconcile`, `propose_spec` |
| supervisor | reports a run truthfully; on a failure sends the task back to the planner or asks you | the spec the page confirmed for this run (or, when it confirmed none in this session, the planner's last proposal, labelled as proposed), a summary of the execution result, the validator's checks (the failed ones first), how often the task went back | `query` (read-only) |

The analyst answers one JSON object; with `chat` or `none` its `note` is the reply (with `none`
the host adds the workbench link when the note lacks it) and nothing else runs. With `query` or
`tool` the planner runs as the chat did before, with that route's tools only; a call of any other
tool is answered `tool_not_offered` and never run. Confirmation and execution stay on the page.
When the page reports a run, the supervisor answers one JSON verdict: `done`, `ask` (its report and
question are the reply) or `retry` (its report is shown and the planner proposes again in the same
reply with the supervisor's fix - a new card to confirm; a task goes back at most twice in a row,
then the host asks you; your next message starts the count again). The host holds the verdict to
the package's, reading the raw result: a run whose validator did not pass, or that Revit did not
report as a success, is never `done` (the host shows the validator's own checks and asks); a run
that succeeded with a validator that did not fail is not sent back (you decide); and a refusal by
nature is never sent back - `refused: true` (revit-bridge 0.9.1: the tool refused the input in
Revit and nothing was kept), a failed precondition, a validator that could not take its first
sample, blocked code, or a failure with no ledger line (refused before it ran, or Revit not
reached). A role whose answer is not its JSON gets one repair request; after that the analyst's
message goes to the whole planner (route `tool`) and the supervisor's report is the host's summary
of the raw result. An execution result may be 4 MiB: what the supervisor, the session and the
host's own reports get of it is cut - each text to 1000 characters, lists to 20 items, a check's
detail to 300, the checks to 30 (the host's report lists 20), `result` to 2000 - and the
supervisor's whole message to 16 KB, as the one loop's. The open task is the one a `tool` routing
opened: a `tool` routing for the same tool continues it, one for another tool replaces it, a run
reported `done` closes it; reads and small talk leave it as it is. The digest is read once per
session and device (one snapshot, no model call; when Revit cannot be read, not again for a minute)
and again after each reported run - so changes made in Revit outside the chat reach the analyst only
after a reported run, while the planner always reads the model itself; a new task on the page is a
new session. Roles pass these hand-offs only - the
planner never sees the analyst's prompt, list or digest, and the supervisor never sees the
planner's transcript - and each role's prompt and tool definitions stay the same within a session
(per route for the planner), so a provider's prompt cache can serve them. Every model call is one
`usage` event and one `llm_calls` row with its role and model. `CHAT_RATE_LIMIT` counts requests to
`/api/chat` (your messages and reported runs), not model calls.

The split costs one more model call per message (the analyst) and one round trip more before the
planner starts; it saves where the analyst answers alone (small talk, no tool), on reads (two tool
definitions instead of seven) and on reports (a short supervisor prompt instead of the whole
conversation). In the test suite's scripted flows, with the same model answers, the request bytes
(one loop -> roles) were: a tool task with a question, an answer and the report 66.6 KB -> 64.7 KB
(each of the two message turns about 3.3 KB more, the report 9.2 KB less); a read 16.0 KB -> 14.6
KB; small talk 7.8 KB -> 3.2 KB; "no tool" 22.3 KB -> 3.2 KB. With all seven tools for the planner
the read is 19.2 KB; without the analyst the tool task is 57.7 KB and the others about the one
loop's (16.0, 7.8, 22.2 KB). Real models do not follow a script:
measure with the token bench (`python -m bench`; it records which way the host ran). A tool task
the designer never runs pays the analyst calls without the report's saving.

`LLM_SINGLE_MODEL=1` runs the chat as one model loop instead (the planner's model, the whole host
text, all seven tools), request for request and event for event as before the split - the
comparison baseline and the fallback. Without the bridge (`bridge: false`) there are no roles. Two
more switches exist to measure variants of the roles: `ROLES_PLANNER_TOOLS=all` gives the planner
all seven tools on every route, and `ROLES_ANALYST=off` leaves the analyst out (planner and
supervisor). The interaction log (`/api/logs`) names each role that ran in a turn and its model.

### Models by role (`models.yml`)

Without a models file every role uses `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` and the
browser's model settings, exactly as before - "one model for everything". With one, each role has
its own entry:

```yaml
models:
  main:  { base_url: https://api.example.com/v1, model: some-model, api_key_env: LLM_API_KEY }
  small: { base_url: https://api.example.com/v1, model: some-small-model, api_key_env: LLM_API_KEY }
roles:
  analyst: small
  planner: main
  supervisor: small
```

- **Where**: `models.yml` in `DATA_DIR` - with Docker, `./data/models.yml` next to
  `docker-compose.yml` - or the path in `MODELS_FILE`. Copy `models.example.yml` to start. The file is
  read once, at startup: restart the host after changing it.
- **Keys stay in the environment.** `api_key_env` is the *name* of the environment variable that
  holds the key (capital letters, digits and `_`), read on every model call: put `LLM_API_KEY=...`
  or `OTHER_API_KEY=...` in `.env`, never in the file. A variable that is not set does not stop the
  start; the browser can then bring its own key (`X-LLM-Key`), and without one the chat answers 400
  as it does without `LLM_API_KEY`.
- **An entry** is `base_url` (`https://`, or `http://` to `127.0.0.1` / `localhost` - to any host
  only with `LLM_ALLOW_HTTP=1`; no user name, password or query), `model`, `api_key_env`, and optionally `max_tokens` and `temperature`
  (0 to 2). Nothing else: `api_key`, `key`, `token`, `extra_headers` or any other field refuses
  the start, as do YAML aliases and a key given twice. The message names the file, the field and
  the problem, never a value from the file.
- **Roles** are `analyst`, `planner` and `supervisor`. `planner` is required; a role left out uses
  the planner's entry.
- **The browser's model settings** (`X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key`) keep their
  meaning: they replace the planner's entry for that browser's requests, field by field as they
  replace `LLM_*` without the file (a base URL of the browser's own needs its own key, and must be
  https on a public address unless `LLM_ALLOW_HTTP=1`). A role on another entry is not affected.
  An entry's `max_tokens` / `temperature` apply only while its own endpoint and model are used.
- **Base URLs** in the file are the operator's settings, treated like `LLM_BASE_URL` (which the host
  never checks against private addresses); only a browser-supplied URL is. `http://` to another
  machine (a local model elsewhere on the network) needs `LLM_ALLOW_HTTP=1`, the same switch with the
  same meaning as for the browser's endpoint.

One model for everything - every role on one entry:

```yaml
models:
  main: { base_url: https://api.example.com/v1, model: some-model, api_key_env: LLM_API_KEY }
roles:
  planner: main
```

Mixed - a cheaper model for sorting and reporting, another provider for the planner, each key in
its own variable:

```yaml
models:
  strong: { base_url: https://api.example.com/v1, model: some-model, api_key_env: EXAMPLE_API_KEY }
  cheap:  { base_url: https://api.example.net/v1, model: some-small-model, api_key_env: OTHER_API_KEY, max_tokens: 1024 }
roles:
  analyst: cheap
  planner: strong
  supervisor: cheap
```

With Docker the default path is in the `./data` volume, so placing the file there is enough. To keep
it read-only and outside the volume, `docker-compose.yml` has two commented lines (a `:ro` mount at
`/app/config/models.yml` and `MODELS_FILE` pointing there); a read-only file mounted inside `/app/data`
would stop the entrypoint, which takes ownership of everything there.

## API v1

`/api/v1/bridge/*` is a thin HTTP surface over the package, exported to
[`docs/api-v1.json`](docs/api-v1.json) (`/openapi.json` on a running host). The routes follow the
package's flow: `GET /snapshot` and `POST /query` read the model; `GET /tools`,
`POST /tools/{name}/missing-params` and `POST /spec/reconcile` prepare a TaskSpec; `POST /spec/confirm`
turns the designer's confirmation into a one-time token; `POST /tools/{name}/run` and `POST /execute`
run only with that token (sandbox review, preconditions, validator and the evidence ledger are the
package's; the ledger records `host: "web"`); `GET /evidence` and `POST /evidence/{id}/validate` show
and re-check what ran. A pack's code runs in a designer's Revit once they confirm its card, so the
routes that change packs - `PUT` and `DELETE /tools/{name}`, `POST /solidify`, `POST /tools/import` -
take the admin password as `X-Admin-Token` on a cloud host (403 `admin_required` without the right one; the 503
`admin_disabled` of a host without `ADMIN_PASSWORD` stays in the contract, but a cloud host no longer
starts without it); on a local host they, the skill writes and `/api/logs` need no token. Reading,
choosing, confirming and running a pack never do. A request the route refuses as such is 400, a body that parsed but is not
valid 422, a Revit that cannot be reached before the run is 503 (a transport failure during the run
is the package's `success: false` without consuming the token), and a refusal by the gate, a failed
precondition or a failed validation is 200 with `success: false`; error bodies are
`{error, message?, ...}` with the MCP tools' codes, declared per route in the export.

The chat's `@` picker: `GET /mentions?kind=level|type|link&category=&q=&limit=` is the package's
`mention_candidates` reply as it is - `{kind, items: [{kind, id, name, label, detail, category?,
family?, disabled?, reason?}], total, truncated, warnings?}`; `type` without `category` lists the
categories (items of kind `category` whose `id` is the OST_* name), `q` keeps what contains it,
`limit` is 1-200 (default 50). A kind or category the package refuses is 422 (`unknown_kind`,
`invalid_args`), Revit reporting an error 200 with the package's `error` (as on `/query`), no Revit
503 `revit_unreachable`; the device rules are those of every read route. A spec may bind the
designer's picks by name with evidence `mention:<kind>#<id>` (the package's rule); before such a
spec is accepted, reconciled (the model's `reconcile` too) or confirmed the host checks every
binding whose evidence starts with `mention` against the picks the package verified in that chat
session for the device the request drives: the evidence exactly `mention:<level|type|link>#<id>`
(lower case, no spaces), the kind the parameter takes as its pack declares it (`levels`: a level;
`floor_types`, `family_types[:OST_*]`: a type, of that category when both name one; a link only
where the pack names neither), the value (the name the package's block shows: a level's or a
type's name, a link's label or else its name) and the source (`tool` for a pack parameter whose
value comes from the model, else `designer`); any other is `unknown_mention` naming the parameter.
So `POST /spec/reconcile` and `POST /spec/confirm` take the chat's `session_id` (the
`X-Session-Id` of `/api/chat`); a binding to a pick with no session, another session's or another
device's is 422 `invalid_spec` with those `errors`. `POST /spec/confirm` also reads the bound picks
again from the open model before it issues a token (one `resolve_mentions`): a pick gone or
renamed since is `unknown_mention` too (the session takes the new reading and lets the gone one
go); no Revit to ask is 503 `revit_unreachable` (503 `revit_error` when the add-in cannot say, 504
`revit_timeout` after 15 s). A spec without picks needs no Revit to be confirmed.

The workbench's routes: `POST /check` is the package's `check_code` (compile in Revit, never run;
with the lessons for the errors, as the MCP tool adds them) and `POST /dry-run` its `dry_run_code`
(run in the open model inside a transaction group that is always rolled back - nothing is kept, so
no confirmation and no token; one `dry_run` line in the evidence ledger). Each takes either
`{code, parameters?}` (a snippet and its `object[]`) or `{pack, values}`: a draft in the pack format
(validated with the package's `validate_pack`, 422 `invalid_pack`; its name checked like every tool
name) and the values by parameter name, whose template the package fills - the draft is saved in a
store whose user and built-in directories are fresh temporary directories, `render` fills it, the
directories are removed - so the code checked or dry-run is exactly what the saved pack would send
(422 `render_failed` with the package's messages in `errors` when it cannot). Any other field (the
package's `include`: local `.cs` files) is 422. Both answer the package's reply as it is, code the
sandbox refuses before Revit is asked (`blocked`), and 503 `revit_unreachable` when no add-in
answers; on a cloud host they count against `/spec/confirm`'s rate limit (429 `rate_limited`), a
local host does not limit them. `POST /choices` `{choices_from}` (`levels`, `floor_types`,
`family_types:OST_*`, `elements:OST_*`) gives `[{label, value}]` from the package's
`get_tool_choices`, the code `GET /tools/{name}/choices` uses, so the values are what a saved pack
receives. `GET /tools/{name}/export` is the pack file as stored (`text/yaml`); `POST /tools/import`
takes one (YAML text, at most 256 KB, YAML aliases refused), validates it as the package validates
a pack file (422 `invalid_pack` with the problems; 400 `blocked` for code the sandbox refuses) and
stores it as sent in the user directory; a user pack of the same name is replaced only with
`?overwrite=true` (409 `pack_exists` otherwise). The import takes the current (v1) pack format
only: a v0 file - an older export, or a hand-written pack without `schema_version` - is refused
until it is saved once in the current format. Built-in packs are read-only: `PUT` and `DELETE` on one,
and saving or importing a pack under its name, are 409 `builtin_read_only` - as long as the user
directory has no file of that name. A user file that carries a built-in's name is the designer's
own: it can be edited, replaced (import with `overwrite=true`, solidify) and deleted, and deleting
it brings the built-in back. Every tool name in a path, a body or a spec is checked before the store
is asked: an ASCII letter or `_`, then ASCII letters, digits, `_` (the shape of the pack schema's
parameter names), at most 100 characters, and never a Windows device name (`con`, `nul`, `prn`,
`aux`, `com0`-`com9`, `lpt0`-`lpt9`, any case) - 422 `invalid_tool_name`. `GET /tools` is read from
disk on every call and marks each pack `builtin` and its `origin` (`builtin` or `user`); a local
host adds `directory`, where the file is.

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
(`get_project_snapshot`, `query`, `list_tools`, `get_tool_choices`, `missing_params`, `reconcile`)
plus `propose_spec`; it has no `confirm_spec`, `run_tool`, `execute_code` or `get_guidance`, and no
tool that writes a pack. `list_tools` is read from disk on every call (a tool saved a moment ago, here
or by the MCP server, is offered at once) and marks each pack `builtin` and its `origin`. A
`propose_spec` with `action.kind: "execute_code"` shows no card and answers `{accepted: false,
errors: [{code: "code_not_offered", param: null, message}], reconcile: null}`: this host runs saved
tools only, and the message tells the model to say so and point to the workbench. A tool name the
model gives that is not one is `invalid_tool_name`, before any file is read. The session keeps a
tool result up to 8 KB after the turn that produced it (head plus a one-line note). After the
page ran something, `{execution: <ExecutionResult>, session_id}` (instead of `message`) hands the result back:
the turn starts with `event: execution` and the model (with the roles, the supervisor) reports what
the validator found. Exactly one
of `message` / `execution` per request (422 `invalid_args` otherwise); with `bridge: false` there are
no tools and no execution feedback (tool calls a model sends anyway are dropped, never
run - as in the last round of a turn, which goes out without tools).

A message may carry the designer's picks: `mentions: [{kind, id, label?}]` (at most 20; `id` an
integer, no other field; with a message and the bridge only, 422 otherwise). Before the model is
called the host checks them with the package's `resolve_mentions` and the turn starts with `event:
mentions` `{resolved, not_found, invalid, unresolved, warnings?}`: `resolved` the package's items,
`not_found` `{kind, id, reason}` for one that is not in the model now or a link that cannot be read
(`not_found`, `not_loaded`, ...), `invalid` for a kind and id that are not a model item, `unresolved`
for every pick when the model could not be asked (`revit_unreachable`, `timeout` after 15 s, or
`revit_error` when the add-in could not say - never read as "not found"). The verified picks follow
the designer's words in the model's user message as the package's "Designer's selections" block
(the system prompt carries its rule from the session's first turn, so the prompt does not change)
and stay with the session for the device the request drives: a spec may bind them in that turn or
any later one on that device; a pick a later turn finds gone leaves the session. Picks not found,
not valid or unresolved are not sent; with no Revit the turn runs as without picks. `label` never
reaches the model (this page does not send it). A message without picks has no such event and
goes to the model as typed.

Every turn also streams what the host did: `event: stage` `{role, model, status}` around each
role's part of the turn (`start`, then `end`; the last `end` before `done` or `error`), `event: tool`
`{id, role, name, args, status, ms?, size?, preview?, error?}` twice per tool call (`running`, then
`ok` or `error` with the same `id`) and `event: usage` `{role, model, round, input, output,
cache_read, cache_write, ms}` after each model call. `role` is `analyst`, `planner` or `supervisor`
(always `planner` with `LLM_SINGLE_MODEL=1` or `bridge: false`), `model` the model the call went to,
and `round` counts the model calls of the turn across its roles. A message's turn is the analyst's
stage, then the planner's when the route is `query` or `tool`; an execution report's is the
supervisor's, then the planner's when it sends the task back - its new card comes as `event: spec`
in that same reply. The analyst's and the supervisor's answers are JSON for the host: the page gets
only the text the host makes of them (the note, the report and question). `args` is what the model sent,
or `{"_truncated": true, "_chars": <length>, "_head": <first 2000 characters>}` when its JSON text
is longer than 8000 characters; for a tool that raised, `error` is the exception's type name only.
Token counts come from the provider's `usage` object (the request asks for it with
`stream_options.include_usage`; an endpoint that answers a 4xx other than 401 / 403 naming that
option is asked once more without it, and that address and model are not asked again once the
retry is accepted). `input` is the whole prompt, cached tokens included; `output` includes
thinking tokens also where the provider counts them only in `total_tokens` (Gemini's
OpenAI-compatible endpoint: `output` is then `total_tokens - input`); a count the provider did
not send, or one that is not an integer from 0 to 2^53, is `null`. Known limit: a gateway that
always sends `prompt_tokens_details.cached_tokens` (new-api sends 0 when the upstream reported
nothing) shows a cache read of 0, not unknown - the host cannot tell the two apart. Each call is also
stored in the `llm_calls` table (no text, no address, no key) with the optional `X-Run-Label`
header (1-64 of `A-Z a-z 0-9 _ . -`, 422 otherwise). `GET /api/usage?session_id=<id>` returns
those rows for one session, `GET /api/usage/runs` one line per run label with each sum `null` as
soon as one call lacks that count, and under `by_role` the same sums per role and model of the run;
like `/api/logs`, they need no password on a local host and `X-Admin-Token` on a cloud one.

Secrets never enter this repository: `.env`, `.secrets/` and `*.token` are ignored; a device's token
and browser key exist in plain text only in the one response that creates them (the store keeps
hashes), and the model key comes only from request headers or the environment.

## License

MIT - see [LICENSE](LICENSE).
