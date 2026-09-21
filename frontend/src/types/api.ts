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
  choices_from?: string
}

export interface ToolInfo {
  name: string
  display_name: string
  description: string
  parameters: ToolParam[]
  tags: string[]
  execution_count: number
}

export interface ToolDetail extends ToolInfo {
  code_template: string
  source_query: string
  preconditions: string[]
  applies_when: string[]
  not_for: string[]
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
