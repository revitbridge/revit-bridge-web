export type Role = 'analyst' | 'planner' | 'supervisor'

/* 'interrupted' never comes from the server: the page sets it on a stage or tool that was
   still open when its turn's stream ended (aborted, broken off). */
export interface StageEvent {
  type: 'stage'
  role: Role
  model: string
  status: 'start' | 'end' | 'interrupted'
}

/* args: the arguments as the model gave them, or, when their JSON text is long,
   {_truncated: true, _chars: <length>, _head: <first 2000 characters>}. */
export interface ToolEvent {
  type: 'tool'
  id: string
  role: Role
  name: string
  args: Record<string, unknown>
  status: 'running' | 'ok' | 'error' | 'interrupted'
  ms?: number
  size?: number
  preview?: string
  error?: string
}

export interface UsageEvent {
  type: 'usage'
  role: Role
  model: string
  round: number
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  ms: number
}

export type TraceEvent = StageEvent | ToolEvent | UsageEvent

export interface ToolParamDraft {
  name: string
  type: 'string' | 'double' | 'int' | 'bool'
  description: string
  source: 'designer' | 'levels' | 'family_types' | 'elements'
  category?: string
  unit?: 'mm' | 'm' | 'feet'
  required: boolean
  default?: unknown
}

export interface ToolDraft {
  name: string
  description: string
  code: string
  parameters: ToolParamDraft[]
  validator?: string
}

export interface WorkbenchApi {
  check(code: string, parameters: ToolParamDraft[]): Promise<unknown>
  dryRun(code: string, parameters: ToolParamDraft[], values: Record<string, unknown>): Promise<unknown>
  choices(param: ToolParamDraft): Promise<{ label: string; value: unknown }[]>
  save(draft: ToolDraft): Promise<{ name: string }>
  list(): Promise<{ name: string; description: string; builtin: boolean; origin?: string }[]>
  load(name: string): Promise<ToolDraft>
  remove(name: string): Promise<void>
  exportYaml(name: string): Promise<string>
  importYaml(text: string): Promise<{ name: string }>
}
