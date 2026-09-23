# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
- The system prompt is the package's `host_instructions()` plus the enabled skills; the
  wheel's `revit-bridge/SKILL.md` is now enabled by default (its references stay off).
  The host's own `HOST_INSTRUCTIONS` and the pack index in the prompt are gone.
- Sessions keep the model's message history (with tool calls), the last snapshot
  fingerprint, the last proposed spec and the last reported `evidence_id`; never a token.
  A tool result is kept in the session up to 8 KB (head plus a one-line note); the turn
  that produced it still sends it to the model in full.
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

### Changed

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
