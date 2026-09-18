/* Shared API types (mirrors docs/api-v0.json). */

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

export interface ProjectUnits {
  revit_unit?: string
  display_name?: string
  detected?: string
  error?: string
  current_setting: string
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
