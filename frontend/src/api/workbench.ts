/* The workbench's HTTP client: every call the workbench makes goes through this one module.

   `workbenchApi` satisfies `WorkbenchApi`, the interface the workbench component takes
   (types/hostv2.ts, re-exported here). Each method is a thin call of a v1 route:

   check      POST /check     the package's check_code reply as it is
   dryRun     POST /dry-run   the package's dry_run_code reply as it is (no token)
   choices    POST /choices   {choices_from}: the package's get_tool_choices for one parameter
   lookup     GET  /tools/{name}             {name, dropped}: what a save over it loses; null on 404
   save       POST /solidify  the draft as a v1 pack (parameters with source, choices_from, unit)
   list       GET  /tools     {name, description, builtin, origin, directory?} (read from disk)
   load       GET  /tools/{name}             the pack as a draft
   remove     DELETE /tools/{name}
   exportYaml GET  /tools/{name}/export      the pack file as stored (text)
   importYaml POST /tools/import[?overwrite=true]  a pack file (text)

   A draft is a pack template: its code reads its parameters as {name} placeholders, the way a
   saved pack does. check and dryRun always send it as {pack, values} (draftPack below), also
   without parameters, and the server has the package fill the template - with `values` for a
   dry run, and for a compile check with sample values (each parameter's default, else 0, "" or
   false), since compiling needs values but not the designer's. So a leftover {x} (a C#
   $"{x}" with no parameter x) is refused there (422 render_failed) as it would be when the saved
   tool runs. Nothing here escapes or substitutes anything.

   Saving an opened tool keeps what the save route can carry and the workbench does not edit:
   source_query, and each parameter's source, choices_from and type spelling while the designer
   leaves its source as shown. What the route cannot carry is listed by lookup (droppedOnSave)
   so the workbench can name it before a replace.

   Errors are BridgeApiError (components/shared/http.ts): the server's {error, message?, ...}
   kept, the message "<status>: <error>: <message>". */

import { getConfig } from '../config'
import { adminHeaders, deviceHeaders } from '../store'
import { BridgeApiError, del, get, post, request } from '../components/shared/http'
import type {
  PackParameter, SolidifyRequest, SolidifyResponse, ToolDetail, ToolInfo, ToolParam,
} from '../types/api'
import type { ExistingTool, ToolDraft, ToolParamDraft, WorkbenchApi, WorkbenchTool } from '../types/hostv2'

export type { ExistingTool, ToolDraft, ToolParamDraft, WorkbenchApi, WorkbenchTool }

/* POST /tools/import's answer (the interface's WorkbenchImport and what else the route says). */
export interface ImportResult { status: string; name: string; version: string; replaced: boolean; revit_synced: boolean }

const B = '/api/v1/bridge'
const enc = encodeURIComponent
const UNITS: NonNullable<ToolParamDraft['unit']>[] = ['mm', 'm', 'feet']
// The package's pack types that read as each draft type (capabilities/schema.py PARAM_TYPES).
const TYPE_OF: Record<string, ToolParamDraft['type']> = {
  string: 'string', double: 'double', number: 'double', float: 'double', int: 'int', integer: 'int', bool: 'bool', boolean: 'bool',
}

const empty = (value: unknown) => value === undefined || value === null || value === ''

/* The source and choices_from of the opened tool's parameter, when the designer left its source
   (and category) as the workbench showed it - so floor_types, answer and default survive a save
   although the workbench shows them as family types of OST_Floors, levels or designer. */
function keptSource(p: ToolParamDraft): { source: string; choices_from?: string } | null {
  const loaded = p.loaded
  if (!loaded || typeof loaded.source !== 'string') return null
  const shown = draftParameter(loaded as unknown as ToolParam)
  if (shown.source !== p.source || (shown.category ?? '') !== (p.category ?? '')) return null
  return typeof loaded.choices_from === 'string' ? { source: loaded.source, choices_from: loaded.choices_from } : { source: loaded.source }
}

/* A draft parameter as a v1 pack parameter (the shape POST /solidify validates). A default of
   null is kept: it is a default (the package fills null when no value is given). */
export function packParameter(p: ToolParamDraft): PackParameter {
  const out: PackParameter = { name: p.name, type: p.type, description: p.description, source: 'designer', required: p.required }
  if (p.source === 'levels') {
    out.source = 'tool:levels'
    out.choices_from = 'levels'
  } else if (p.source === 'family_types' || p.source === 'elements') {
    out.source = `tool:${p.source}`
    out.choices_from = `${p.source}:${p.category ?? ''}`
  }
  const kept = keptSource(p)
  if (kept) {
    out.source = kept.source
    if (kept.choices_from === undefined) delete out.choices_from
    else out.choices_from = kept.choices_from
  }
  // The type as the pack spells it (integer, number...) while the designer keeps its meaning.
  const loadedType = p.loaded?.type
  if (typeof loadedType === 'string' && TYPE_OF[loadedType.toLowerCase()] === p.type) out.type = loadedType
  if (p.unit) out.unit = p.unit
  if (p.default !== undefined && p.default !== '') out.default = p.default
  if (out.source === 'default' && !('default' in out)) out.source = 'designer'   // the default was removed
  return out
}

