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

// X3c: mention input contract. The other hostv2 packages append their own types.
export type MentionKind = 'level' | 'type' | 'link'
export interface MentionItem {
  kind: string
  id: number | string
  name: string
  label: string
  detail: string
  category?: string
  family?: string
  disabled?: boolean
}
export interface MentionCandidates {
  kind: string
  items: MentionItem[]
  total: number
  truncated: boolean
}
export interface Mention { kind: MentionKind; id: number; name: string }
export type FetchCandidates =
  (kind: MentionKind, opts: { category?: string; q?: string }) => Promise<MentionCandidates>

/* -- the tool workbench (components/hostv2/workbench; its client is api/workbench.ts) -- */

export interface ToolParamDraft {
  name: string
  type: 'string' | 'double' | 'int' | 'bool'
  description: string
  source: 'designer' | 'levels' | 'family_types' | 'elements'
  category?: string            // OST_* name, when source is family_types or elements
  unit?: 'mm' | 'm' | 'feet'
  required: boolean
  default?: unknown            // null is a default (the value null), undefined is none
  // The parameter as the opened tool declares it (from load). A save keeps its source and
  // choices_from (floor_types, answer, default...) while the designer leaves the source as shown.
  loaded?: Record<string, unknown>
}

export interface ToolDraft {
  name: string
  description: string
  code: string
  parameters: ToolParamDraft[]
  validator?: string           // the pack's validator object as JSON text; empty is none
  // What the opened tool has that the workbench does not edit and a save passes through.
  keep?: { source_query: string }
}

/* A tool already saved under a name: what saving over it would not keep (the parts of the
   saved tool the save route cannot carry), each as a short phrase; unseen: fields the save would
   not keep either but the lookup cannot see, lost only if the file has them. */
export interface ExistingTool {
  name: string
  dropped: string[]
  unseen: string[]
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
  // GET /tools/{name}: the tool saved under that name and what a save over it drops; null on
  // 404 unknown_tool.
  lookup(name: string): Promise<ExistingTool | null>
  // POST /solidify: replaces a tool of yours of that name; a built-in's name is refused (409).
  save(draft: ToolDraft): Promise<{ name: string }>
  list(): Promise<WorkbenchTool[]>
  load(name: string): Promise<ToolDraft>
  remove(name: string): Promise<void>
  exportYaml(name: string): Promise<string>
  // Without overwrite a tool of yours of that name is refused (409 pack_exists).
  importYaml(text: string, options?: { overwrite?: boolean }): Promise<WorkbenchImport>
}
