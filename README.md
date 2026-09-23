# revit-bridge-web

Deploy-anywhere demo host for [revit-bridge](https://github.com/revitbridge/revit-bridge): one container
that serves the web UI, relays remote Revit add-ins and streams a model you bring yourself. The host
contains no Revit logic of its own - every query, execution and capability pack goes through the
`revit_bridge` package.

设计师与 AI 之间的意图桥演示站：连接 Revit、用自带模型提出任务、审查并执行 C#、把可复用的代码固化为能力包。

## Install

```bash
git clone https://github.com/revitbridge/revit-bridge-web.git
cd revit-bridge-web
cp .env.example .env        # optional: server-side model, admin password, device limit
docker compose up -d --build
```

The UI is on `http://127.0.0.1:7860`. Put a reverse proxy with TLS in front of it for anything
beyond localhost; WebSocket upgrades on `/api/v1/bridge/ws/` must be forwarded.

Split deployment (SPA on a static host, API on another machine): build `frontend/` and upload
`dist/`, then place a static `config.json` next to its `index.html` - copy
[`frontend/public/config.example.json`](frontend/public/config.example.json) and set `apiBase` to the
API origin (and `wsBase` if the relay differs). The SPA reads that file at startup; the backend's own
`/config.json` only describes a same-origin setup. Set `CORS_ORIGINS` on the API side to the static
host's origin.

Connect a designer's Revit to the host in three steps: on the Connect page choose *pair a Revit* and
you get a pairing code (ten minutes) plus the one line to run on that machine (Revit closed):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/revitbridge/revit-bridge-addin/main/installer/install.ps1))) -Mode remote -Server https://<your-host> -Pair XXXX-XXXX
```

The add-in redeems the code, receives its own device token and connects; the page then shows the
device as online. The browser that paired it keeps the *browser key* (session storage) and sends
`X-Device-Id` / `X-Device-Key` with every request; *revoke* ends both at once. A Revit running on the
Docker host itself is reached over TCP (`host.docker.internal:18080`) without any pairing.

## Use

1. **Connect** - choose the Revit (the local add-in over TCP, or a paired device) and fill in *Model*
   (OpenAI-compatible base URL, model name, API key). Model settings and the device's browser key
   stay in the browser tab (`sessionStorage`) and travel as request headers; the server never stores
   them. The pairing UI itself arrives with the next release; until then a device is paired through
   `POST /api/v1/bridge/devices/pair` (see below).
2. **Task** - the demo sequence. The page opens with a snapshot of the model (units, levels, type
   summary, selection, fingerprint). Describe what you want in one sentence: the model works through
   the package's tools - it takes a snapshot, asks the questions a capability pack still has open
   (with the real levels and types, as buttons you can click) and reconciles a draft - then proposes
   a TaskSpec, which the page shows as a spec card: every parameter with its source and evidence,
   the readings it made as checkboxes, conflicts in red. It cannot confirm or execute anything.
   *Confirm* lights up only when the reconciliation is ready; it issues a one-time token that stays
   in the page's memory (never `localStorage`), and the host runs exactly what was confirmed. You
   see the validator's checks, the evidence id, and `validation_failed` in red when Revit ran the
   code but the model did not change as claimed. The tamper demo changes one confirmed value and
   runs again under the same token: `confirmation_invalid`, nothing reaches Revit. The result then
   goes back to the model for a faithful report, and code that worked can be solidified into a v1
   pack. The *Compare with no bridge* switch sends the same sentence a second time with
   `bridge: false` (no tools, no skills, nothing executable) and shows both answers side by side.
3. **Capabilities** - *Packs*: a saved pack with its version, validator and preconditions; load the
   real choices from the model (levels, types, elements), fill the parameters, and *Confirm and run*
   takes the same route as the task page (a spec card, a token, the validator, the ledger); edit or
   delete it. *Skills*: Markdown protocols that are appended to the model's system prompt - the
   plugin's `revit-bridge` skill comes with the package and is on; its references are listed but off
   (the model reads them on demand); add, upload or import more from GitHub (admin password required).
4. **Evidence** - the ledger of every execution through this host or the MCP server: who confirmed
   it, the token prefix, what ran, what the validator found. Expand a record for all of its fields;
   *Validate* re-runs its assertion against the model as it is now.

The system prompt is the package's `host_instructions()` plus the enabled skills; the task page's
"without the bridge" comparison sends the same message with `bridge: false` (one plain prompt, no
tools, no skills).

Upgrading from 0.1: the 0.1 host copied the package's built-in packs into `data/capabilities/`,
which is now the package's own directory for user packs, where a file of the same name would hide
the wheel's pack. On its first start 0.2 moves every 0.1 copy (a pack file without
`schema_version`) to `data/capabilities/legacy-0.1/` and logs one line per file; nothing is
deleted. A pack you solidified with 0.1 lands there too: solidify it again from the Capabilities
page (its `code_template` and `parameters` are still in the file).

Development without Docker:

```bash
uv sync                                  # backend deps, revit-bridge included
cd frontend && npm ci && npm run build   # or `npm run dev` with the proxy to :7860
cd .. && uv run python -m backend.main   # http://localhost:7860
uv run pytest                            # backend tests, no Revit needed
```

## Configure

Everything is an environment variable (`.env` for Docker). The browser gets what it needs from
`/config.json`, so the same build runs on any address.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | unset | Server-side model defaults; headers `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` override them per request. Unset = visitors must bring their own. |
| `LLM_ALLOW_HTTP` | `0` | Allow browser-supplied `http://` endpoints (local models). |
| `CHAT_RATE_LIMIT` | `30` | `/api/chat` and `/api/v1/bridge/spec/confirm` requests per minute per client IP (separate counters). |
| `ADMIN_PASSWORD` | unset | Enables skill edits and `/api/logs` via `X-Admin-Token`. |
| `REVIT_BRIDGE_HOST`, `REVIT_BRIDGE_PORT`, `REVIT_BRIDGE_TOKEN`, `REVIT_BRIDGE_TIMEOUT` | `host.docker.internal`, `18080`, unset, `60` | Local add-in over TCP (read by the package). |
| `MAX_DEVICES` | `20` | How many paired add-ins may be connected at once. Pairing creates the credentials; only their hashes are stored (`REVIT_BRIDGE_DATA_DIR/auth/devices.json`), so nothing is mounted. The 0.1 slot variables (`MAX_SLOTS`, `MCP_BRIDGE_REQUIRE_SLOT_TOKEN`, `MCP_BRIDGE_SLOT_TOKEN_*`) are gone and the host refuses to start if one is still set. |
| `DATA_DIR` | `/app/data` | This host's files: skills, interaction logs (`./data` volume). |
| `REVIT_BRIDGE_DATA_DIR` | `/app/data` | The package's data root: user capability packs (solidified, edited or hidden built-ins), `usage.json`, the evidence ledger (read by the package; same volume). The built-in packs are read from the wheel. |
| `SKILLS_DIR` | unset | Optional read-only directory that replaces the built-in skill *directory* (the wheel's `revit_bridge/skills/`, laid out as `revit-bridge/SKILL.md` + `references/`): mount a checkout with the same layout to keep the `builtin:revit-bridge/...` ids, or point it at an empty directory to list no built-in skills at all. |
| `PUBLIC_WS_BASE` | unset | Relay address shown in the add-in command when it differs from the page origin (dedicated WebSocket host name). |
| `CORS_ORIGINS` | unset | Origins allowed to call the API cross-origin (split deployment). |
| `BIND_ADDRESS`, `PORT` | `127.0.0.1`, `7860` | Published host interface and port (`docker-compose.yml`). |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers are trusted for client IPs. |

## API v1

`/api/v1/bridge/*` is a thin HTTP surface over the package, exported to
[`docs/api-v1.json`](docs/api-v1.json) (`/openapi.json` on a running host). The routes follow the
package's flow: `GET /snapshot` and `POST /query` read the model; `GET /tools`,
`POST /tools/{name}/missing-params` and `POST /spec/reconcile` prepare a TaskSpec; `POST /spec/confirm`
turns the designer's confirmation into a one-time token; `POST /tools/{name}/run` and `POST /execute`
run only with that token (sandbox review, preconditions, validator and the evidence ledger are the
package's; the ledger records `host: "web"`); `GET /evidence` and `POST /evidence/{id}/validate` show
and re-check what ran. A request the route refuses as such is 400, a body that parsed but is not
valid 422, a Revit that cannot be reached before the run is 503 (a transport failure during the run
is the package's `success: false` without consuming the token), and a refusal by the gate, a failed
precondition or a failed validation is 200 with `success: false`; error bodies are
`{error, message?, ...}` with the MCP tools' codes, declared per route in the export.

Pairing and devices: `POST /devices/pair` returns `{code, device_id, expires_at, browser_key,
install_command}`; the add-in calls `POST /devices/redeem` with the code (no headers) and receives
`{device_id, device_token, ws_url}`; `GET /devices/{id}` and `DELETE /devices/{id}` take the browser
key (or the admin password), `GET /devices` is admin only, and `GET /slots` publishes
`{max_devices, connected}`. Pick a Revit with `X-Device-Id` + `X-Device-Key`; without them a request
drives the local add-in over TCP. Every confirmation and every ledger line belongs to that device
(`scope`), so a browser sees and re-validates only its own device's evidence, and a token confirmed
for one device is refused on another. Ad-hoc code (`POST /execute`) also asks the designer on the
device itself; a No there is `success: false, error: "declined_on_device"`.

`POST /api/chat` is the host loop: `{message, session_id, bridge?}` streams one turn over SSE -
reply text as `data: "<token>"` frames, `event: spec` (`{spec, card, errors, reconcile}`) when the
model proposed a spec, `event: done`. The model's tools are the package's read-only ones
(`get_project_snapshot`, `query`, `list_tools`, `get_tool_choices`, `missing_params`, `reconcile`)
plus `propose_spec`; it has no `confirm_spec`, `run_tool` or `execute_code`. After the page ran
something, `{execution: <ExecutionResult>, session_id}` (instead of `message`) hands the result back:
the turn starts with `event: execution` and the model reports what the validator found. Exactly one
of `message` / `execution` per request (422 `invalid_args` otherwise); with `bridge: false` there are
no tools, no skills and no execution feedback.

Secrets never enter this repository: `.env`, `.secrets/` and `*.token` are ignored; a device's token
and browser key exist in plain text only in the one response that creates them (the store keeps
hashes), and the model key comes only from request headers or the environment.

## License

MIT - see [LICENSE](LICENSE).
