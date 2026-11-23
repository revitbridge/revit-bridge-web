/* The workbench page, its address, the link the chat model writes to it, and Run (the saved
   tool through the spec card). */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import App from '../../App'
import type { ConfirmResponse, ExecutionResult, TaskSpec, ToolDetail } from '../../types/api'
import type { ToolDraft, WorkbenchApi } from '../../types/hostv2'
import { pathOf, viewOf, WORKBENCH_PATH } from '../../route'
import Markdown from '../shared/Markdown'
import { BridgeApiError } from '../shared/http'
import CapabilitiesPage from '../pages/CapabilitiesPage'
import WorkbenchPage, { RunPanel } from './WorkbenchPage'
import { confirmRun, differsFromSaved, workbenchSpec, type WorkbenchRunApi } from './workbenchRun'

const noApi: WorkbenchApi = {
  check: async () => ({}), dryRun: async () => ({}), choices: async () => [], exists: async () => false,
  save: async () => ({ name: 'x' }), list: async () => [], load: async () => ({ name: 'x', description: '', code: '', parameters: [] }),
  remove: async () => {}, exportYaml: async () => '', importYaml: async () => ({ name: 'x', replaced: false }),
}

const saved: ToolDetail = {
  name: 'raise_walls', display_name: 'Raise Walls', description: 'Raise the selected walls', version: '1.0.0',
  code_template: 'var h = {height};\nreturn h;', source_query: '', tags: [], execution_count: 0, preconditions: [],
  applies_when: [], not_for: [], validator: { kind: 'param_equals' },
  parameters: [
    { name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', required: true },
    { name: 'level_name', type: 'string', description: 'Level', source: 'tool:levels', choices_from: 'levels', required: false },
    { name: 'note', type: 'string', description: 'Note', source: 'default', default: 'none', required: false },
  ],
}

// The draft the workbench holds after opening the saved tool (api/workbench.ts load()).
const draft: ToolDraft = {
  name: 'raise_walls', description: 'Raise the selected walls', code: 'var h = {height};\r\nreturn h;',
  validator: '{"kind":"param_equals"}',
  parameters: [
    { name: 'height', type: 'double', description: 'Height', source: 'designer', required: true },
    { name: 'level_name', type: 'string', description: 'Level', source: 'levels', required: false },
    { name: 'note', type: 'string', description: 'Note', source: 'designer', required: false, default: 'none' },
  ],
}

const runApi = (overrides: Partial<WorkbenchRunApi> = {}): WorkbenchRunApi => ({
  getTool: vi.fn(async () => saved),
  confirm: vi.fn(async (): Promise<ConfirmResponse> => ({ token: 'tok-1234567890', spec_hash: 'h', expires_at: '2026-11-23T12:00:00Z', card: {} } as ConfirmResponse)),
  runTool: vi.fn(async (): Promise<ExecutionResult> => ({ success: true } as ExecutionResult)),
  ...overrides,
})

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

describe('the page', () => {
  it('shows the note and the workbench; on a cloud host without the admin password, no import and the reason', () => {
    const html = renderToStaticMarkup(<WorkbenchPage api={noApi} runApi={runApi()} />)
    expect(html).toContain('Your tools are shared with every host that uses the same tool directory')
    expect(html).toContain('Check')
    expect(html).toContain('Dry run')
    expect(html).toContain('Save as tool')
    // The tests run with the default config: a cloud host without ADMIN_PASSWORD.
    expect(html).not.toContain('Import YAML')
    expect(html).toContain('This host has no admin password configured')
  })

  it('the run panel: the spec card with Confirm, then the refusal, failure or result', () => {
    const spec: TaskSpec = { task: 't', action: { kind: 'run_tool', tool: 'raise_walls' }, parameters: [{ name: 'height', value: 500, unit: 'mm', source: 'designer', evidence: 'capabilities page' }] }
    const card = renderToStaticMarkup(<RunPanel run={{ spec, running: false, outcome: null }} onConfirm={() => {}} onClose={() => {}} />)
    expect(card).toContain('Run raise_walls')
    expect(card).toContain('Confirm and run in Revit')
    expect(renderToStaticMarkup(<RunPanel run={{ spec, running: true, outcome: null }} onConfirm={() => {}} onClose={() => {}} />)).toMatch(/disabled=""[^>]*>Running\.\.\./)
    const failed = renderToStaticMarkup(<RunPanel run={{ spec, running: false, outcome: { kind: 'failed', message: '429: rate_limited: Too many confirmations', details: [] } }} onConfirm={() => {}} onClose={() => {}} />)
    expect(failed).toContain('429: rate_limited: Too many confirmations')
    expect(failed).not.toContain('Confirm and run in Revit')
    const refused = renderToStaticMarkup(<RunPanel run={{ spec, running: false, outcome: { kind: 'rejected', errors: [{ code: 'missing_param', param: 'height', message: 'needed' }] } }} onConfirm={() => {}} onClose={() => {}} />)
    expect(refused).toContain('<code>missing_param</code>')
  })
})

describe('Run runs the saved tool', () => {
  it('builds the spec card from the saved tool: a model option is a tool value, a typed value the designer, a default as the Packs page binds it', async () => {
    const api = runApi()
    const spec = await workbenchSpec(api, draft, { height: 2.5, level_name: 'L1', note: 'none' })
    expect(api.getTool).toHaveBeenCalledWith('raise_walls')
    expect(spec.action).toEqual({ kind: 'run_tool', tool: 'raise_walls' })
    expect(spec.parameters).toEqual([
      { name: 'height', value: 2.5, unit: 'mm', source: 'designer', evidence: 'capabilities page' },
      { name: 'level_name', value: 'L1', unit: null, source: 'tool', evidence: 'tool:levels' },
      { name: 'note', value: 'none', unit: null, source: 'default', evidence: 'default:raise_walls' },
    ])
  })

  it('refuses a draft that is not the saved tool, with the reason', async () => {
    await expect(workbenchSpec(runApi(), { ...draft, name: ' ' }, {})).rejects.toThrow('The draft has no name. Save it first')
    const missing = runApi({ getTool: async () => { throw new BridgeApiError(404, JSON.stringify({ error: 'unknown_tool', message: 'not found' })) } })
    await expect(workbenchSpec(missing, draft, {})).rejects.toThrow('There is no saved tool named raise_walls. Save it first')
    await expect(workbenchSpec(runApi(), { ...draft, code: 'return 0;' }, {})).rejects.toThrow('This draft is not the saved raise_walls: its code differs.')
    const other = { ...draft, parameters: draft.parameters.map(p => p.name === 'height' ? { ...p, default: '3000' } : p) }
    await expect(workbenchSpec(runApi(), other, {})).rejects.toThrow('its parameter declarations differ')
    const unreachable = runApi({ getTool: async () => { throw new BridgeApiError(503, JSON.stringify({ error: 'revit_unreachable' })) } })
    await expect(workbenchSpec(unreachable, draft, {})).rejects.toThrow('503: revit_unreachable')
  })

  it('a description or a unit the package added is not a difference; a default typed as text is read as the saved number', () => {
    expect(differsFromSaved({ ...draft, parameters: draft.parameters.map(p => ({ ...p, description: 'other', unit: undefined })) }, saved)).toBeNull()
    const withDefault = { ...saved, parameters: saved.parameters.map(p => p.name === 'height' ? { ...p, default: 3000 } : p) }
    expect(differsFromSaved({ ...draft, parameters: draft.parameters.map(p => p.name === 'height' ? { ...p, default: '3000' } : p) }, withDefault)).toBeNull()
  })

  it('confirm and run: the result, the spec errors, or the failure - never a thrown error', async () => {
    const spec = await workbenchSpec(runApi(), draft, { height: 500 })
    const done = await confirmRun(runApi(), spec)
    expect(done).toMatchObject({ kind: 'executed', token: 'tok-1234567890', result: { success: true } })
    const invalid = new BridgeApiError(422, JSON.stringify({ error: 'invalid_spec', errors: [{ code: 'missing_param', param: 'height', message: 'needed' }] }))
    expect(await confirmRun(runApi({ confirm: async () => { throw invalid } }), spec))
      .toEqual({ kind: 'rejected', errors: [{ code: 'missing_param', param: 'height', message: 'needed' }] })
    const limited = new BridgeApiError(429, JSON.stringify({ error: 'rate_limited', message: 'Too many confirmations' }))
    expect(await confirmRun(runApi({ confirm: async () => { throw limited } }), spec))
      .toEqual({ kind: 'failed', message: '429: rate_limited: Too many confirmations', details: [] })
    const blocked = new BridgeApiError(400, JSON.stringify({ error: 'blocked', message: 'Code review failed', warnings: ['System.IO'] }))
    expect(await confirmRun(runApi({ runTool: async () => { throw blocked } }), spec))
      .toEqual({ kind: 'failed', message: '400: blocked: Code review failed', details: ['System.IO'] })
  })
})
