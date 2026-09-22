/* Shared API types (mirrors docs/api-v1.json). */

export interface RevitHealthResponse {
  revit_connected: boolean
  latency_ms: number | null
  detail: string
  mode: string
  endpoint?: string
  protocol?: string
  bridge_version: string
  timestamp: string
  ws_slots: SlotsStatus
}

export interface SlotInfo {
  status: string
  connected_at?: number
  requests?: number
}

export interface SlotsStatus {
  max_slots: number
  connected: number
  slots: Record<string, SlotInfo>
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

/* One item of GET /tools: the package's list_tools shape. */
export interface ToolInfo {
  name: string
  description: string
  version: string
  parameters: ToolParam[]
  preconditions: Array<Record<string, unknown>>
  validator: string | null
  used: number
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

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
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

export interface Question {
  id: string
  param: string
  text: string
  why?: string
  options: Array<{ label: string; value: unknown; source?: string }>
  allow_other?: boolean
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
}

/* One turn of the host loop: exactly one of message / execution. */
export interface HostRequest {
  message?: string
  execution?: ExecutionResult
  session_id: string | null
  bridge?: boolean
}

/* The SSE events of one turn, as chatEvents() yields them. */
export type HostEvent =
  | { type: 'session'; id: string }
  | { type: 'token'; text: string }
  | { type: 'spec'; spec: TaskSpec; card: string; errors: SpecError[]; reconcile: ReconcileResult | null;
      reconcile_error?: { error: string; message?: string } }
  | { type: 'execution'; execution: ExecutionResult }
  | { type: 'error'; detail: string }
  | { type: 'done' }