/* A stored pack parameter as a draft parameter, the stored one kept in `loaded`. floor_types
   (the one source a draft cannot name) reads as the floor family types; answer and default read
   as the designer's value (packParameter puts them back). */
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
  draft.loaded = structuredClone(p) as unknown as Record<string, unknown>
  return draft
}

/* What a save over `detail` loses. POST /solidify carries the name, code, description,
   parameters, source_query and validator; the package then writes the display name from the
   name, version 1.0.0 and a new created_at, and no preconditions, applies_when, not_for or
   (0.1) tags. revit_versions, fixtures and the approval are lost too but GET /tools/{name} does
   not show them: UNSEEN_ON_SAVE names them for the question. */
export function droppedOnSave(detail: ToolDetail): string[] {
  const dropped: string[] = []
  const count = (list: unknown) => Array.isArray(list) ? list.length : 0
  if (detail.display_name && detail.display_name !== defaultDisplayName(detail.name)) dropped.push(`display name "${detail.display_name}"`)
  if (detail.version && detail.version !== '1.0.0') dropped.push(`version ${detail.version} (saved again as 1.0.0)`)
  for (const [key, list] of [['preconditions', detail.preconditions], ['applies_when', detail.applies_when], ['not_for', detail.not_for], ['tags', detail.tags]] as const) {
    if (count(list)) dropped.push(`${key} (${count(list)})`)
  }
  return dropped
}

export const UNSEEN_ON_SAVE = ['revit_versions', 'fixtures', 'approved_by', 'approved_at']

/* The package's display name for a pack name: name.replace("_", " ").title(). */
export function defaultDisplayName(name: string): string {
  return name.replace(/_/g, ' ').toLowerCase().replace(/(^|[^a-z])([a-z])/g, (_, before: string, letter: string) => before + letter.toUpperCase())
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

/* The choices_from of a draft parameter whose options come from the model (the opened tool's
   own while its source is as loaded, so the options are what the saved tool gets); null for a
   designer's value. */
export function choicesFromOf(p: ToolParamDraft): string | null {
  if (p.source === 'designer') return null
  const kept = keptSource(p)
  if (kept?.choices_from) return kept.choices_from
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
    post<unknown>(`${B}/check`, { pack: draftPack(code, parameters), values: sampleValues(parameters) }),

  dryRun: (code: string, parameters: ToolParamDraft[], values: Record<string, unknown>): Promise<unknown> =>
    post<unknown>(`${B}/dry-run`, { pack: draftPack(code, parameters), values }),

  choices: async (param: ToolParamDraft): Promise<{ label: string; value: unknown }[]> => {
    const from = choicesFromOf(param)
    return from ? post<{ label: string; value: unknown }[]>(`${B}/choices`, { choices_from: from }) : []
  },

  /* Asked before a save, which replaces a tool of yours of that name without asking. */
  lookup: async (name: string): Promise<ExistingTool | null> => {
    let detail: ToolDetail
    try {
      detail = await get<ToolDetail>(`${B}/tools/${enc(name)}`)
    } catch (e: unknown) {
      if (e instanceof BridgeApiError && e.status === 404 && e.code === 'unknown_tool') return null
      throw e
    }
    return { name: detail.name, dropped: droppedOnSave(detail), unseen: [...UNSEEN_ON_SAVE] }
  },

  save: async (draft: ToolDraft): Promise<{ name: string }> => {
    const payload: SolidifyRequest = {
      name: draft.name, code: draft.code, description: draft.description,
      parameters: draft.parameters.map(packParameter), source_query: draft.keep?.source_query ?? '',
      validator: parseValidator(draft.validator),
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
    if (detail.source_query) draft.keep = { source_query: detail.source_query }
    return draft
  },

  remove: async (name: string): Promise<void> => { await del<unknown>(`${B}/tools/${enc(name)}`) },

  exportYaml: (name: string): Promise<string> => getText(`${B}/tools/${enc(name)}/export`),

  importYaml: (text: string, options: { overwrite?: boolean } = {}): Promise<ImportResult> =>
    request<ImportResult>(`${B}/tools/import${options.overwrite ? '?overwrite=true' : ''}`, {
      method: 'POST', body: text, headers: { 'Content-Type': 'text/yaml' },
    }),
} satisfies WorkbenchApi
