/* Run on the workbench page. The workbench asks for a run of its draft (its onRun); what runs
   is the saved tool of that name, through the gate the Packs page uses - a spec card,
   /spec/confirm, /tools/{name}/run under the token, the validator, the evidence ledger. This
   host runs no free code (the chat proposes none, the task page has no such path), so a draft
   that is not the saved tool is refused with the reason: save it first. */

import { draftParameter } from '../../api/workbench'
import type { ExecutionResult, SpecError, TaskSpec, ToolChoiceItem, ToolDetail } from '../../types/api'
import type { ToolDraft, ToolParamDraft } from '../../types/hostv2'
import { getErrorMessage } from '../../utils/errors'
import { errorDetails, typedParameters } from '../hostv2/workbench/model'
import { normalizeCode } from '../hostv2/workbench/results'
import { BridgeApiError, get } from '../shared/http'
import { taskApi } from '../task/api'
import { confirmAndRun, isToolSourced, specFromForm, type DirectRunApi } from './directRun'

export interface WorkbenchRunApi extends DirectRunApi {
  getTool(name: string): Promise<ToolDetail>
}

export const workbenchRunApi: WorkbenchRunApi = {
  getTool: name => get<ToolDetail>(`/api/v1/bridge/tools/${encodeURIComponent(name)}`),
  confirm: spec => taskApi.confirm(spec),
  runTool: (name, params, token) => taskApi.runTool(name, params, token),
}

const SAVE_FIRST = 'Save it first: Run confirms and runs the saved tool.'

/* What decides what runs, per parameter: name, type, where the value comes from, required, the
   default. Not the description, nor the unit (the package may add mm from a description). */
const shape = (p: ToolParamDraft) => JSON.stringify([p.name, p.type, p.source, p.category ?? null, p.required, p.default ?? null])

/* Why the draft is not the saved tool, or null when it is: the same code (line ends aside)
   and the same parameter declarations. */
export function differsFromSaved(draft: ToolDraft, saved: ToolDetail): string | null {
  if (normalizeCode(draft.code) !== normalizeCode(saved.code_template)) return 'its code differs'
  const mine = typedParameters(draft.parameters).map(shape)
  const theirs = typedParameters((saved.parameters ?? []).map(draftParameter)).map(shape)
  if (mine.length !== theirs.length || mine.some((item, index) => item !== theirs[index])) return 'its parameter declarations differ'
  return null
}

/* The spec card for running the saved tool with the workbench's values. A value of a parameter
   whose options come from the model is one of /choices' options (the workbench takes nothing
   else), so it is bound as a tool value; the rest as the Packs page binds them. */
export async function workbenchSpec(api: Pick<WorkbenchRunApi, 'getTool'>, draft: ToolDraft,
                                    values: Record<string, unknown>): Promise<TaskSpec> {
  const name = draft.name.trim()
  if (!name) throw new Error(`The draft has no name. ${SAVE_FIRST}`)
  let saved: ToolDetail
  try {
    saved = await api.getTool(name)
  } catch (e: unknown) {
    if (e instanceof BridgeApiError && e.status === 404 && e.code === 'unknown_tool') throw new Error(`There is no saved tool named ${name}. ${SAVE_FIRST}`)
    throw e
  }
  const difference = differsFromSaved(draft, saved)
  if (difference) throw new Error(`This draft is not the saved ${name}: ${difference}. ${SAVE_FIRST}`)
  const params = saved.parameters ?? []
  const texts: Record<string, string> = {}
  const picked: Record<string, ToolChoiceItem[]> = {}
  for (const p of params) {
    if (!Object.hasOwn(values, p.name)) continue
    const value = values[p.name]
    texts[p.name] = String(value)
    if (isToolSourced(p)) picked[p.name] = [{ label: String(value), value: value as string | number }]
  }
  return specFromForm({ name: saved.name, display_name: saved.display_name }, params, texts, picked)
}

export type RunOutcome =
  | { kind: 'rejected'; errors: SpecError[] }
  | { kind: 'executed'; token: string; expires_at: string; result: ExecutionResult }
  | { kind: 'failed'; message: string; details: string[] }

/* Confirm and run; never throws, so the page's button always leaves its busy state. */
export async function confirmRun(api: DirectRunApi, spec: TaskSpec): Promise<RunOutcome> {
  try {
    const outcome = await confirmAndRun(api, spec)
    return outcome.status === 'rejected'
      ? { kind: 'rejected', errors: outcome.errors }
      : { kind: 'executed', token: outcome.token, expires_at: outcome.expires_at, result: outcome.result }
  } catch (e: unknown) {
    return { kind: 'failed', message: getErrorMessage(e), details: errorDetails(e) }
  }
}
