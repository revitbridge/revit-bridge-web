/* Running a pack from the Capabilities page: the values the designer filled in
   become a TaskSpec (every binding with its source and evidence), which goes
   through the same gate as the Task page - /spec/confirm, then /tools/{name}/run
   with the token. Pure functions, so the spec building is unit-tested. */

import type { ConfirmResponse, ExecutionResult, SpecError, TaskSpec, ToolChoiceItem, ToolParam } from '../../types/api'
import { TaskApiError } from '../task/api'
import { paramsOf } from '../task/flow'

const NUMERIC = ['double', 'number', 'float', 'int', 'integer']
const EVIDENCE = 'capabilities page'

/* {kind: levels_min, value} / {kind: category_present, category} / {text} as one line. */
export function describePreconditions(items: Array<Record<string, unknown>>): string {
  if (!items.length) return '-'
  return items.map(p => {
    if (p.kind === 'levels_min') return `levels >= ${String(p.value)}`
    if (p.kind === 'category_present') return `has ${String(p.category)}`
    if (typeof p.text === 'string') return p.text
    return JSON.stringify(p)
  }).join('; ')
}

export function isToolSourced(p: ToolParam): boolean {
  return Boolean(p.choices_from) || String(p.source ?? '').startsWith('tool:')
}

/* The typed text as the pack declares the parameter: numbers numeric, booleans boolean. */
export function typedValue(p: ToolParam, text: string): unknown {
  const type = String(p.type ?? 'string').toLowerCase()
  if (NUMERIC.includes(type)) {
    const n = Number(text)
    return text.trim() !== '' && Number.isFinite(n) ? n : text
  }
  if (type === 'bool' || type === 'boolean') return ['true', '1', 'yes'].includes(text.trim().toLowerCase())
  return text
}

export function specFromForm(
  tool: { name: string; display_name?: string },
  params: ToolParam[],
  values: Record<string, string>,
  choices: Record<string, ToolChoiceItem[]>,
): TaskSpec {
  const parameters: TaskSpec['parameters'] = []
  for (const p of params) {
    const text = values[p.name]
    if (text === undefined || text === '') continue
    const value = typedValue(p, text)
    const picked = (choices[p.name] ?? []).some(c => String(c.value) === text)
    let source = 'designer'
    let evidence = EVIDENCE
    if (isToolSourced(p)) {
      // picked from the list Revit gave: a tool value; typed by hand: the designer's answer
      const query = p.choices_from || String(p.source).slice(5)
      source = picked ? 'tool' : 'answer'
      evidence = picked ? `tool:${query}` : EVIDENCE
    } else if (p.source === 'default' && p.default !== undefined && String(p.default) === text) {
      source = 'default'
      evidence = `default:${tool.name}`
    }
    parameters.push({ name: p.name, value, unit: p.unit ?? null, source, evidence })
  }
  return {
    task: `Run ${tool.display_name || tool.name} with the values filled in on the capabilities page`,
    action: { kind: 'run_tool', tool: tool.name },
    parameters,
    interpretations: [],
    language: 'en',
  }
}

export interface DirectRunApi {
  confirm(spec: TaskSpec): Promise<ConfirmResponse>
  runTool(name: string, params: Record<string, unknown>, token: string): Promise<ExecutionResult>
}

export type DirectRunOutcome =
  | { status: 'rejected'; errors: SpecError[] }
  | { status: 'executed'; token: string; expires_at: string; result: ExecutionResult }

/* Confirm, then run under the token; a 422 from confirm comes back as the spec errors. */
export async function confirmAndRun(api: DirectRunApi, spec: TaskSpec): Promise<DirectRunOutcome> {
  let issued: ConfirmResponse
  try {
    issued = await api.confirm(spec)
  } catch (e: unknown) {
    if (e instanceof TaskApiError && e.body?.errors?.length) return { status: 'rejected', errors: e.body.errors }
    throw e
  }
  const result = await api.runTool(spec.action.tool ?? '', paramsOf(spec), issued.token)
  return { status: 'executed', token: issued.token, expires_at: issued.expires_at, result }
}
