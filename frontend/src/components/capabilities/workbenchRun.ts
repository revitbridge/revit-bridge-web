/* Run on the workbench page. The workbench asks for a run of its draft (its onRun); what runs
   is the saved tool of that name, through the gate the Packs page uses - a spec card,
   /spec/confirm, /tools/{name}/run under the token, the validator, the evidence ledger. This
   host runs no free code (the chat proposes none, the task page has no such path), so a draft
   that is not the saved tool is refused with the reason: save it first.

   WorkbenchRun holds the card. The card belongs to the draft it was built from: any change to
   the draft or its values drops a card still waiting for Confirm, and Confirm reads the saved
   tool again and refuses when it is no longer that draft. While a confirmed run is in progress
   Run is refused and its result is always shown when it comes. */

import { uiText } from '../../utils/strings'
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
export const RUN_IN_PROGRESS = 'A run is in progress; wait for its result.'

/* What decides what runs, per parameter: name, type, where the value comes from, required, the
   default. Not the description, nor the unit (the package may add mm from a description). */
const shape = (p: ToolParamDraft) => JSON.stringify([p.name, p.type, p.source, p.category ?? null, p.required, p.default === undefined ? '<none>' : p.default])

const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical)
  : value && typeof value === 'object'
    ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical((value as Record<string, unknown>)[key])]))
    : value

/* The draft's validator text and the saved validator object as one comparable text. */
function draftValidator(text: string | undefined): string {
  if (!text || !text.trim()) return 'null'
  try { return JSON.stringify(canonical(JSON.parse(text))) } catch { return `not JSON: ${text}` }
}

/* Why the draft is not the saved tool, naming the part, or null when it is: the same code (line
   ends aside), the same parameter declarations, the same validator (as JSON, key order aside). */
export function differsFromSaved(draft: ToolDraft, saved: ToolDetail): string | null {
  if (normalizeCode(draft.code) !== normalizeCode(saved.code_template)) return uiText("its code differs")
  const mine = typedParameters(draft.parameters)
  const theirs = typedParameters((saved.parameters ?? []).map(draftParameter))
  if (mine.length !== theirs.length) return uiText("it declares {0} parameter(s), the saved tool {1}", mine.length, theirs.length)
  const index = mine.findIndex((param, i) => shape(param) !== shape(theirs[i]))
  if (index >= 0) return uiText("its parameter {0} differs", mine[index].name || index + 1)
  if (draftValidator(draft.validator) !== JSON.stringify(canonical(saved.validator ?? null))) return uiText("its validator differs")
  return null
}

/* A parameter left without a value whose default the tool fills: shown on the card, not in it. */
export interface DefaultUsed { name: string; value: unknown; unit: string | null }

export interface RunCard { spec: TaskSpec; defaults: DefaultUsed[] }

/* The spec card for running the saved tool with the workbench's values. A value of a parameter
   whose options come from the model is one of /choices' options (the workbench takes nothing
   else), so it is bound as a tool value; the rest as the Packs page binds them. */
export async function workbenchSpec(api: Pick<WorkbenchRunApi, 'getTool'>, draft: ToolDraft,
                                    values: Record<string, unknown>): Promise<RunCard> {
  const name = draft.name.trim()
  if (!name) throw new Error(uiText("The draft has no name. {0}", uiText(SAVE_FIRST)))
  let saved: ToolDetail
  try {
    saved = await api.getTool(name)
  } catch (e: unknown) {
    if (e instanceof BridgeApiError && e.status === 404 && e.code === 'unknown_tool') throw new Error(uiText("There is no saved tool named {0}. {1}", name, uiText(SAVE_FIRST)))
    throw e
  }
  const difference = differsFromSaved(draft, saved)
  if (difference) throw new Error(uiText("This draft is not the saved {0}: {1}. {2}", name, difference, uiText(SAVE_FIRST)))
  const params = saved.parameters ?? []
  const texts: Record<string, string> = {}
  const picked: Record<string, ToolChoiceItem[]> = {}
  const defaults: DefaultUsed[] = []
  for (const p of params) {
    if (!Object.hasOwn(values, p.name)) {
      if (!isToolSourced(p) && 'default' in p) defaults.push({ name: p.name, value: p.default ?? null, unit: p.unit ?? null })
      continue
    }
    const value = values[p.name]
    texts[p.name] = String(value)
    if (isToolSourced(p)) picked[p.name] = [{ label: String(value), value: value as string | number }]
  }
  return { spec: specFromForm({ name: saved.name, display_name: saved.display_name }, params, texts, picked), defaults }
}

