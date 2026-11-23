/* The workbench's HTTP client: every call the workbench makes goes through this one module.

   `workbenchApi` satisfies `WorkbenchApi`, the interface the workbench component takes (its
   text is copied below from the component's specification until the component's own types are
   merged; the names must stay as they are). Each method is a thin call of a v1 route:

   check      POST /check     the package's check_code reply as it is
   dryRun     POST /dry-run   the package's dry_run_code reply as it is (no token)
   choices    POST /choices   {choices_from}: the package's get_tool_choices for one parameter
   save       POST /solidify  the draft as a v1 pack (parameters with source, choices_from, unit)
   list       GET  /tools     {name, description, builtin, origin, directory?} (read from disk)
   load       GET  /tools/{name}             the pack as a draft
   remove     DELETE /tools/{name}
   exportYaml GET  /tools/{name}/export      the pack file as stored (text)
   importYaml POST /tools/import[?overwrite=true]  a pack file (text)

   A draft with parameters is a pack template: its code reads them as {name} placeholders, the
   way a saved pack does. check and dryRun send it as {pack, values} (draftPack below) and the
   server has the package fill the template - with `values` for a dry run, and for a compile check
   with sample values (each parameter's default, else 0, "" or false), since compiling needs
   values but not the designer's. Nothing here escapes or substitutes anything. A draft without
   parameters goes as {code}.

   Errors are BridgeApiError (components/shared/http.ts): the server's {error, message?, ...}
   kept, the message "<status>: <error>: <message>". */

import { getConfig } from '../config'
import { adminHeaders, deviceHeaders } from '../store'
import { BridgeApiError, del, get, post, request } from '../components/shared/http'
import type {
  PackParameter, SolidifyRequest, SolidifyResponse, ToolDetail, ToolInfo, ToolParam,
} from '../types/api'

/* -- the workbench component's interface (copied; do not rename) --------------------------- */

export interface ToolParamDraft {
  name: string; type: 'string' | 'double' | 'int' | 'bool'
  description: string
  source: 'designer' | 'levels' | 'family_types' | 'elements'
  category?: string            // OST_* , when source is family_types or elements
  unit?: 'mm' | 'm' | 'feet'
  required: boolean; default?: unknown
}
export interface ToolDraft {
  name: string; description: string; code: string
  parameters: ToolParamDraft[]; validator?: string
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

/* -- what this client adds ------------------------------------------------------------------- */

/* A list item: the interface's fields plus the directory the file is in (a local host only). */
export interface WorkbenchTool { name: string; description: string; builtin: boolean; origin: string; directory?: string }

/* POST /tools/import's answer. */
export interface ImportResult { status: string; name: string; version: string; replaced: boolean; revit_synced: boolean }

const B = '/api/v1/bridge'
const enc = encodeURIComponent
const UNITS: NonNullable<ToolParamDraft['unit']>[] = ['mm', 'm', 'feet']
// The package's pack types that read as each draft type (capabilities/schema.py PARAM_TYPES).
const TYPE_OF: Record<string, ToolParamDraft['type']> = {
  string: 'string', double: 'double', number: 'double', float: 'double', int: 'int', integer: 'int', bool: 'bool', boolean: 'bool',
}

const empty = (value: unknown) => value === undefined || value === null || value === ''

/* A draft parameter as a v1 pack parameter (the shape POST /solidify validates). */
export function packParameter(p: ToolParamDraft): PackParameter {
  const out: PackParameter = { name: p.name, type: p.type, description: p.description, source: 'designer', required: p.required }
  if (p.source === 'levels') {
    out.source = 'tool:levels'
    out.choices_from = 'levels'
  } else if (p.source === 'family_types' || p.source === 'elements') {
    out.source = `tool:${p.source}`
    out.choices_from = `${p.source}:${p.category ?? ''}`
  }
  if (p.unit) out.unit = p.unit
  if (!empty(p.default)) out.default = p.default
  return out
}

/* A stored pack parameter as a draft parameter. floor_types (the one source a draft cannot name)
   reads as the floor family types. */
export function draftParameter(p: ToolParam): ToolParamDraft {
  const from = p.choices_from ?? (p.source?.startsWith('tool:') ? p.source.slice(5) : '')
  const [kind, category] = from.split(':', 2)
  const draft: ToolParamDraft = {
    name: p.name, type: TYPE_OF[String(p.type ?? 'string').toLowerCase()] ?? 'string',
    description: p.description ?? '', source: 'designer', required: p.required ?? !('default' in p),
  }
  if (kind === 'levels') draft.source = 'levels'
  else if (kind === 'family_types' || kind === 'elements') { draft.source = kind; draft.category = category }
  else if (kind === 'floor_types') { draft.source = 'family_types'; draft.category = 'OST_Floors' }
  if (p.unit && (UNITS as string[]).includes(p.unit)) draft.unit = p.unit as ToolParamDraft['unit']
  if ('default' in p && p.default !== undefined) draft.default = p.default
  return draft
}

/* The draft's validator field (JSON text) as the pack's validator object; empty is none. */
export function parseValidator(text: string | undefined): Record<string, unknown> | null {
  if (!text || !text.trim()) return null
  let parsed: unknown
  try { parsed = JSON.parse(text) } catch { parsed = null }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('The validator must be a JSON object, e.g. {"kind": "created_ids", "category": "OST_Walls"}')
  }
  return parsed as Record<string, unknown>
}

