import type { ToolDraft, ToolParamDraft, WorkbenchApi } from '../../../types/hostv2'
import { dryRunNotSent, normalizeCode, readCheck, readDryRun, type CheckReply, type DryRunReply } from './results'

export type Action = 'check' | 'dryRun' | 'run' | 'save' | 'list' | 'load' | 'choices' | 'remove' | 'export' | 'import'
// details: what the server sent with a refusal (problems, errors, warnings), line by line.
export interface ActionState { status: 'idle' | 'running' | 'success' | 'error'; message: string; details?: string[] }
export type ToolEntry = Awaited<ReturnType<WorkbenchApi['list']>>[number]
export type Choice = Awaited<ReturnType<WorkbenchApi['choices']>>[number]
export type OnRun = (draft: ToolDraft, values: Record<string, unknown>) => void | Promise<void>
export interface WorkbenchState {
  draft: ToolDraft
  builtin: boolean
  tools: ToolEntry[]
  values: Record<string, unknown>
  choices: Record<string, Choice[]>
  actions: Partial<Record<Action, ActionState>>
  check: CheckReply | null
  dryRun: DryRunReply | null
  revision: number
  dryRunRevision: number | null
  deleteName: string | null
  replaceName: string | null                             // save: a tool of this name exists, replace it?
  importReplace: { name: string; text: string } | null   // import: the same question for a file
  canEdit: boolean                                       // this host lets this page save, delete, import
  editNote: string                                       // why not, when it does not
}

const errorMessage = (error: unknown) => error instanceof Error ? error.message : String(error)
const bodyOf = (error: unknown): Record<string, unknown> | null => {
  const body = error && typeof error === 'object' && 'body' in error ? (error as { body: unknown }).body : null
  return body && typeof body === 'object' && !Array.isArray(body) ? body as Record<string, unknown> : null
}

/* What the server sent with a refusal, as it sent it: problems (422 invalid_pack), errors (422
   render_failed: the package's text; invalid_spec: {code, param, message}), warnings (400
   blocked). Read from the error's body (BridgeApiError of components/shared/http.ts). */
export function errorDetails(error: unknown): string[] {
  const body = bodyOf(error)
  if (!body) return []
  const lines: string[] = []
  for (const key of ['problems', 'errors', 'warnings']) {
    const list = body[key]
    if (!Array.isArray(list)) continue
    for (const item of list) {
      if (typeof item === 'string') lines.push(item)
      else if (item && typeof item === 'object' && typeof (item as { message?: unknown }).message === 'string') {
        const { code, param, message } = item as { code?: unknown; param?: unknown; message: string }
        lines.push(`${typeof code === 'string' ? code : ''}${typeof param === 'string' && param ? ` (${param})` : ''}${typeof code === 'string' ? ': ' : ''}${message}`)
      } else lines.push(JSON.stringify(item))
    }
  }
  return lines
}

/* The server's error code of a refusal (body.error), or null. */
export const errorCode = (error: unknown): string | null => {
  const code = bodyOf(error)?.error
  return typeof code === 'string' ? code : null
}
const clone = <T,>(value: T): T => structuredClone(value)
const signature = (draft: ToolDraft) => JSON.stringify([draft.code, draft.parameters])
const emptyDraft = (description = ''): ToolDraft => ({ name: '', description, code: '', parameters: [] })
const empty = (value: unknown) => value === undefined || value === null || value === ''

// Number fields keep the text the designer typed (state.values, a parameter's default) and are
// read only when the value is used, from the whole text: typing "2.5" passes through "2.", which
// must not become 2 and then 25. Text that is not a number is an error, never another value.
const DECIMAL = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/
const WHOLE = /^[+-]?\d+$/

export type Parsed = { value: unknown } | { error: string }

/* The value of a typed text (or of a value already typed, e.g. a loaded default) for a parameter
   type; { value: undefined } when it is empty. */