export type RunOutcome =
  | { kind: 'rejected'; errors: SpecError[] }
  | { kind: 'executed'; token: string; expires_at: string; result: ExecutionResult }
  | { kind: 'failed'; message: string; details: string[] }

/* Confirm and run - after reading the saved tool again: when it is no longer the draft the card
   was built from, nothing is confirmed (stale). Never throws, so the button always leaves its
   busy state. */
export async function confirmRun(api: WorkbenchRunApi, spec: TaskSpec, draft: ToolDraft): Promise<RunOutcome | { kind: 'stale'; message: string }> {
  const name = spec.action.tool ?? ''
  try {
    let saved: ToolDetail
    try {
      saved = await api.getTool(name)
    } catch (e: unknown) {
      if (e instanceof BridgeApiError && e.status === 404 && e.code === 'unknown_tool') return { kind: 'stale', message: uiText("{0} is no longer saved. Nothing was run.", name) }
      throw e
    }
    const difference = differsFromSaved(draft, saved)
    if (difference) return { kind: 'stale', message: uiText("The saved {0} changed after its card was shown ({1}). Nothing was run; run again.", name, difference) }
    const outcome = await confirmAndRun(api, spec)
    return outcome.status === 'rejected'
      ? { kind: 'rejected', errors: outcome.errors }
      : { kind: 'executed', token: outcome.token, expires_at: outcome.expires_at, result: outcome.result }
  } catch (e: unknown) {
    return { kind: 'failed', message: getErrorMessage(e), details: errorDetails(e) }
  }
}

export interface RunView extends RunCard { draft: ToolDraft; running: boolean; outcome: RunOutcome | null }
export interface RunState { card: RunView | null; notice: string | null }

/* The page's run: the card (waiting, running, or with its outcome) and a notice when a card was
   dropped. useSyncExternalStore reads it. */
export class WorkbenchRun {
  private state: RunState = { card: null, notice: null }
  private listeners = new Set<() => void>()
  private generation = 0        // every change to the workbench's draft or values counts one
  private confirming = false    // not render state: a double click confirms once
  private api: WorkbenchRunApi

  constructor(api: WorkbenchRunApi) { this.api = api }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(patch: Partial<RunState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(listener => listener()) }
  get running() { return this.confirming }

  /* The workbench's draft or values changed: a card still waiting for Confirm was built from
     the draft as it was and goes; a running or finished run keeps its card and outcome. */
  draftChanged = () => {
    this.generation++
    const card = this.state.card
    if (card && !card.running && !card.outcome) this.update({ card: null, notice: null })
    else if (this.state.notice) this.update({ notice: null })
  }

  /* The workbench's Run. Throws the reason it refuses (the workbench shows it on its Run line). */
  onRun = async (draft: ToolDraft, values: Record<string, unknown>) => {
    if (this.confirming) throw new Error(uiText(RUN_IN_PROGRESS))
    const generation = this.generation
    this.update({ card: null, notice: null })
    const card = await workbenchSpec(this.api, draft, values)
    if (generation !== this.generation) throw new Error('The draft changed while the run was being prepared. Run again.')
    if (this.confirming) throw new Error(uiText(RUN_IN_PROGRESS))
    this.update({ card: { ...card, draft: structuredClone(draft), running: false, outcome: null } })
  }

  confirm = async () => {
    const card = this.state.card
    if (!card || card.running || card.outcome || this.confirming) return
    this.confirming = true
    const running: RunView = { ...card, running: true }
    this.update({ card: running })
    try {
      const outcome = await confirmRun(this.api, card.spec, card.draft)
      if (outcome.kind === 'stale') this.update({ card: null, notice: outcome.message })
      else this.update({ card: { ...running, running: false, outcome } })
    } finally {
      this.confirming = false
      if (this.state.card?.running) this.update({ card: { ...running, running: false, outcome: { kind: 'failed', message: 'Stopped before it finished.', details: [] } } })
    }
  }

  close = () => {
    if (this.confirming) return
    this.update({ card: null, notice: null })
  }
}
