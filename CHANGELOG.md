# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Requires `revit-bridge` 0.2 (`>=0.2.0,<0.3`). The package now ships eight built-in
  packs read in place from the wheel, keeps user packs and `usage.json` under its own
  data root, and no longer rewrites pack files to count executions.
- Capability packs come straight from the package's `ToolStore`: built-in packs are
  read from the wheel, user packs live under `REVIT_BRIDGE_DATA_DIR/capabilities`
  (`/app/data` in the container, the `./data` volume). Nothing is copied on first
  start any more; the entrypoint only makes the volume writable.

### Removed

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