export function parseValue(type: ToolParamDraft['type'], raw: unknown): Parsed {
  if (raw === undefined || raw === null) return { value: undefined }
  if (type === 'string') return typeof raw === 'string' ? { value: raw === '' ? undefined : raw } : { error: 'must be text' }
  if (type === 'bool') {
    if (typeof raw === 'boolean') return { value: raw }
    if (raw === 'true' || raw === 'false') return { value: raw === 'true' }
    return raw === '' ? { value: undefined } : { error: 'must be true or false' }
  }
  if (typeof raw === 'number') {
    if (!Number.isFinite(raw)) return { error: 'must be a finite number' }
    return type === 'int' && !Number.isInteger(raw) ? { error: `${raw} is not a whole number` } : { value: raw }
  }
  if (typeof raw !== 'string') return { error: `must be ${type === 'int' ? 'a whole number' : 'a number'}` }
  const text = raw.trim()
  if (text === '') return { value: undefined }
  if (type === 'int') {
    const value = Number(text)
    return WHOLE.test(text) && Number.isSafeInteger(value) ? { value } : { error: `"${raw}" is not a whole number` }
  }
  const value = Number(text)
  return DECIMAL.test(text) && Number.isFinite(value) ? { value } : { error: `"${raw}" is not a number` }
}

export function parameterProblem(parameters: ToolParamDraft[]): string | null {
  const names = new Set<string>()
  for (const param of parameters) {
    if (!param.name.trim()) return 'Every parameter needs a name.'
    if (names.has(param.name)) return `Duplicate parameter: ${param.name}`
    names.add(param.name)
    if ((param.source === 'family_types' || param.source === 'elements') && !param.category?.startsWith('OST_')) return `${param.name}: select a category using its OST_* name.`
    const parsed = parseValue(param.type, param.default)
    if ('error' in parsed) return `${param.name}: the default ${parsed.error}.`
  }
  return null
}

/* The declarations as the interface takes them: each default read from its text (none when
   empty). Call after parameterProblem returned null. */
export function typedParameters(parameters: ToolParamDraft[]): ToolParamDraft[] {
  return parameters.map(param => {
    const { default: raw, ...rest } = clone(param)
    const parsed = parseValue(param.type, raw)
    return 'value' in parsed && parsed.value !== undefined ? { ...rest, default: parsed.value } : rest
  })
}

const typedDraft = (draft: ToolDraft): ToolDraft => ({ ...clone(draft), parameters: typedParameters(draft.parameters) })

/* The status line of a dry run that did not succeed. An empty error (a compile failure) is not
   an empty line; a refusal before anything was sent is not a rollback to worry about. */
export function dryRunFailure(result: DryRunReply): string {
  if (dryRunNotSent(result)) return `Nothing was sent to Revit: ${result.error}.`
  const reason = result.error || (result.diagnostics.length ? 'Dry run failed. See diagnostics.' : 'Dry run failed.')
  return result.rolled_back === true ? reason : `${reason.replace(/\.?$/, '.')} Rollback is not confirmed. Check the model before continuing.`
}

function validType(type: ToolParamDraft['type'], value: unknown): boolean {
  if (type === 'string') return typeof value === 'string'
  if (type === 'bool') return typeof value === 'boolean'
  return typeof value === 'number' && Number.isFinite(value) && (type !== 'int' || Number.isInteger(value))
}

export function saveDisabledReason(state: WorkbenchState): string | null {
  if (!state.canEdit) return state.editNote || 'This host does not let this page save tools.'
  if (state.actions.save?.status === 'running') return 'Wait for the current save to finish before saving again.'
  if (state.replaceName !== null) return `Answer whether to replace ${state.replaceName} first.`
  if (state.builtin) return 'Built-in tools are read-only. Use Copy as mine first.'
  const problem = parameterProblem(state.draft.parameters)
  if (problem) return problem
  if (state.dryRunRevision !== state.revision) return 'Dry-run the current code and parameter declarations successfully before saving.'
  if (!state.draft.name.trim()) return 'Enter a tool name before saving.'
  if (state.actions.list?.status !== 'success') return 'Refresh the tool list before saving.'
  if (state.tools.some(tool => tool.builtin && tool.name === state.draft.name.trim())) return 'Choose a name different from a built-in tool.'
  return null
}

export class WorkbenchModel {
  private state: WorkbenchState
  private listeners = new Set<() => void>()
  private tickets: Partial<Record<Action, number>> = {}
  private draftVersion = 0
  private choicesRevision = -1
  private saveInFlight = false
  private api: WorkbenchApi

