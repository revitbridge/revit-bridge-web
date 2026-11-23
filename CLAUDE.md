# revit-bridge-web

Demo host for the `revit-bridge` package. Public repository: only `README.md`,
`CHANGELOG.md`, this file and `docs/api-v1.json` are documentation; design notes,
deployment details (domains, nginx, Cloudflare) and logs live in the private notes
repository.

## Build and test

```bash
uv sync                                  # backend deps (revit-bridge included)
uv run pytest                            # backend tests with TestClient, no Revit needed
cd frontend && npm ci && npm run lint && npm test && npm run build   # vitest: config parsing, chat stream handling
docker compose up --build                # the smoke test: UI on http://127.0.0.1:7860 (local mode)
uv run revit-bridge-web                  # the same without Docker (frontend built first); --port to move it
# the contract is a cloud host's export (every route present):
DEPLOY_MODE=cloud ADMIN_PASSWORD=openapi-export uv run python -c "from backend.main import create_app; import json; print(json.dumps(create_app().openapi(), indent=2))" > docs/api-v1.json
```

Tests run as a cloud host by default (`tests/conftest.py` `env`: `DEPLOY_MODE=cloud` with a fixture
`ADMIN_PASSWORD`); `local_env` gives a local one, whose requests must look like a browser on this
machine (`make_client(**LOCAL)`: Host `127.0.0.1:7860`, peer `127.0.0.1`; TestClient's `testserver`
is refused, and `websocket_connect` needs a full `ws://127.0.0.1:7860/...` URL).
`tests/test_deploy_mode.py` covers what differs.

## Layout

