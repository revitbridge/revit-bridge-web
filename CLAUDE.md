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
`tests/test_deploy_mode.py` covers what differs; `tests/test_refused.py` runs a pack's refusal
(revit-bridge 0.9.1 `refused: true`) through the package with the add-in's own reply. The suite's
chat is the one model loop (`env` sets `LLM_SINGLE_MODEL=1`: the tests written before the roles
describe it, and passing them unchanged is what shows the switch keeps it); `roles_env` gives the three roles
(`ROLES_ANALYST=on`, mode `roles_analyst`; the host's default is planner + supervisor, mode `roles`),
with `tests/fake_model.py` `RoleScriptedModel` answering each role from its own script
(`tests/test_roles.py`); `tests/test_chat_modes.py` covers the modes, the switches and the cloud rule.

## Layout

- `backend/main.py` - app factory, `/health`, `/config.json`, SPA serving, the `revit-bridge-web` console entry (`[project.scripts]`).
- `backend/api/bridge.py` - `/api/v1/bridge/*` (the v1 contract: snapshot, query, `mentions` (the chat's `@` picker: the package's `mention_candidates`), packs, specs (`/spec/reconcile` and `/spec/confirm` take the chat's `session_id` for the pick check), confirmed execution, evidence, and the workbench's `check` and `dry-run` (`code` or a draft `pack` + `values` filled by the package in a temporary store, `render_draft`), `choices`, `tools/{name}/export`, `tools/import` (`PackYamlLoader`: no aliases)), the tool name check (`require_tool_name`), the built-in read-only rule (`is_builtin`, 409 `builtin_read_only`), the listing (`list_packs`: `builtin`, `origin`, local `directory`), the device header dependency, add-in WebSocket endpoint; `relay_router` (socket + device routes) is mounted in cloud mode only, `cloud_only_router` (404 `cloud_mode_only`) in local mode; `backend/api/devices.py` pairing and the request's device; `backend/api/errors.py` the `{error, message?}` responses.
- `backend/api/chat.py` - `/api/chat`, the host loop (SSE: tokens, `spec`, `execution`, `done`, and
  what the host did: `stage`, `tool`, `usage`; the optional `X-Run-Label` header); a message's
  `mentions` (the designer's picks) are checked with the package's `resolve_mentions` before any
  model call (`event: mentions` first; the verified ones as the package's `mention_block` after the
  designer's words, kept on the session). The request's `mode` (`single`, `roles`, `roles_analyst`,
  `auto`) picks how the turn runs: `WebSettings.resolve_chat_mode` (a local host honours it, a cloud
  host only with the admin token; else `chat_mode_default`: `roles`, or `single` with
  `LLM_SINGLE_MODEL=1`, `roles_analyst` with `ROLES_ANALYST=on`); `Turn.mode` goes on the turn's
  first `stage` event and every `llm_calls` row. With the roles the turn runs in role segments (`Turn.loop`,
  one per role; `message_roles`: the analyst (mode `roles_analyst`), then the planner for `query` / `tool`;
  `execution_roles`: the supervisor, then the planner on `retry`), each role with the package's
  `host_instructions(role=..., code=False)` directly. The planner adds `WORKBENCH_LINE`,
  the analyst adds `roles.ANALYST_WORKBENCH_LINE`, and the supervisor uses the package text. Each role has its own tools and the model
  `models_file.resolve(role, headers)` gives it; the analyst's digest is the package's
  `snapshot_digest` (`model_digest`: one snapshot per session and device scope, again after a
  reported run; a failed read waits a minute). `ROLES_PLANNER_TOOLS` (`route` / `all`) is a
  variant of the roles for measuring, `ROLES_ANALYST` (`off` / `on`) the default mode (`WebSettings`);
  `no_model` names the role without a model; the interaction log names each role that ran.
  `LLM_SINGLE_MODEL=1` (`WebSettings.single_model`) is the one loop as before the roles - system
  prompt from the package with `code=False, mentions=True`, plus `WORKBENCH_LINE` and all eight
  tools. A request with `bridge: false` uses the baseline prompt and no tools;
  `backend/roles.py` the role table, the route -> tools map (`ROUTE_TOOLS`, `SUPERVISOR_TOOLS`; a call
  outside it is `tool_not_offered`, never run), the hand-offs (`analyst_message`, `planner_message`,
  `retry_message`, `supervisor_message`), the parsing of the analyst's and the supervisor's JSON
  (`parse_routing`, `parse_verdict`, one repair call, then `FALLBACK_ROUTING` / `fallback_verdict`),
  the verdict held to the package's (`enforce` on the raw result: validator failed or no success
  is never `done`, a run that succeeded is not sent back, a refusal by nature - `refusal_kind`, read
  from the package's fields - is never `retry`, at most `MAX_RETRIES` in a row), the caps of
  everything a model or the session gets of a result (`clip`, `execution_summary`,
  `supervisor_message`: `MESSAGE_KEEP` = the one loop's `EXECUTION_KEEP`) and the session's open
  task (`RoleTask`, `next_task`: only a `tool` routing opens one);
  `backend/api/model_tools.py` the six read-only tools, `propose_spec` (saved tools only) and
  `ask_designer`. The last tool ends the turn with an inline `questions` event. An optional
  `answers` field in the next chat request submits the active form. The host validates and labels
  these answers before handing them to the planner. Typed messages follow ordinary routing;
  `backend/llm.py` the single OpenAI-compatible client, with function calling and token usage
  (`parse_usage`: the provider shapes; a count not reported is `None`, never 0), the provider
  options a models file entry sets (`api: responses` -> `stream_responses`, not streamed, the same
  items out; `max_tokens_param`, `reasoning_effort`, `timeout_s`) and `ProviderMemory` (a tool
  call's provider keys - Gemini's `extra_content` - and a responses reply's output items, per
  session, back to the same endpoint and model only; a key two different replies share is unusable,
  so is a value over `MEMORY_VALUE_MAX`; at most `MEMORY_CALLS`, trimmed with the history by
  `Session.add` via `retain`, skipped when a trim leaves no user message); the default output limit
  `DEFAULT_MAX_TOKENS` (16384, read at call time), replaced by `LLM_MAX_TOKENS` (`WebSettings.llm_max_tokens`,
  passed as `ModelTarget.max_tokens` by `models_file.resolve` for every call whose entry sets none);
  an empty reply at the output limit is an `OutputLimitError`, which `chat.Turn.limit_notice` turns
  into the turn's `error` detail (role, model, limit, what is still there - `Turn.card`, the reported
  run, the untold planner after the supervisor's limit - and `limit_advice`, which depends on
  `ModelTarget.browser_model` / `entry_max_tokens`) - every role, no repair, no fallback. Without
  a models file the requests and streams are byte for byte the record in
  `tests/default_path_golden.json` (`tests/test_default_path.py`). The V3 request hashes live in
  `inline_forms_16384` and `inline_forms_4096`. The existing scenarios' SSE streams must also
  match the frozen `max_tokens_4096` stream hashes from aa5556c;
  `backend/session.py` (history, the session's fixed system prompt and role prompts, the turn
  counter, the verified picks per device scope, the roles' open task, digests per device scope,
  pending verdict and the spec `/spec/confirm` confirmed with the session until its run is reported,
  the `provider_memory`); `backend/mention_check.py` the `unknown_mention` check of a spec's
  `mention` evidence against the session's picks (the model's `propose_spec` and `reconcile`, the
  HTTP spec routes; `/spec/confirm` also reads the picks again, `recheck_picks` in `bridge.py`).
- `backend/api/usage.py` - `GET /api/usage`, `/api/usage/runs` (totals per run label, the run's chat `mode` (`mixed` for more than one), and `by_role`: the same per role and model): the `llm_calls` rows read back.
- `backend/relay.py` - `DeviceRelay` / `WebSocketRevitClient` (same surface as `RevitClient`).
  Its optional `mention_cache_scope` identifies the live connection for the package's picker cache.
- `backend/localguard.py` - the request guard of a local host (proxy headers, Host, peer, Origin of writes).
- `backend/skill_store.py`, `backend/log_store.py` (interaction logs and the `llm_calls` table: one row per model call, no text, no key), `backend/config.py`, `backend/models_file.py` (the models file: load and check at startup, `resolve(role, headers)` -> `ModelTarget`, the `features.models` of `/config.json`), `backend/upgrade.py` (0.1 data-volume repair at startup).
- `frontend/src` - Vite + React; `config.ts` loads `/config.json` before render (`features.chatModes` / `chatModeDefault` feed `components/task/chatMode.ts` and the task page's `ChatModeSelect`: the pick in `localStorage`, sent as `mode` by `flow.ts`); pages in `components/pages` (`route.ts`: `/capabilities/workbench` opens the Capabilities page's workbench, every other path the tabs); `components/hostv2/trace` the tool timeline and the token panel, fed by `components/task/flow.ts` (each reply keeps its `stage` / `tool` / `usage` events); `components/hostv2/workbench` the workbench component (editor, check, dry run, save, run, the tool list; every call through the `WorkbenchApi` it is given, types in `types/hostv2.ts`), `api/workbench.ts` its one HTTP client, `components/capabilities/WorkbenchPage.tsx` the page that wires them (with `workbenchRun.ts`: Run runs the saved tool through the spec card); `components/hostv2/mention` the mention input (the `@` picker), used by `components/task/ChatPanel.tsx`, fed by `components/task/mentions.ts` (`GET /mentions`, 10 s timeout; the picks' statuses from `event: mentions`).
- `bench/` - the token bench (`python -m bench`; `bench/tests` run with `uv run pytest`). `--mode`
  (default `auto`) goes as the chat mode with every request; the bench records it (`chat_mode`), the
  host's default and the modes its turns ran in (`chat`), and how it fed results back
  (`feedback_policy`) and confirmed ready proposals (`confirm_policy`; `--confirm once`, the
  unattended mode: one "yes" when the host's tool events show a ready `reconcile` and no
  `propose_spec`, `runner.waits_for_confirmation`; `compare` notes a difference in either).
  `bench/tasks-supervisor.yml`: tasks whose fixed execution result failed (a task's own
  `execution`), to measure the report step and the supervisor's send-backs.
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
- Configuration comes only from environment variables (see `.env.example`); no config files
  except `models.yml` (`MODELS_FILE`, default `DATA_DIR/models.yml`; `models.example.yml`): the
  model of each role (`analyst`, `planner`, `supervisor`), read and checked once at startup by
  `backend/models_file.py` - a file that is not valid, or missing where `MODELS_FILE` points,
  refuses the start with the file, field and problem, never a value. Keys never go in it: an
  entry names the environment variable (`api_key_env`), read on every call; any other field
  (`api_key`, `token`, `extra_headers`...) is refused. No file at the default path: `LLM_*` and
  the headers, byte for byte as before. With the file, the planner's entry stands where `LLM_*`
  stood and the `X-LLM-*` headers replace that entry (and only that one) for the request;
  `/config.json` `features.models` carries model names only. Every model call goes through
  `models_file.resolve(role, headers)`.
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
- The designer's picks (`@level`, `@type`, `@link`, `@element`): what to offer, whether a pick exists and the
  text the model gets all come from the package (`revit_bridge.mentions`). The host only keeps the
  verified items on the session and compares a spec's `mention:` evidence strings and values with
  them (`backend/mention_check.py`: a decimal ID for elements, a label or name for links, a name
  for levels and types), per device scope; `/spec/confirm` reads the bound picks again with the package before a
  token. The page sends only `{kind, id}`.
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
  on every call and come first; anything that varies belongs in later messages. With the roles this
  holds per role (and per route for the planner): roles pass structured hand-offs, never each
  other's prompts or transcripts, and a role's messages are only ever appended to.
- The package supplies role texts, model digests, selections and validator verdicts. The host
  adapts card instructions, routes turns, validates form answers and enforces the supervisor's verdict.
  The single-loop and role-based modes share the inline card and form protocol. Keep the existing
  golden scenarios' streams identical to aa5556c and compare requests against the V3 sections.
- Tests never touch the machine's real package data root: `tests/conftest.py` `env` points
  `REVIT_BRIDGE_DATA_DIR` at the test's own directory; a test that unsets it sets `LOCALAPPDATA` and
  `XDG_DATA_HOME` to a temporary directory first.
- Web API contract changes are a planning decision: stop and record `BLOCKED` in the notes log.
- `docs/` holds exactly one file, `docs/api-v1.json`. Deployment details stay in the notes repo.
- Scripts (`*.sh`, `*.ps1`, `*.py` tools, Dockerfile) are ASCII only.
- `.env`, `.secrets/`, `*.token` never enter the repository.

## Dependency on the package

Runtime attribute lookup across the host/package boundary:

- `WebSocketRevitClient.mention_cache_scope` returns (device ID, connection identity), or
  `None` when disconnected. The 0.10 picker looks up
  this exact name to keep cached inventories separate across devices and reconnects.

`pyproject.toml` requires `revit-bridge>=0.10,<0.11` from PyPI; `uv.lock` pins the exact
release (0.10.0). When the host needs something newer, bump the constraint and
`uv lock`. No test counts the built-in packs or names the first one: compare a listing with
`tests/conftest.py` `builtin_pack_names()` (read from the installed wheel), and give a user pack in
a test a name no release ships (`count_walls`, not a `create_*` or `list_*` name).

## Commits

Prefix commit subjects with `revit-bridge-web: `. Before committing run `uv run pytest`,
`npm run build` and the key-pattern `git grep` from the workspace `AGENTS.md` (it must
print nothing; the pattern is deliberately not repeated here so this file cannot match it).
