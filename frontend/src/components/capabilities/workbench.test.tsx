/* The workbench page, its address, the link the chat model writes to it, and Run (the saved
   tool through the spec card: dropped with any draft change, checked again at Confirm, one run
   at a time). */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../../App'
import type { ConfirmResponse, ExecutionResult, ToolDetail } from '../../types/api'
import type { ToolDraft, WorkbenchApi } from '../../types/hostv2'
import { pathOf, viewOf, WORKBENCH_PATH } from '../../route'
import { WorkbenchModel } from '../hostv2/workbench/model'
import Markdown from '../shared/Markdown'
import { BridgeApiError } from '../shared/http'
import CapabilitiesPage from '../pages/CapabilitiesPage'
import WorkbenchPage, { RunPanel } from './WorkbenchPage'
import { confirmRun, differsFromSaved, RUN_IN_PROGRESS, WorkbenchRun, workbenchSpec, type WorkbenchRunApi } from './workbenchRun'

const saved: ToolDetail = {
  name: 'raise_walls', display_name: 'Raise Walls', description: 'Raise the selected walls', version: '1.0.0',
  code_template: 'var h = {height};\nreturn h;', source_query: '', tags: [], execution_count: 0, preconditions: [],
  applies_when: [], not_for: [], validator: { kind: 'param_equals', category: 'OST_Walls' },
  parameters: [
    { name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', required: true },
    { name: 'level_name', type: 'string', description: 'Level', source: 'tool:levels', choices_from: 'levels', required: false },
    { name: 'note', type: 'string', description: 'Note', source: 'default', default: 'none', required: false },
  ],
}

// The draft the workbench holds after opening the saved tool (api/workbench.ts load()).
const draft: ToolDraft = {
  name: 'raise_walls', description: 'Raise the selected walls', code: 'var h = {height};\r\nreturn h;',
  validator: '{"category":"OST_Walls","kind":"param_equals"}',
  parameters: [
    { name: 'height', type: 'double', description: 'Height', source: 'designer', required: true },
    { name: 'level_name', type: 'string', description: 'Level', source: 'levels', required: false },
    { name: 'note', type: 'string', description: 'Note', source: 'designer', required: false, default: 'none' },
  ],
}

const executed = { success: true, validation: { passed: true }, evidence_id: 'ev-1' } as unknown as ExecutionResult

const runApi = (overrides: Partial<WorkbenchRunApi> = {}): WorkbenchRunApi => ({
  getTool: vi.fn(async () => structuredClone(saved)),
  confirm: vi.fn(async (): Promise<ConfirmResponse> => ({ token: 'tok-1234567890', spec_hash: 'h', expires_at: '2026-11-23T12:00:00Z', card: {} } as ConfirmResponse)),
  runTool: vi.fn(async (): Promise<ExecutionResult> => executed),
  ...overrides,
})

// The workbench's side, for wiring a real WorkbenchModel to a WorkbenchRun as the page does.
const workbenchApi = (): WorkbenchApi => ({
  check: async () => ({}), dryRun: async () => ({}), choices: async () => [{ label: 'L1', value: 'L1' }], lookup: async () => null,
  save: async () => ({ name: 'x' }), remove: async () => {}, exportYaml: async () => '', importYaml: async () => ({ name: 'x', replaced: false }),
  list: async () => [{ name: 'raise_walls', description: '', builtin: false, origin: 'user' }, { name: 'other', description: '', builtin: false, origin: 'user' }],
  load: async name => name === 'raise_walls' ? structuredClone(draft) : { ...structuredClone(draft), name },
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(yes => { resolve = yes })
  return { promise, resolve }
}

/* A WorkbenchModel holding the opened raise_walls, wired to a WorkbenchRun as the page wires them. */
async function page(api: WorkbenchRunApi = runApi()) {
  const model = new WorkbenchModel(workbenchApi())
  const run = new WorkbenchRun(api)
  model.setDraftListener(run.draftChanged)
  await model.refresh()
  await model.open('raise_walls')
  model.setValue('height', '500')
  model.setValue('level_name', 'L1')
  return { model, run, api }
}

describe('the workbench address', () => {
  it('opens the Capabilities page at its workbench; every other path opens the first tab', () => {
    expect(WORKBENCH_PATH).toBe('/capabilities/workbench')
    for (const path of ['/capabilities/workbench', '/capabilities/workbench/', '/Capabilities/Workbench']) {
      expect(viewOf(path)).toEqual({ tab: 2, capabilities: 'workbench' })
    }
    for (const path of ['/', '/task', '/capabilities', '/capabilities/packs', '/capabilities/workbench/x']) {
      expect(viewOf(path)).toEqual({ tab: 0, capabilities: 'packs' })
    }
    expect(pathOf({ tab: 2, capabilities: 'workbench' })).toBe(WORKBENCH_PATH)
    expect(pathOf({ tab: 2, capabilities: 'packs' })).toBe('/')
    expect(pathOf({ tab: 1, capabilities: 'workbench' })).toBe('/')
  })

  it('renders the workbench there', () => {
    const html = renderToStaticMarkup(<App path="/capabilities/workbench" />)
    expect(html).toContain('<button class="tab-button is-active">Capabilities</button>')
    expect(html).toContain('<button class="subtab is-active">Workbench</button>')
    expect(html).toContain('<h2>Tool workbench</h2>')
    expect(html).not.toContain('being wired in')
    expect(renderToStaticMarkup(<App path="/" />)).not.toContain('Tool workbench')
    expect(renderToStaticMarkup(<CapabilitiesPage />)).toContain('<button class="subtab">Workbench</button>')
  })

  it('the chat renders the link the host paragraph asks for, opening in a new tab', () => {
    const html = renderToStaticMarkup(<Markdown>{'No tool fits. Make one in the [workbench](/capabilities/workbench).'}</Markdown>)
    expect(html).toContain('<a href="/capabilities/workbench" target="_blank" rel="noopener noreferrer">workbench</a>')
  })
})

beforeEach(() => vi.stubGlobal('navigator', { languages: ['en-US'] }))
afterEach(() => vi.unstubAllGlobals())

describe('the page', () => {
  it('shows the note and the workbench; on a cloud host without the admin password, no import and the reason', () => {
    const html = renderToStaticMarkup(<WorkbenchPage api={workbenchApi()} runApi={runApi()} />)
    expect(html).toContain('Your tools are shared with every host that uses the same tool directory')
    expect(html).toContain('Save as tool')
    // The tests run with the default config: a cloud host without ADMIN_PASSWORD.
    expect(html).not.toContain('Import YAML')
    expect(html).toContain('This host has no admin password configured')
  })

  it('the run panel: the card with the defaults the tool fills, Confirm, then the outcome; or the notice', async () => {
    const { model, run } = await page()
    model.setValue('note', '')
    await model.run(run.onRun)
    const card = renderToStaticMarkup(<RunPanel state={run.getSnapshot()} onConfirm={() => {}} onClose={() => {}} />)
    expect(card).toContain('Run raise_walls')
    expect(card).toContain('<td class="mono">note</td><td class="mono">default &quot;none&quot; will be used</td>')
    expect(card).toContain('Confirm and run in Revit')
    const running = renderToStaticMarkup(<RunPanel state={{ card: { ...run.getSnapshot().card!, running: true }, notice: null }} onConfirm={() => {}} onClose={() => {}} />)
    expect(running).toMatch(/disabled=""[^>]*>Running\.\.\./)
    const failed = renderToStaticMarkup(<RunPanel state={{ card: { ...run.getSnapshot().card!, outcome: { kind: 'failed', message: '429: rate_limited: Too many confirmations', details: [] } }, notice: null }} onConfirm={() => {}} onClose={() => {}} />)
    expect(failed).toContain('429: rate_limited: Too many confirmations')
    expect(failed).not.toContain('Confirm and run in Revit')
    expect(renderToStaticMarkup(<RunPanel state={{ card: null, notice: 'The saved raise_walls changed' }} onConfirm={() => {}} onClose={() => {}} />))
      .toContain('The saved raise_walls changed')
  })
})

describe('Run runs the saved tool', () => {
  it('builds the spec card from the saved tool: a model option is a tool value, a typed value the designer, a default as the Packs page binds it', async () => {
    const api = runApi()
    const { spec, defaults } = await workbenchSpec(api, draft, { height: 2.5, level_name: 'L1', note: 'none' })
    expect(api.getTool).toHaveBeenCalledWith('raise_walls')
    expect(spec.action).toEqual({ kind: 'run_tool', tool: 'raise_walls' })
    expect(spec.parameters).toEqual([
      { name: 'height', value: 2.5, unit: 'mm', source: 'designer', evidence: 'capabilities page' },
      { name: 'level_name', value: 'L1', unit: null, source: 'tool', evidence: 'tool:levels' },
      { name: 'note', value: 'none', unit: null, source: 'default', evidence: 'default:raise_walls' },
    ])
    expect(defaults).toEqual([])
    expect((await workbenchSpec(api, draft, { height: 2.5 })).defaults).toEqual([{ name: 'note', value: 'none', unit: null }])
  })

  it('refuses a draft that is not the saved tool, naming the part', async () => {
    await expect(workbenchSpec(runApi(), { ...draft, name: ' ' }, {})).rejects.toThrow('The draft has no name. Save it first')
    const missing = runApi({ getTool: async () => { throw new BridgeApiError(404, JSON.stringify({ error: 'unknown_tool', message: 'not found' })) } })
    await expect(workbenchSpec(missing, draft, {})).rejects.toThrow('There is no saved tool named raise_walls. Save it first')
    await expect(workbenchSpec(runApi(), { ...draft, code: 'return 0;' }, {})).rejects.toThrow('This draft is not the saved raise_walls: its code differs.')
    const other = { ...draft, parameters: draft.parameters.map(p => p.name === 'height' ? { ...p, type: 'int' as const } : p) }
    await expect(workbenchSpec(runApi(), other, {})).rejects.toThrow('its parameter height differs')
    await expect(workbenchSpec(runApi(), { ...draft, parameters: draft.parameters.slice(1) }, {})).rejects.toThrow('it declares 2 parameter(s), the saved tool 3')
    await expect(workbenchSpec(runApi(), { ...draft, validator: '{"kind":"created_ids"}' }, {})).rejects.toThrow('its validator differs')
    await expect(workbenchSpec(runApi(), { ...draft, validator: '' }, {})).rejects.toThrow('its validator differs')
    const unreachable = runApi({ getTool: async () => { throw new BridgeApiError(503, JSON.stringify({ error: 'revit_unreachable' })) } })
    await expect(workbenchSpec(unreachable, draft, {})).rejects.toThrow('503: revit_unreachable')
  })

  it('a description, a unit the package added, key order in the validator or a default typed as text is no difference', () => {
    expect(differsFromSaved({ ...draft, validator: '{"kind": "param_equals", "category": "OST_Walls"}', parameters: draft.parameters.map(p => ({ ...p, description: 'other', unit: undefined })) }, saved)).toBeNull()
    const withDefault = { ...saved, parameters: saved.parameters.map(p => p.name === 'height' ? { ...p, default: 3000, required: false } : p) }
    expect(differsFromSaved({ ...draft, parameters: draft.parameters.map(p => p.name === 'height' ? { ...p, default: '3000', required: false } : p) }, withDefault)).toBeNull()
    expect(differsFromSaved({ ...draft, parameters: draft.parameters.map(p => p.name === 'height' ? { ...p, default: null, required: false } : p) }, withDefault)).toBe('its parameter height differs')
  })

  it('confirm and run: the result, the spec errors, or the failure - never a thrown error', async () => {
    const { spec } = await workbenchSpec(runApi(), draft, { height: 500 })
    const done = await confirmRun(runApi(), spec, draft)
    expect(done).toMatchObject({ kind: 'executed', token: 'tok-1234567890', result: executed })
    const invalid = new BridgeApiError(422, JSON.stringify({ error: 'invalid_spec', errors: [{ code: 'missing_param', param: 'height', message: 'needed' }] }))
    expect(await confirmRun(runApi({ confirm: async () => { throw invalid } }), spec, draft))
      .toEqual({ kind: 'rejected', errors: [{ code: 'missing_param', param: 'height', message: 'needed' }] })
    const limited = new BridgeApiError(429, JSON.stringify({ error: 'rate_limited', message: 'Too many confirmations' }))
    expect(await confirmRun(runApi({ confirm: async () => { throw limited } }), spec, draft))
      .toEqual({ kind: 'failed', message: '429: rate_limited: Too many confirmations', details: [] })
    const blocked = new BridgeApiError(400, JSON.stringify({ error: 'blocked', message: 'Code review failed', warnings: ['System.IO'] }))
    expect(await confirmRun(runApi({ runTool: async () => { throw blocked } }), spec, draft))
      .toEqual({ kind: 'failed', message: '400: blocked: Code review failed', details: ['System.IO'] })
  })
})

describe('the card belongs to the draft it was built from', () => {
  it.each([
    ['an edit of the code', (m: WorkbenchModel) => m.edit({ code: 'return 0;' })],
    ['an edit of the description', (m: WorkbenchModel) => m.edit({ description: 'other' })],
    ['a value', (m: WorkbenchModel) => m.setValue('height', '600')],
    ['opening another tool', async (m: WorkbenchModel) => { await m.open('other') }],
    ['a new draft', (m: WorkbenchModel) => m.newDraft()],
  ])('%s drops a card waiting for Confirm', async (_, change) => {
    const { model, run, api } = await page()
    await model.run(run.onRun)
    expect(run.getSnapshot().card?.spec.action.tool).toBe('raise_walls')
    await change(model)
    expect(run.getSnapshot().card).toBeNull()
    await run.confirm()
    expect(api.confirm).not.toHaveBeenCalled()
  })

  it('a card prepared while the draft changes never appears; an edit says why on Run', async () => {
    const pending = deferred<ToolDetail>()
    const getTool = vi.fn(() => pending.promise)
    const { model, run } = await page(runApi({ getTool }))
    const running = model.run(run.onRun)
    await vi.waitFor(() => expect(getTool).toHaveBeenCalled())
    model.edit({ description: 'changed while preparing' })
    pending.resolve(structuredClone(saved))
    await running
    expect(run.getSnapshot().card).toBeNull()
    expect(model.getSnapshot().actions.run?.message).toBe('The draft changed while the run was being prepared. Run again.')
  })

  it('a card prepared while another tool is opened never appears', async () => {
    const pending = deferred<ToolDetail>()
    const getTool = vi.fn(() => pending.promise)
    const { model, run } = await page(runApi({ getTool }))
    const running = model.run(run.onRun)
    await vi.waitFor(() => expect(getTool).toHaveBeenCalled())
    await model.open('other')
    pending.resolve(structuredClone(saved))
    await running
    expect(run.getSnapshot().card).toBeNull()
    expect(model.getSnapshot().draft.name).toBe('other')
  })

  it('Confirm reads the saved tool again: changed since the card was shown, nothing is confirmed and the card goes with a notice', async () => {
    const api = runApi()
    const { model, run } = await page(api)
    await model.run(run.onRun)
    vi.mocked(api.getTool).mockResolvedValue({ ...structuredClone(saved), code_template: 'return 99;' })
    await run.confirm()
    expect(api.confirm).not.toHaveBeenCalled()
    expect(run.getSnapshot()).toEqual({ card: null, notice: 'The saved raise_walls changed after its card was shown (its code differs). Nothing was run; run again.' })
    model.setValue('height', '700')
    expect(run.getSnapshot().notice).toBeNull()
  })

  it('Confirm after the saved tool was deleted runs nothing', async () => {
    const api = runApi()
    const { model, run } = await page(api)
    await model.run(run.onRun)
    vi.mocked(api.getTool).mockRejectedValue(new BridgeApiError(404, JSON.stringify({ error: 'unknown_tool' })))
    await run.confirm()
    expect(api.confirm).not.toHaveBeenCalled()
    expect(run.getSnapshot().notice).toBe('raise_walls is no longer saved. Nothing was run.')
  })
})

describe('one run at a time', () => {
  it('while a confirmed run is in progress Run is refused, edits keep the card, and the result is shown when it comes', async () => {
    const result = deferred<ExecutionResult>()
    const api = runApi({ runTool: vi.fn(() => result.promise) })
    const { model, run } = await page(api)
    await model.run(run.onRun)
    const confirming = run.confirm()
    expect(run.getSnapshot().card?.running).toBe(true)
    expect(run.running).toBe(true)
    await model.run(run.onRun)
    expect(model.getSnapshot().actions.run).toEqual({ status: 'error', message: RUN_IN_PROGRESS })
    model.edit({ description: 'edited while running' })
    run.close()
    expect(run.getSnapshot().card?.running).toBe(true)
    result.resolve(executed)
    await confirming
    expect(run.getSnapshot().card?.outcome).toMatchObject({ kind: 'executed', result: executed })
    model.setValue('height', '800')
    expect(run.getSnapshot().card?.outcome?.kind).toBe('executed')
    expect(api.runTool).toHaveBeenCalledOnce()
  })

  it('a double click on Confirm confirms once', async () => {
    const api = runApi()
    const { model, run } = await page(api)
    await model.run(run.onRun)
    await Promise.all([run.confirm(), run.confirm()])
    expect(api.confirm).toHaveBeenCalledOnce()
    expect(api.runTool).toHaveBeenCalledOnce()
    await run.confirm()
    expect(api.confirm).toHaveBeenCalledOnce()
  })

  it('the page turns the workbench Run off while a run is in progress', async () => {
    const { model, run } = await page(runApi({ runTool: vi.fn(() => new Promise<ExecutionResult>(() => {})) }))
    expect(renderToStaticMarkup(<WorkbenchPage api={workbenchApi()} runner={run} />)).toMatch(/<button type="button" class="btn-primary">Run</)
    await model.run(run.onRun)
    void run.confirm()
    expect(run.getSnapshot().card?.running).toBe(true)
    const html = renderToStaticMarkup(<WorkbenchPage api={workbenchApi()} runner={run} />)
    expect(html).toMatch(/<button type="button" class="btn-primary" disabled="" title="A run is in progress; wait for its result.">Run</)
    expect(html).toMatch(/disabled=""[^>]*>Running\.\.\./)
  })
})
