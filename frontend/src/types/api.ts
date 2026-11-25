/* Shared API types (mirrors docs/api-v1.json). */

import type { Mention, MentionItem, StageEvent, ToolEvent, TraceEvent, UsageEvent } from './hostv2'

export interface RevitHealthResponse {
  revit_connected: boolean
  user_tools_allowed?: boolean | null
  latency_ms: number | null
  detail: string
  mode: string
  endpoint?: string
  protocol?: string
  bridge_version: string
  timestamp: string
  devices: DevicesStatus
}

/* GET /slots: the relay's capacity, no per-device detail. */
export interface DevicesStatus {
  max_devices: number
  connected: number
}

/* POST /devices/pair: the code the designer types (or the install command carries),
   the browser key that drives and revokes this device, both shown once. */
export interface PairingResponse {
  code: string
  device_id: string
  expires_at: string
  browser_key: string
  install_command: string
}

/* GET /devices/{id} (browser key or admin), and one item of the admin GET /devices. */
export interface DeviceStatus {
  device_id: string
  label: string
  online: boolean
  last_seen: string | null      // ISO, or null before the add-in ever connected
  requests: number              // the relay's counter; 0 while offline
  revoked: boolean
  addin_version?: string | null
}

/* revit_bridge.snapshot.ProjectSnapshot, as GET /snapshot returns it. */
export interface ProjectSnapshot {
  schema_version: number
  taken_at: string
  duration_ms: number
  document: { title: string; revit_version: string; is_workshared: boolean }
  units: { length: string; raw: string }
  active_view: { name: string; view_type: string; level: string | null } | null
  levels: Array<{ id: number; name: string; elevation_mm: number }>
  grids: { count: number; names: string[] }
  family_types: Array<{ category: string; count: number; names: string[] }>
  selection: Array<{ id: number; category: string; name: string }>
  selection_count: number
  links: Array<{ name: string; loaded: boolean }>
  phases: string[]
  warnings: string[]
  fingerprint: string
}

/* POST /query: the package's answer as is ({kind, items, ...} or {error, ...}). */
export interface QueryAnswer {
  kind?: string
  items?: Array<Record<string, unknown>>
  error?: string
  message?: string
  [key: string]: unknown
}

export interface ToolParam {
  name: string
  description?: string
  type?: string
  default?: unknown
  source?: string
  required?: boolean
  unit?: string
  choices_from?: string
}

/* One item of GET /tools: the package's list_tools shape, plus where the pack comes from:
   builtin (the wheel's, read-only) or user (a file in the user capabilities directory);
   directory only on a local host. */
export interface ToolInfo {
  name: string
  display_name?: string
  applies_when?: string[]
  not_for?: string[]
  revit_versions?: string[]
  read_only?: boolean
  ui_only?: boolean
  description: string
  version: string
  parameters: ToolParam[]
  preconditions: Array<Record<string, unknown>>
  validator: string | null
  used: number
  builtin: boolean
  origin: 'builtin' | 'user'
  directory?: string
}

/* GET /tools/{name}: the full pack. */
export interface ToolDetail {
  name: string
  display_name: string
  description: string
  version: string
  parameters: ToolParam[]
  tags: string[]
  execution_count: number
  code_template: string
  source_query: string
  preconditions: Array<Record<string, unknown>>
  applies_when: string[]
  not_for: string[]
  validator: Record<string, unknown> | null
}

export interface ToolChoiceItem {
  label: string
  value: string | number
}

export interface ToolUpdatePayload {
  display_name?: string
  description?: string
  code_template?: string
  parameters?: ToolParam[]
  tags?: string[]
  source_query?: string
}

export interface ExecutionResponse {
  success: boolean
  result: unknown
  error: string | null
  tool?: string
}

export interface SolidifyResponse {
  status: string
  name: string
  display_name: string
  version?: string
  revit_synced: boolean
}

export interface Skill {
  id: string
  name: string
  description: string
  version: string
  author: string
  enabled: boolean
  source: 'builtin' | 'custom'
  layer: string
  readonly: boolean
  file_size?: number
  content?: string
  raw?: string
}

export type PlanStatus = 'pending' | 'proposed' | 'done' | 'failed' | 'skipped'
export interface PlanStep {
  id: string
  title: string
  status: PlanStatus
  evidence_id?: string
}
export interface TaskPlan {
  plan_id: string
  workflow?: string
  steps: PlanStep[]
}

