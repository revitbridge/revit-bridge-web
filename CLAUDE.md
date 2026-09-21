# revit-bridge-web

Demo host for the `revit-bridge` package. Public repository: only `README.md`,
`CHANGELOG.md`, this file and `docs/api-v0.json` are documentation; design notes,
deployment details (domains, nginx, Cloudflare) and logs live in the private notes
repository.

## Build and test

```bash
uv sync                                  # backend deps (revit-bridge included)
uv run pytest                            # backend tests with TestClient, no Revit needed
cd frontend && npm ci && npm run lint && npm test && npm run build   # vitest: config parsing, chat stream handling
docker compose up --build                # the smoke test: UI on http://127.0.0.1:7860
uv run python -c "from backend.main import create_app; import json; print(json.dumps(create_app().openapi(), indent=2))" > docs/api-v0.json
```

## Layout

- `backend/main.py` - app factory, `/health`, `/config.json`, SPA serving.
- `backend/api/bridge.py` - `/api/v1/bridge/*`, slot header dependency, add-in WebSocket endpoint.
- `backend/api/chat.py` - `/api/chat` SSE; `backend/llm.py` the single OpenAI-compatible client.
- `backend/relay.py` - `SlotManager` / `WebSocketRevitClient` (same surface as `RevitClient`).
- `backend/skill_store.py`, `backend/log_store.py`, `backend/config.py`.
- `frontend/src` - Vite + React; `config.ts` loads `/config.json` before render; pages in `components/pages`.
- `tests/` - pytest; `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`.

## Hard constraints

- No domain logic here. Revit access, model queries, capability packs, the sandbox and
  slot-token checks come from `revit_bridge`. If the site needs something the package
  does not expose, add it to the package (PR there), never reimplement it here.
- The frontend reads `/config.json` (`apiBase`, `wsBase`, `features`) at runtime. No
  `VITE_API_BASE_URL` or any other build-time address.
- Bring your own model: `X-LLM-Base-Url` / `X-LLM-Model` / `X-LLM-Key` first, then
  `LLM_*` environment variables. Keys are never written to disk, logs or error messages.
- Configuration comes only from environment variables (see `.env.example`); no config files.
- Web API contract changes are a planning decision: stop and record `BLOCKED` in the notes log.
- `docs/` holds exactly one file, `docs/api-v0.json`. Deployment details stay in the notes repo.
- Scripts (`*.sh`, `*.ps1`, `*.py` tools, Dockerfile) are ASCII only.
- `.env`, `.secrets/`, `*.token` never enter the repository.

## Dependency on the package

`pyproject.toml` requires `revit-bridge>=0.1.0` from PyPI; `uv.lock` pins the exact
release. When the host needs something newer, bump the constraint and `uv lock`.

## Commits

Prefix commit subjects with `revit-bridge-web: `. Before committing run `uv run pytest`,
`npm run build` and the key-pattern `git grep` from the workspace `AGENTS.md` (it must
print nothing; the pattern is deliberately not repeated here so this file cannot match it).
