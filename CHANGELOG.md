# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Web API v1 under `/api/v1/bridge`, every route a thin wrapper over `revit-bridge` 0.2:
  `GET /snapshot`, `POST /query`, `POST /tools/{name}/missing-params`,
  `POST /spec/reconcile`, `POST /spec/confirm` (issues the one-time token, channel
  `host_ui`), `GET /evidence`, `POST /evidence/{id}/validate`. The contract is exported
  to `docs/api-v1.json` (CI checks it).
- `tests/fake_revit.py`: a TCP stand-in for the add-in so every route is tested end to
  end without Revit.
- `POST /spec/confirm` is rate limited per client address like `/api/chat`
  (`CHAT_RATE_LIMIT`, its own counter): 429 `{error: rate_limited}`.

### Changed

- `POST /tools/{name}/run` and `POST /execute` require the token from
  `POST /spec/confirm` and run through the package's `run_pack` / `run_code`
  (sandbox, preconditions, validator, evidence ledger with `host: "web"`); without a
  token they answer 400 `confirmation_required`, a tampered call is 200
  `confirmation_invalid`, a failed validation 200 `validation_failed`. The reply is the
  MCP `ExecutionResult` shape.
- `GET /tools` returns the MCP `list_tools` shape (`name, description, version,
  parameters, preconditions, validator, used`). `POST /solidify` takes v1 parameters
  and an optional `validator`.
- Status codes and error bodies across the bridge routes: 400 for a request the route
  refuses as such (`confirmation_required`, `invalid_category`, `unknown_kind`,
  `blocked`, `no_validator`), 422 for a body that parsed but is not valid
  (`invalid_args`, `invalid_spec`, `invalid_snapshot`, `invalid_pack`), 404 for what
  does not exist (`unknown_tool`, `unknown_evidence`), 503 `revit_unreachable` when no
  add-in answers (was 502), 200 `success: false` for refusals; bodies are
  `{error, message?, ...}` instead of FastAPI's `{detail}`.

- Requires `revit-bridge` 0.2.1 (`>=0.2.1,<0.3`). The package now ships eight built-in
  packs read in place from the wheel, keeps user packs and `usage.json` under its own
  data root, and no longer rewrites pack files to count executions.
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

- Over a slot, a Revit that is gone or leaves mid-request is a transport failure
  (`ConnectionError`) like over TCP, so the package consumes no confirmation token and
  writes no ledger line for code that never reached Revit; only a reply that does not
  arrive in time is still `success: false, "Timeout ..."`.
- `POST /api/v1/bridge/solidify` and `PUT /api/v1/bridge/tools/{name}` answer
  `422 {"error": "invalid_pack", "problems": [...]}` when the package rejects the pack
  (an undeclared `{placeholder}` in the code, a malformed parameter, an unknown
  validator) instead of 500.

### Removed

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