export interface ChatMessage {
  images?: ViewImage[]
  imageNotice?: string
  plan?: TaskPlan
  stepResults?: Record<string, ExecutionResult>
  continuesTurn?: boolean
  form?: DesignerForm
  supervisorForm?: boolean
  answered?: 'form' | 'text' | 'replaced' | 'invalid' | 'execution'
  role: 'user' | 'assistant' | 'host'   // host: a note from the page itself, e.g. the execution result going to the model
  content: string
  trace?: TraceEvent[]                  // assistant: the stage / tool / usage events of its turn, in order
  mentions?: SentMention[]              // user: the model items picked in it, with what the host found
}

/* A pick in a sent message. status: 'pending' until the turn's mentions event says more;
   'resolved' (checked against the model: the model got it), 'not_found' and 'invalid'
   (not sent to the model; reason as the host gave it), 'unresolved' (the model could not
   be asked, e.g. Revit was not connected; not sent either). */
export type MentionStatus = 'pending' | 'resolved' | 'not_found' | 'invalid' | 'unresolved'
export interface SentMention extends Mention {
  status: MentionStatus
  reason?: string
}

/* A pick as POST /api/chat takes it: the kind and the element id (the host also takes a
   `label`, which this page does not send: the host reads the item from the model). */
export interface MentionRef {
  document_id?: string
  kind: string
  id: number
}

/* One pick the host could not use (event: mentions): as the request named it, and why. */
export interface MentionProblem {
  kind: string | null
  id: unknown
  reason: string
}

/* event: mentions - the turn's first frame when the message carried picks. */
export interface MentionReport {
  resolved: (MentionItem & Record<string, unknown>)[]
  not_found: MentionProblem[]
  invalid: MentionProblem[]
  unresolved: MentionProblem[]
  warnings?: string[]
}

/* -- the host loop (POST /api/chat), spec 10.10 ------------------------------------ */

/* revit_bridge.spec.models.TaskSpec as JSON. */
export interface TaskSpec {
  task: string
  action: { kind: 'run_tool' | 'execute_code'; tool?: string; code?: string; code_parameters?: unknown[] }
  parameters: SpecParameter[]
  interpretations?: Interpretation[]
  steps?: string[]
  snapshot_fingerprint?: string
  language?: string
}

export interface SpecParameter {
  name: string
  value: unknown
  unit?: string | null
  source: string
  evidence: string
}

export interface Interpretation {
  param: string | null
  text: string
  confirmed: boolean
}

export interface SpecError {
  code: string
  param: string | null
  message: string
}

export interface ReconcileResult {
  conflicts: Array<Record<string, unknown>>
  questions: Question[]
  interpretations_required: Interpretation[]
  ready: boolean
}

/* Who can answer a question (revit-bridge 0.4): the designer (user_intent), the model
   once checked (model_fact: a value the model was said to have and does not), or a
   technical one. Absent from a 0.3 server. */
export type QuestionKind = 'user_intent' | 'model_fact' | 'technical'

/* The steps an unanswered question holds up (revit-bridge 0.4). */
export type BlockedStep = 'spec' | 'execute' | 'build'

export interface Question {
  id: string
  param: string
  text: string
  why?: string
  options: Array<{ label: string; value: unknown; source?: string }>
  allow_other?: boolean
  // Shown, never acted on: the page gates on reconcile.ready alone.
  kind?: QuestionKind
  blocks?: BlockedStep[]
}

/* What /tools/{name}/run and /execute answer (the package's ExecutionResult, or a refusal). */
export interface ExecutionResult {
  success: boolean
  error: string | null
  tool?: string | null
  result?: unknown
  validation?: { validator: string; passed: boolean; checks: Array<{ detail: string; passed?: boolean }> } | null
  evidence_id?: string
  preconditions_failed?: string[]
  warnings?: string[]
  hint?: string
  reason?: string
  message?: string
  refused?: boolean      // revit-bridge 0.9.1: the pack refused the input in Revit, nothing was kept
}

/* One turn of the host loop: exactly one of message / execution; mentions only with a
   message and the bridge (at most 20). */
export interface DesignerOption {
  value: string | number | boolean
  label: string
}

export interface DesignerForm {
  selection?: SelectionQuestion
  ask_id: string
  questions: { id: string; text: string; options: DesignerOption[]; allow_other?: boolean; multiple?: boolean }[]
}

export interface DesignerAnswers {
  ask_id: string
  items: (DesignerOption & { id: string })[]
}

export interface HostRequest {
  selection_answer?: string
  capture?: Pick<ViewImage, 'capture_id' | 'png_base64'>
  run?: 'confirm' | 'auto'
  answers?: DesignerAnswers
  message?: string
  execution?: ExecutionResult
  session_id: string | null
  bridge?: boolean
  mentions?: MentionRef[]
  mode?: 'auto' | 'single' | 'roles' | 'roles_analyst'    // how the chat runs it; absent: the host's default
}

