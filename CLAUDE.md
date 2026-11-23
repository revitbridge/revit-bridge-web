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
- `backend/api/bridge.py` - `/api/v1/bridge/*` (the v1 contract: snapshot, query, packs, specs, confirmed execution, evidence), the device header dependency, add-in WebSocket endpoint; `relay_router` (socket + device routes) is mounted in cloud mode only, `cloud_only_router` (404 `cloud_mode_only`) in local mode; `backend/api/devices.py` pairing and the request's device; `backend/api/errors.py` the `{error, message?}` responses.
- `backend/api/chat.py` - `/api/chat`, the host loop (SSE: tokens, `spec`, `execution`, `done`);
  `backend/api/model_tools.py` the model's tools (the package's read-only ones + `propose_spec`);
  `backend/llm.py` the single OpenAI-compatible client, with function calling; `backend/session.py`.
- `backend/relay.py` - `DeviceRelay` / `WebSocketRevitClient` (same surface as `RevitClient`).
- `backend/localguard.py` - the request guard of a local host (proxy headers, Host, peer, Origin of writes).
- `backend/skill_store.py`, `backend/log_store.py`, `backend/config.py`, `backend/upgrade.py` (0.1 data-volume repair at startup).
- `frontend/src` - Vite + React; `config.ts` loads `/config.json` before render; pages in `components/pages`.
- `tests/` - pytest, with `tests/fake_revit.py` (a TCP stand-in for the add-in; no import from the package's tests) and `tests/fake_model.py` (a scripted OpenAI-compatible model behind `httpx.MockTransport`); `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`.

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
  path: no proxy forwarding header, a loopback `Host` (and peer outside a container), writes only
  from the host's own `Origin`. `cloud`: the relay and pairing, and `ADMIN_PASSWORD` is required
  (no start without it); no request guard.
- On a cloud host, changing capability packs (`PUT`/`DELETE /api/v1/bridge/tools/{name}`,
  `POST /solidify`), skill writes and `/api/logs` are admin only, through the one `X-Admin-Token`
  check (`WebSettings.admin_token_ok`). On a local host they need no password: changing packs and
  skills and reading logs is open to this machine. Either way no model tool may write a pack.
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
