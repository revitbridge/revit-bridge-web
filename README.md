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
cp .env.example .env        # optional: server-side model, admin password, slot tokens
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

Connect a designer's Revit to the host (on their machine, Revit closed):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/revitbridge/revit-bridge-addin/main/installer/install.ps1))) -Mode remote -Server wss://<your-host>/api/v1/bridge/ws
```

The add-in then shows up as a slot on the Connect page. A Revit running on the Docker host itself is
reached over TCP (`host.docker.internal:18080`) without any of that.

## Use

1. **Connect** - choose the Revit (local TCP or a slot), enter the slot token if the host requires one,
   fill in *Model* (OpenAI-compatible base URL, model name, API key). Model settings stay in the
   browser tab (`sessionStorage`) and travel as request headers; the server never stores them.
2. **Task** - describe what you want. The model works through the package's tools - it takes a
   snapshot, asks the questions a capability pack still has open (with the real levels and types),
   reconciles a draft against the model - and proposes a TaskSpec; the host shows the spec card. It
   cannot confirm or execute anything: you confirm the card and the host runs exactly what was
   confirmed, then the result goes back to the model for a faithful report. Until the task page of
   the next release ships, the page still shows the old chat: executions from the UI are refused
   because the page does not obtain the token from `/spec/confirm` yet.
3. **Capabilities** - *Packs*: run a saved pack with real choices from the model (levels, types,
   elements), edit or delete it (*Run* is refused the same way until the next release). *Skills*:
   Markdown protocols that are appended to the model's system prompt - the plugin's `revit-bridge`
   skill comes with the package and is on; its references are listed but off (the model reads them on
   demand); add, upload or import more from GitHub (admin password required).

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
| `MAX_SLOTS` | `5` | Remote add-in slots on the relay. |
| `MCP_BRIDGE_REQUIRE_SLOT_TOKEN`, `MCP_BRIDGE_SLOT_TOKEN_FILE_N` | `0`, unset | Pre-shared token per slot; put the file in `./.secrets/` (mounted read-only at `/run/secrets`). The browser sends `X-Slot-Id` / `X-Slot-Token`. |
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
`{error, message?, ...}` with the MCP tools' codes, declared per route in the export. Pick a Revit
with `X-Slot-Id` (+ `X-Slot-Token`) as before.

`POST /api/chat` is the host loop: `{message, session_id, bridge?}` streams one turn over SSE -
reply text as `data: "<token>"` frames, `event: spec` (`{spec, card, errors, reconcile}`) when the
model proposed a spec, `event: done`. The model's tools are the package's read-only ones
(`get_project_snapshot`, `query`, `list_tools`, `get_tool_choices`, `missing_params`, `reconcile`)
plus `propose_spec`; it has no `confirm_spec`, `run_tool` or `execute_code`. After the page ran
something, `{execution: <ExecutionResult>, session_id}` (instead of `message`) hands the result back:
the turn starts with `event: execution` and the model reports what the validator found. Exactly one
of `message` / `execution` per request (422 `invalid_args` otherwise); with `bridge: false` there are
no tools, no skills and no execution feedback.

Secrets never enter this repository: `.env`, `.secrets/` and `*.token` are ignored; the container reads
slot tokens from mounted files and the model key only from request headers or the environment.

## License

MIT - see [LICENSE](LICENSE).