/* The SSE events of one turn, as chatEvents() yields them. */
export type HostEvent =
  | { type: 'ask_selection'; ask_id: string; selection: SelectionQuestion }
  | ({ type: 'view_image' } & ViewImage)
  | { type: 'image_notice'; reason: string }
  | ({ type: 'plan' } & TaskPlan)
  | ({ type: 'questions' } & DesignerForm)
  | { type: 'session'; id: string }
  | { type: 'token'; text: string }
  | { type: 'spec'; spec: TaskSpec; card: string; errors: SpecError[]; reconcile: ReconcileResult | null;
      run?: 'confirm' | 'auto'; run_reason?: string; assistant_selection?: AssistantSelection;
      reconcile_error?: { error: string; message?: string } }
  | { type: 'execution'; execution: ExecutionResult }
  | ({ type: 'mentions' } & MentionReport)   // what the host found for the message's picks
  | StageEvent | ToolEvent | UsageEvent      // what the host did: its stage, each tool call, each model call's tokens
  | { type: 'error'; detail: string }
  | { type: 'done' }

export interface SelectionQuestion { prompt: string; mode: 'single' | 'multiple' | 'box'; category?: string }
export interface AssistantSelection { tool: string; count: number | null; ids: string[] }
export interface ViewImage { capture_id: string; png_base64: string; width: number; height: number; view_name: string }

export interface HostFunction {
  name: string
  class: 'ui_only' | 'read_only'
  description: string
  parameters: { name: string; type: string; required: boolean; default?: unknown }[]
}

/* -- phase 6: confirmed execution, evidence and v1 packs (the /api/v1/bridge contract) -- */

/* POST /spec/confirm: the designer confirmed the card; the token stays in the browser. */
export interface ConfirmResponse {
  token: string
  spec_hash: string
  expires_at: string
  card: string
}

/* The pack validator's report, as it appears in an execution result and in the ledger. */
export interface ValidationReport {
  validator: string
  passed: boolean
  checks: Array<{ detail: string; passed?: boolean }>
  before?: Record<string, unknown>
  after?: Record<string, unknown>
}

/* One line of the evidence ledger: GET /evidence. */
export interface EvidenceRecord {
  id: string
  ts: string
  host: string
  action: 'run_tool' | 'execute_code' | string
  tool: string | null
  tool_version: string | null
  spec_hash: string | null
  projection_hash: string | null
  token_prefix: string | null
  confirmed_by: string | null
  channel: string | null
  params: Record<string, unknown> | null
  code_sha256: string | null
  code_head: string | null
  // fingerprint: the snapshot fingerprint at execution time (revit-bridge 0.4.1+; null when it could not be read)
  document: { title?: string; revit_version?: string; fingerprint?: string | null } | null
  success: boolean
  error: string | null
  result_summary: Record<string, unknown> | null
  validation: ValidationReport | null
  duration_ms: number | null
  preconditions_failed: string[]
  warnings: string[]
  // revit-bridge 0.9.1: only on a run_tool line whose pack refused the input in Revit (nothing kept)
  refused?: true
}

/* POST /evidence/{id}/validate: the recorded assertion re-run against the model now. */
export interface RevalidateReport extends ValidationReport {
  evidence_id: string
  tool: string
}

/* A v1 pack parameter as POST /solidify and PUT /tools/{name} take it. */
export interface PackParameter {
  name: string
  type: string
  description?: string
  source: string          // designer | tool:<query> | answer | default
  required: boolean
  unit?: string | null
  choices_from?: string
  default?: unknown
}

export interface SolidifyRequest {
  name: string
  code: string
  description: string
  parameters: PackParameter[]
  source_query: string
  validator?: Record<string, unknown> | null
}

/* A rejected request: {error, message?} plus what the endpoint adds (spec errors, pack problems, sandbox warnings). */
export interface ApiErrorBody {
  error: string
  message?: string
  errors?: Array<{ code: string; param: string | null; message: string }>
  problems?: string[]
  warnings?: string[]
  [key: string]: unknown
}

/* GET /api/usage/runs: the model calls of each run label (X-Run-Label). A token sum is null
   when any call of it lacks the count; by_role has the same per role and model (a host
   before it sends none). */
export interface UsagePart {
  role: string
  model: string | null
  calls: number
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  ms: number
}

export interface UsageRun {
  plan_id?: string | null
  step_id?: string | null
  run_label: string
  calls: number
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  ms: number
  first_at: string
  last_at: string
  by_role?: UsagePart[]
  run?: string | null
  mode?: string | null     // the chat mode the run's calls ran in; 'mixed' for more than one; null or absent: not recorded
}
