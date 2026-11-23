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

/* -- the tool workbench (components/hostv2/workbench; its client is api/workbench.ts) -- */

export interface ToolParamDraft {
  name: string
  type: 'string' | 'double' | 'int' | 'bool'
  description: string
  source: 'designer' | 'levels' | 'family_types' | 'elements'
  category?: string            // OST_* name, when source is family_types or elements
  unit?: 'mm' | 'm' | 'feet'
  required: boolean
  default?: unknown
}

export interface ToolDraft {
  name: string
  description: string
  code: string
  parameters: ToolParamDraft[]
  validator?: string           // the pack's validator object as JSON text; empty is none
}

/* One tool of GET /tools (read from disk on every call): origin 'builtin' (the package's,
   read-only) or 'user' (a file in the tool directory); directory on a local host only. */
export interface WorkbenchTool {
  name: string
  description: string
  builtin: boolean
  origin: string
  directory?: string
}

/* What POST /tools/import answered: replaced when a tool of yours had that name. */
export interface WorkbenchImport {
  name: string
  replaced: boolean
}

export interface WorkbenchApi {
  check(code: string, parameters: ToolParamDraft[]): Promise<unknown>
  dryRun(code: string, parameters: ToolParamDraft[], values: Record<string, unknown>): Promise<unknown>
  choices(param: ToolParamDraft): Promise<{ label: string; value: unknown }[]>
  // GET /tools/{name}: whether a tool of that name is there (false on 404 unknown_tool).
  exists(name: string): Promise<boolean>
  // POST /solidify: replaces a tool of yours of that name; a built-in's name is refused (409).
  save(draft: ToolDraft): Promise<{ name: string }>
  list(): Promise<WorkbenchTool[]>
  load(name: string): Promise<ToolDraft>
  remove(name: string): Promise<void>
  exportYaml(name: string): Promise<string>
  // Without overwrite a tool of yours of that name is refused (409 pack_exists).
  importYaml(text: string, options?: { overwrite?: boolean }): Promise<WorkbenchImport>
}