  constructor(api: WorkbenchApi, initialDescription = '') {
    this.api = api
    this.state = { draft: emptyDraft(initialDescription), builtin: false, tools: [], values: {}, choices: {}, actions: {}, check: null, dryRun: null, revision: 0, dryRunRevision: null, deleteName: null,
      replaceName: null, importReplace: null, canEdit: true, editNote: '' }
  }

  getSnapshot = () => this.state
  setApi(api: WorkbenchApi) { this.api = api }
  /* Whether this host lets the page change tools (a cloud host wants the admin password). */
  setGate(canEdit: boolean, editNote = '') {
    if (canEdit !== this.state.canEdit || editNote !== this.state.editNote) {
      this.update({ canEdit, editNote, ...(canEdit ? {} : { replaceName: null, importReplace: null, deleteName: null }) })
    }
  }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener) } }
  private update(patch: Partial<WorkbenchState>) { this.state = { ...this.state, ...patch }; this.listeners.forEach(listener => listener()) }
  private status(action: Action, status: ActionState['status'], message: string, details: string[] = []) {
    this.update({ actions: { ...this.state.actions, [action]: details.length ? { status, message, details } : { status, message } } })
  }
  private begin(action: Action, message: string) {
    const ticket = (this.tickets[action] ?? 0) + 1
    this.tickets[action] = ticket
    this.status(action, 'running', message)
    return ticket
  }
  private current(action: Action, ticket: number) { return this.tickets[action] === ticket }
  private fail(action: Action, error: unknown) { this.status(action, 'error', errorMessage(error), errorDetails(error)) }
  // The last word of an action that is still the current one: never left showing "running".
  private settle(action: Action, ticket: number) {
    if (this.current(action, ticket) && this.state.actions[action]?.status === 'running') this.fail(action, 'Stopped before it finished. Try again.')
  }
  reportError(action: Action, error: unknown) { this.fail(action, error) }

  private replaceDraft(draft: ToolDraft, builtin: boolean) {
    this.draftVersion++
    this.choicesRevision = -1
    for (const action of ['check', 'dryRun', 'choices', 'run'] as const) this.tickets[action] = (this.tickets[action] ?? 0) + 1
    // values hold only what the designer set; a field without one shows (and uses) the default.
    this.update({ draft: { ...clone(draft), code: normalizeCode(draft.code) }, builtin, revision: this.state.revision + 1, dryRunRevision: null,
      values: {}, choices: {}, check: null, dryRun: null, deleteName: null, replaceName: null,
      actions: { ...this.state.actions, check: undefined, dryRun: undefined, choices: undefined, save: this.saveInFlight ? this.state.actions.save : undefined, run: undefined },
    })
  }

  edit(patch: Partial<ToolDraft>) {
    if (this.state.builtin) return
    const draft = clone({ ...this.state.draft, ...patch })
    draft.code = normalizeCode(draft.code)
    this.draftVersion++
    // A pending "replace it?" was asked about the draft as it was: any edit withdraws it.
    if (this.state.replaceName !== null) { this.update({ replaceName: null }); this.status('save', 'idle', '') }
    if (signature(draft) !== signature(this.state.draft)) {
      this.choicesRevision = -1
      for (const action of ['check', 'dryRun', 'choices'] as const) this.tickets[action] = (this.tickets[action] ?? 0) + 1
      this.update({ draft, revision: this.state.revision + 1, dryRunRevision: null, check: null, dryRun: null, choices: {},
        // Not seeded from the default: a default typed key by key ("0", "0.", "0.5") would
        // leave its first keystroke behind as the value.
        values: Object.fromEntries(draft.parameters.filter(param => Object.hasOwn(this.state.values, param.name)).map(param => [param.name, this.state.values[param.name]])),
        actions: { ...this.state.actions, check: undefined, dryRun: undefined, choices: undefined, save: this.saveInFlight ? this.state.actions.save : undefined },
      })
    } else this.update({ draft })
  }
  setValue(name: string, value: unknown) { this.update({ values: { ...this.state.values, [name]: value } }) }
  newDraft() { this.replaceDraft(emptyDraft(), false); this.tickets.load = (this.tickets.load ?? 0) + 1; this.status('load', 'idle', '') }
  copyAsMine() {
    const draft = clone(this.state.draft)
    const base = `${draft.name || 'tool'}_copy`
    draft.name = base
    let suffix = 2
    while (this.state.tools.some(tool => tool.name === draft.name)) draft.name = `${base}_${suffix++}`
    this.replaceDraft(draft, false)
    this.tickets.load = (this.tickets.load ?? 0) + 1
    this.status('load', 'success', 'Copied to an editable draft. Dry-run it before saving.')
  }

  async refresh() {
    const ticket = this.begin('list', 'Loading tools...')
    try {
      const tools = await this.api.list()
      if (this.current('list', ticket)) { this.update({ tools: clone(tools) }); this.status('list', 'success', `${tools.length} tools loaded.`) }
    } catch (error) { if (this.current('list', ticket)) this.fail('list', error) }
  }
  async open(name: string) {
    const entry = this.state.tools.find(tool => tool.name === name)
    if (!entry) { this.fail('load', 'Refresh the tool list before opening this tool.'); return }
    const version = this.draftVersion
    const ticket = this.begin('load', `Loading ${name}...`)
    try {
      const draft = await this.api.load(name)
      if (!this.current('load', ticket)) return
      if (version !== this.draftVersion) { this.fail('load', 'The draft changed while loading. Open the tool again when ready.'); return }
      this.replaceDraft(draft, entry.builtin)
      this.status('load', 'success', `Opened ${name}${entry.builtin ? ' (read-only)' : ''}.`)
      await this.loadChoices()
    } catch (error) { if (this.current('load', ticket)) this.fail('load', error) }
  }

  async loadChoices(): Promise<boolean> {
    const revision = this.state.revision
    const problem = parameterProblem(this.state.draft.parameters)
    if (problem) { this.fail('choices', problem); return false }
    const params = typedParameters(this.state.draft.parameters)
    const ticket = this.begin('choices', 'Loading model options...')
    const results = await Promise.all(params.filter(param => param.source !== 'designer').map(async param => {
      try { return { name: param.name, items: await this.api.choices(param), error: '' } }
      catch (error) { return { name: param.name, items: [] as Choice[], error: `${param.name}: ${errorMessage(error)}` } }
    }))
    if (!this.current('choices', ticket) || revision !== this.state.revision) return false
    this.update({ choices: Object.fromEntries(results.map(result => [result.name, clone(result.items)])) })
    const errors = results.map(result => result.error).filter(Boolean)
    if (errors.length) { this.fail('choices', errors.join('; ')); return false }
    this.choicesRevision = revision
    this.status('choices', 'success', 'Model options loaded. Choose values below.')
    return true
  }

  private async valuesFor(action: 'dryRun' | 'run'): Promise<Record<string, unknown> | null> {
    const revision = this.state.revision
    const problem = parameterProblem(this.state.draft.parameters)
    if (problem) { this.fail(action, problem); return null }
    if (!this.state.draft.code.trim()) { this.fail(action, 'Enter code first.'); return null }
    if (this.state.draft.parameters.some(param => param.source !== 'designer') && this.choicesRevision !== this.state.revision && !await this.loadChoices()) {
      if (revision === this.state.revision) this.fail(action, 'Load model options before continuing.')
      return null
    }
    const values: [string, unknown][] = []
    for (const param of this.state.draft.parameters) {
      // A value the designer cleared stays cleared (the field shows it empty): the default is
      // used only while no value was set. A model option is the option's own value, typed by the
      // server, never parsed from text.
      const set = Object.hasOwn(this.state.values, param.name)
      const raw = set ? this.state.values[param.name] : param.default
      const parsed = param.source !== 'designer' && set && !empty(raw) ? { value: raw } : parseValue(param.type, raw)
      if ('error' in parsed) { this.fail(action, `${param.name}: ${parsed.error}.`); return null }
      const value = parsed.value
      if (empty(value)) {
        if (param.required) { this.fail(action, `Fill a value for ${param.name}.`); return null }
        continue
      }
      if (!validType(param.type, value)) { this.fail(action, `${param.name} must be ${param.type}.`); return null }
      const choices = Object.hasOwn(this.state.choices, param.name) ? this.state.choices[param.name] : []
      if (param.source !== 'designer' && !choices.some(choice => JSON.stringify(choice.value) === JSON.stringify(value))) {
        this.fail(action, `Choose a model option for ${param.name}.`); return null
      }
      values.push([param.name, clone(value)])
    }
    return Object.fromEntries(values)
  }

  async check() {
    const problem = parameterProblem(this.state.draft.parameters)
    if (problem || !this.state.draft.code.trim()) { this.fail('check', problem ?? 'Enter code first.'); return }
    const draft = typedDraft(this.state.draft)
    const ticket = this.begin('check', 'Checking...')
    this.update({ check: null })
    try {
      const result = readCheck(await this.api.check(draft.code, draft.parameters))
      if (!this.current('check', ticket)) return
      this.update({ check: result })
      this.status('check', result.checked && result.compiled && !result.blocked ? 'success' : 'error',
        result.checked && result.compiled && !result.blocked ? 'Compilation succeeded.' : result.reason || (result.blocked ? 'Code was blocked.' : result.checked ? 'Compilation failed. See diagnostics.' : 'Code was not checked.'))
    } catch (error) { if (this.current('check', ticket)) this.fail('check', error) }
  }

  async dryRun() {
    if (this.state.actions.dryRun?.status === 'running') return
    const ticket = this.begin('dryRun', 'Preparing dry run...')
    this.update({ dryRunRevision: null, dryRun: null })
    const revision = this.state.revision
    try {
      const values = await this.valuesFor('dryRun')
      if (!values || !this.current('dryRun', ticket) || revision !== this.state.revision) return
      const draft = typedDraft(this.state.draft)
      this.status('dryRun', 'running', 'Dry-running...')
      const result = readDryRun(await this.api.dryRun(draft.code, draft.parameters, values))
      if (!this.current('dryRun', ticket) || revision !== this.state.revision) return
      const success = result.success && result.rolled_back === true
      this.update({ dryRun: result, dryRunRevision: success ? revision : null })
      this.status('dryRun', success ? 'success' : 'error', success ? 'Dry run succeeded. Model was not changed.' : dryRunFailure(result))
    } catch (error) { if (this.current('dryRun', ticket)) this.fail('dryRun', error) }
    finally { this.settle('dryRun', ticket) }
  }

  async run(onRun: OnRun) {
    if (this.state.actions.run?.status === 'running') return
    const ticket = this.begin('run', 'Preparing run...')
    const revision = this.state.revision
    try {
      const values = await this.valuesFor('run')
      if (!this.current('run', ticket)) return
      // An edit while the options were loading makes valuesFor stop without a message of its own.
      if (revision !== this.state.revision) { this.fail('run', 'The draft changed while the run was being prepared. Review the values and run again.'); return }
      if (!values) return
      await onRun(typedDraft(this.state.draft), values)
      if (this.current('run', ticket)) this.status('run', 'success', 'Run requested. Confirmation and execution are handled by the host.')
    } catch (error) { if (this.current('run', ticket)) this.fail('run', error) }
    finally { this.settle('run', ticket) }
  }

  /* Save never replaces a tool silently: the name is looked up first (api.exists) and, when a
     tool has it, the designer is asked (replaceName; confirmReplace / cancelReplace). The
     server keeps the last word: a built-in's name is refused there (409 builtin_read_only). */
  async save() {
    if (this.saveInFlight) return
    const reason = saveDisabledReason(this.state)
    if (reason) { this.fail('save', reason); return }
    const draft = typedDraft(this.state.draft)
    draft.name = draft.name.trim()
    const version = this.draftVersion
    this.saveInFlight = true
    const ticket = this.begin('save', `Looking for a tool named ${draft.name}...`)
    let exists: boolean
    try { exists = await this.api.exists(draft.name) }
    catch (error) {
      this.saveInFlight = false
      if (this.current('save', ticket)) this.fail('save', error)
      return
    }
    this.saveInFlight = false
    if (!this.current('save', ticket)) return
    if (version !== this.draftVersion) { this.fail('save', 'The draft changed while its name was looked up. Save again.'); return }
    if (exists) {
      this.update({ replaceName: draft.name })
      this.status('save', 'idle', '')
      return
    }
    await this.write(draft, version, ticket)
  }
  async confirmReplace() {
    const name = this.state.replaceName
    if (name === null || this.saveInFlight) return
    this.update({ replaceName: null })
    const reason = saveDisabledReason(this.state)
    if (reason || name !== this.state.draft.name.trim()) { this.fail('save', reason ?? 'The name changed. Save again.'); return }
    const draft = typedDraft(this.state.draft)
    draft.name = name
    await this.write(draft, this.draftVersion, this.begin('save', `Replacing ${name}...`))
  }
  cancelReplace() {
    if (this.state.replaceName === null) return
    this.update({ replaceName: null })
    this.status('save', 'idle', '')
  }
  private async write(draft: ToolDraft, version: number, ticket: number) {
    this.saveInFlight = true
    try {
      const result = await this.api.save(draft)
      await this.refresh()
      if (this.current('save', ticket)) this.status('save', 'success', `Saved ${result.name}.${version === this.draftVersion ? '' : ' The current draft has not been saved.'}`)
    } catch (error) { if (this.current('save', ticket)) this.fail('save', error) }
    finally { this.saveInFlight = false; this.settle('save', ticket) }
  }

  requestDelete(name: string) {
    const tool = this.state.tools.find(item => item.name === name)
    if (!this.state.canEdit) { this.fail('remove', this.state.editNote || 'This host does not let this page delete tools.'); return }
    if (!tool || tool.builtin) { this.fail('remove', 'Built-in or unknown tools cannot be deleted.'); return }
    this.update({ deleteName: name })
  }
  cancelDelete() { this.update({ deleteName: null }) }
  async confirmDelete() {
    const name = this.state.deleteName
    if (!name || this.state.actions.remove?.status === 'running') return
    const tool = this.state.tools.find(item => item.name === name)
    if (!this.state.canEdit || !tool || tool.builtin) { this.fail('remove', 'Built-in or unknown tools cannot be deleted.'); return }
    const ticket = this.begin('remove', `Deleting ${name}...`)
    try {
      await this.api.remove(name)
      if (!this.current('remove', ticket)) return
      this.update({ deleteName: null })
      this.status('remove', 'success', `Deleted ${name}.`)
      await this.refresh()
    } catch (error) { if (this.current('remove', ticket)) this.fail('remove', error) }
    finally { this.settle('remove', ticket) }
  }

  async exportTool(name: string, download: (name: string, text: string) => void) {
    const ticket = this.begin('export', `Exporting ${name}...`)
    try {
      const text = await this.api.exportYaml(name)
      if (!this.current('export', ticket)) return
      download(name, text)
      this.status('export', 'success', `Exported ${name}.`)
    } catch (error) { if (this.current('export', ticket)) this.fail('export', error) }
    finally { this.settle('export', ticket) }
  }

  /* An import never replaces a tool silently either: the server refuses a name of yours with
     409 pack_exists, and the designer is asked (importReplace) before it is sent with overwrite. */
  async importTool(text: string | Promise<string>) {
    if (this.state.actions.import?.status === 'running') return
    if (!this.state.canEdit) { this.fail('import', this.state.editNote || 'This host does not let this page import tools.'); return }
    this.update({ importReplace: null })
    const ticket = this.begin('import', 'Importing tool...')
    let yaml = ''
    try {
      yaml = await text
      const result = await this.api.importYaml(yaml)
      if (!this.current('import', ticket)) return
      this.status('import', 'success', `Imported ${result.name}${result.replaced ? ' (replaced)' : ''}.`)
      await this.refresh()
    } catch (error) {
      if (!this.current('import', ticket)) return
      if (errorCode(error) === 'pack_exists') {
        const tool = bodyOf(error)?.tool
        this.update({ importReplace: { name: typeof tool === 'string' ? tool : 'this tool', text: yaml } })
        this.status('import', 'idle', '')
      } else this.fail('import', error)
    } finally { this.settle('import', ticket) }
  }
  async confirmImport() {
    const pending = this.state.importReplace
    if (!pending || this.state.actions.import?.status === 'running') return
    this.update({ importReplace: null })
    const ticket = this.begin('import', `Replacing ${pending.name}...`)
    try {
      const result = await this.api.importYaml(pending.text, { overwrite: true })
      if (!this.current('import', ticket)) return
      this.status('import', 'success', `Imported ${result.name}${result.replaced ? ' (replaced)' : ''}.`)
      await this.refresh()
    } catch (error) { if (this.current('import', ticket)) this.fail('import', error) }
    finally { this.settle('import', ticket) }
  }
  cancelImport() {
    if (!this.state.importReplace) return
    this.update({ importReplace: null })
    this.status('import', 'idle', '')
  }
}