- `backend/main.py` - app factory, `/health`, `/config.json`, SPA serving, the `revit-bridge-web` console entry (`[project.scripts]`).
- `backend/api/bridge.py` - `/api/v1/bridge/*` (the v1 contract: snapshot, query, packs, specs, confirmed execution, evidence, and the workbench's `check` and `dry-run` (`code` or a draft `pack` + `values` filled by the package in a temporary store, `render_draft`), `choices`, `tools/{name}/export`, `tools/import` (`PackYamlLoader`: no aliases)), the tool name check (`require_tool_name`), the built-in read-only rule (`is_builtin`, 409 `builtin_read_only`), the listing (`list_packs`: `builtin`, `origin`, local `directory`), the device header dependency, add-in WebSocket endpoint; `relay_router` (socket + device routes) is mounted in cloud mode only, `cloud_only_router` (404 `cloud_mode_only`) in local mode; `backend/api/devices.py` pairing and the request's device; `backend/api/errors.py` the `{error, message?}` responses.
- `backend/api/chat.py` - `/api/chat`, the host loop (SSE: tokens, `spec`, `execution`, `done`, and
  what the host did: `stage`, `tool`, `usage`; the optional `X-Run-Label` header); the system prompt
  is the package's `host_instructions()` plus `HOST_PARAGRAPH` (no code, saved tools only, the
  workbench link) - no skills;
  `backend/api/model_tools.py` the model's tools (the package's read-only ones + `propose_spec`, which
  refuses `execute_code` with `code_not_offered`; no `get_guidance`);
  `backend/llm.py` the single OpenAI-compatible client, with function calling and token usage
  (`parse_usage`: the provider shapes; a count not reported is `None`, never 0);
  `backend/session.py` (history, the session's fixed system prompt, the turn counter).
- `backend/api/usage.py` - `GET /api/usage`, `/api/usage/runs`: the `llm_calls` rows read back.
- `backend/relay.py` - `DeviceRelay` / `WebSocketRevitClient` (same surface as `RevitClient`).
- `backend/localguard.py` - the request guard of a local host (proxy headers, Host, peer, Origin of writes).
- `backend/skill_store.py`, `backend/log_store.py` (interaction logs and the `llm_calls` table: one row per model call, no text, no key), `backend/config.py`, `backend/upgrade.py` (0.1 data-volume repair at startup).
- `frontend/src` - Vite + React; `config.ts` loads `/config.json` before render; pages in `components/pages` (`route.ts`: `/capabilities/workbench` opens the Capabilities page's workbench, every other path the tabs); `components/hostv2/trace` the tool timeline and the token panel, fed by `components/task/flow.ts` (each reply keeps its `stage` / `tool` / `usage` events); `api/workbench.ts` the one HTTP client of the workbench (it satisfies the `WorkbenchApi` of the workbench component), `components/capabilities/WorkbenchPlaceholder.tsx` the page until that component is wired in.
- `tests/` - pytest, with `tests/fake_revit.py` (a TCP stand-in for the add-in; no import from the package's tests) and `tests/fake_model.py` (a scripted OpenAI-compatible model behind `httpx.MockTransport`; it returns usage in each provider's shape, `USAGE_SHAPES`); `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`.

## Hard constraints

- No domain logic here. Revit access, model queries, capability packs, the sandbox and
  slot-token checks come from `revit_bridge`. If the site needs something the package
  does not expose, add it to the package (PR there), never reimplement it here.
- The frontend reads `/config.json` (`apiBase`, `wsBase`, `features`) at runtime. No
  `VITE_API_BASE_URL` or any other build-time address.
- Bring your own model: `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` first, then
  `LLM_*` environment variables - except the key: `LLM_API_KEY` only ever goes to
  `LLM_BASE_URL`, so a request with its own `X-LLM-Base-Url` must bring `X-LLM-Key`.
  A browser-supplied endpoint must be https on a public address unless `LLM_ALLOW_HTTP=1`.
  Keys are never written to disk, logs or error messages.
- Configuration comes only from environment variables (see `.env.example`); no config files.
- `DEPLOY_MODE` is `local` (default) or `cloud` (`backend/config.py` validates it at startup).
  `local`: only loopback may reach the host (`HOST`, or in the image the published `BIND_ADDRESS`;
  otherwise it refuses to start), `ADMIN_PASSWORD` or `CORS_ORIGINS` set refuses to start too, no
  relay, pairing or device routes (404 `cloud_mode_only`), device headers are not read, and the
  admin checks pass without a password - so `backend/localguard.py` must stay in front of every
  path: no proxy forwarding header, a loopback `Host` (and peer outside a container), no
  `Sec-Fetch-Site` of `cross-site` / `same-site` (except a top-level navigation to the page shell or
  a static file - keep any new data path out of that exception, `NOT_THE_PAGE`), writes and
  WebSocket upgrades only from the host's own `Origin`, and a frame policy on every response. A new
  route on a local host - WebSocket included - needs nothing else to be guarded; do not mount
  anything outside it. `cloud`: the relay and pairing, and `ADMIN_PASSWORD` is required
  (no start without it); no request guard.
- On a cloud host, changing capability packs (`PUT`/`DELETE /api/v1/bridge/tools/{name}`,
  `POST /solidify`, `POST /tools/import`), skill writes, `/api/logs` and `/api/usage` are admin only,
  through the one `X-Admin-Token` check (`WebSettings.admin_token_ok`). On a local host they need no
  password: changing packs and skills and reading logs and usage is open to this machine. Either way
  no model tool may write a pack, and a built-in pack (the wheel's, with no user file of its name) is
  never changed, hidden or replaced (409 `builtin_read_only`).
- The model in the chat writes no code: no `get_guidance`, no skills in the prompt, `propose_spec`
  refuses `execute_code`. Code is tried and saved as a tool by a person on the workbench.
- Every tool name from a request or from the model goes through `require_tool_name` (ASCII
  letters, digits and `_`, at most 100 characters, no Windows device name) before any `ToolStore`
  call. A draft pack is filled only by the package (`ToolStore.render` in a store of temporary
  directories): no escaping or substitution in the host or the frontend.
- The host never sets the package's data variables: without a container the user tools are the
  package's per-user directory, shared with its MCP server. Listings are read from disk on every
  call (no cache). The 0.1 repair at startup touches only `DATA_DIR/capabilities`.
- Keys and request headers never reach an SSE event or `llm_calls`; a token count the provider
  did not report stays `null` everywhere (events, table, `/api/usage` sums, the page's totals).
- Within a chat session the system prompt and the tool definitions go out byte for byte the same
  on every call and come first; anything that varies belongs in later messages.
- Tests never touch the machine's real package data root: `tests/conftest.py` `env` points
  `REVIT_BRIDGE_DATA_DIR` at the test's own directory; a test that unsets it sets `LOCALAPPDATA` and
  `XDG_DATA_HOME` to a temporary directory first.
- Web API contract changes are a planning decision: stop and record `BLOCKED` in the notes log.
- `docs/` holds exactly one file, `docs/api-v1.json`. Deployment details stay in the notes repo.
- Scripts (`*.sh`, `*.ps1`, `*.py` tools, Dockerfile) are ASCII only.
- `.env`, `.secrets/`, `*.token` never enter the repository.

## Dependency on the package

`pyproject.toml` requires `revit-bridge>=0.8,<0.9` from PyPI; `uv.lock` pins the exact
release. When the host needs something newer, bump the constraint and `uv lock`.

## Commits

Prefix commit subjects with `revit-bridge-web: `. Before committing run `uv run pytest`,
`npm run build` and the key-pattern `git grep` from the workspace `AGENTS.md` (it must
print nothing; the pattern is deliberately not repeated here so this file cannot match it).