/* The name a draft has while it is checked or dry-run (the workbench passes no name then;
   the server only fills the template with it, in a temporary store). */
export const DRAFT_NAME = 'workbench_draft'

/* A draft as the pack the server's /check and /dry-run take (the v1 pack format). */
export function draftPack(code: string, parameters: ToolParamDraft[]): Record<string, unknown> {
  return { schema_version: 1, name: DRAFT_NAME, version: '1.0.0', code_template: code, parameters: parameters.map(packParameter) }
}

/* Values that compile: each parameter's default, else a neutral value of its type. */
export function sampleValues(parameters: ToolParamDraft[]): Record<string, unknown> {
  const neutral = { string: '', double: 0, int: 0, bool: false } as const
  return Object.fromEntries(parameters.map(p => [p.name, empty(p.default) ? neutral[p.type] : p.default]))
}

/* The choices_from of a draft parameter whose options come from the model; null for a designer's value. */
export function choicesFromOf(p: ToolParamDraft): string | null {
  if (p.source === 'levels') return 'levels'
  if (p.source === 'family_types' || p.source === 'elements') return `${p.source}:${p.category ?? ''}`
  return null
}

/* GET as text: the export is the pack file, not JSON. */
async function getText(path: string): Promise<string> {
  const resp = await fetch(`${getConfig().apiBase}${path}`, { headers: { ...deviceHeaders(), ...adminHeaders() } })
  const raw = await resp.text().catch(() => '')
  if (!resp.ok) throw new BridgeApiError(resp.status, raw, resp.headers.get('content-type') || '')
  return raw
}

export const workbenchApi = {
  check: (code: string, parameters: ToolParamDraft[]): Promise<unknown> =>
    post<unknown>(`${B}/check`, parameters.length
      ? { pack: draftPack(code, parameters), values: sampleValues(parameters) }
      : { code }),

  dryRun: (code: string, parameters: ToolParamDraft[], values: Record<string, unknown>): Promise<unknown> =>
    post<unknown>(`${B}/dry-run`, parameters.length ? { pack: draftPack(code, parameters), values } : { code }),

  choices: async (param: ToolParamDraft): Promise<{ label: string; value: unknown }[]> => {
    const from = choicesFromOf(param)
    return from ? post<{ label: string; value: unknown }[]>(`${B}/choices`, { choices_from: from }) : []
  },

  save: async (draft: ToolDraft): Promise<{ name: string }> => {
    const payload: SolidifyRequest = {
      name: draft.name, code: draft.code, description: draft.description,
      parameters: draft.parameters.map(packParameter), source_query: '', validator: parseValidator(draft.validator),
    }
    const saved = await post<SolidifyResponse>(`${B}/solidify`, payload)
    return { name: saved.name }
  },

  list: async (): Promise<WorkbenchTool[]> =>
    (await get<ToolInfo[]>(`${B}/tools`)).map(t => ({
      name: t.name, description: t.description, builtin: t.builtin, origin: t.origin,
      ...(t.directory ? { directory: t.directory } : {}),
    })),

  load: async (name: string): Promise<ToolDraft> => {
    const detail = await get<ToolDetail>(`${B}/tools/${enc(name)}`)
    const draft: ToolDraft = {
      name: detail.name, description: detail.description, code: detail.code_template,
      parameters: (detail.parameters ?? []).map(draftParameter),
    }
    if (detail.validator) draft.validator = JSON.stringify(detail.validator)
    return draft
  },

  remove: async (name: string): Promise<void> => { await del<unknown>(`${B}/tools/${enc(name)}`) },

  exportYaml: (name: string): Promise<string> => getText(`${B}/tools/${enc(name)}/export`),

  importYaml: (text: string, options: { overwrite?: boolean } = {}): Promise<ImportResult> =>
    request<ImportResult>(`${B}/tools/import${options.overwrite ? '?overwrite=true' : ''}`, {
      method: 'POST', body: text, headers: { 'Content-Type': 'text/yaml' },
    }),
} satisfies WorkbenchApi
