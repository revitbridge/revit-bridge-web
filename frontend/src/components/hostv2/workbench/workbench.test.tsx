import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ToolDraft, ToolParamDraft, WorkbenchApi } from '../../../types/hostv2'
import { inputValue, saveDisabledReason, WorkbenchModel } from './model'
import { diagnosticRange, indentSelection, readCheck, readDryRun } from './results'
import Workbench, { WorkbenchView } from './Workbench'

const draft: ToolDraft = { name: 'test_tool', description: 'Test tool', code: 'var x = 1;\nreturn x;', parameters: [] }
const param = (overrides: Partial<ToolParamDraft> = {}): ToolParamDraft => ({ name: 'height', description: 'Height', type: 'double', source: 'designer', required: true, ...overrides })
const checkReply = { schema_version: 1, checked: true, compiled: true, blocked: false, capability: 'supported', reason: null,
  diagnostics: [], truncated: 0, fingerprint: null, hints: [], guidance: [], environment: null, warnings: [] }
const dryReply = { success: true, rolled_back: true, result: { ElementId: 42 }, error: null, diagnostics: [],
  changes: { added: { count: 1, ids: [42], by_category: { OST_Walls: 1 } }, modified: { count: 0 }, deleted: { count: 0 } },
  failures: [], fingerprint: null, evidence_id: null, hint: null, warnings: [] }

function fakeApi() {
  return {
    check: vi.fn<WorkbenchApi['check']>().mockResolvedValue(checkReply),
    dryRun: vi.fn<WorkbenchApi['dryRun']>().mockResolvedValue(dryReply),
    choices: vi.fn<WorkbenchApi['choices']>().mockResolvedValue([{ label: 'Level 1', value: 'L1' }]),
    save: vi.fn<WorkbenchApi['save']>().mockResolvedValue({ name: draft.name }),
    list: vi.fn<WorkbenchApi['list']>().mockResolvedValue([{ name: draft.name, description: draft.description, builtin: false }]),
    load: vi.fn<WorkbenchApi['load']>().mockResolvedValue(structuredClone(draft)),
    remove: vi.fn<WorkbenchApi['remove']>().mockResolvedValue(undefined),
    exportYaml: vi.fn<WorkbenchApi['exportYaml']>().mockResolvedValue('name: test_tool\ncode_template: return 1;\n'),
    importYaml: vi.fn<WorkbenchApi['importYaml']>().mockResolvedValue({ name: draft.name }),
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

async function setup() {
  const api = fakeApi()
  const model = new WorkbenchModel(api)
  await model.refresh()
  model.edit(structuredClone(draft))
  return { api, model }
}

const html = (model: WorkbenchModel) => renderToStaticMarkup(<WorkbenchView model={model} onRun={() => {}} />)

describe('saving the current dry-run draft', () => {
  it('pre-fills the description and keeps save disabled before a successful dry run', async () => {
    const api = fakeApi()
    expect(renderToStaticMarkup(<Workbench api={api} onRun={() => {}} initialDescription="User brief" />)).toContain('value="User brief"')
    const { model, api: commands } = await setup()
    await model.save()
    expect(commands.save).not.toHaveBeenCalled()
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    expect(html(model)).toMatch(/disabled=""[^>]*>Save as tool/)
    await model.dryRun()
    expect(saveDisabledReason(model.getSnapshot())).toBeNull()
    await model.save()
    expect(commands.save).toHaveBeenCalledWith(draft)
    expect(model.getSnapshot().actions.save).toEqual({ status: 'success', message: 'Saved test_tool.' })
  })

  it.each([{ success: false, rolled_back: true }, { success: true, rolled_back: false }, { success: true, rolled_back: null }])('rejects unsafe or failed replies: %j', async reply => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ...dryReply, ...reply })
    await model.dryRun()
    await model.save()
    expect(api.save).not.toHaveBeenCalled()
    expect(model.getSnapshot().actions.dryRun?.status).toBe('error')
    if (reply.rolled_back !== true) {
      expect(html(model)).toContain('Rollback is not confirmed')
      expect(html(model)).not.toContain('Model was not changed')
    }
  })

  it('invalidates a successful dry run after code or declaration edits, even if then reverted', async () => {
    const { model } = await setup()
    await model.dryRun()
    model.edit({ description: 'New description', validator: 'count_delta' })
    expect(saveDisabledReason(model.getSnapshot())).toBeNull()
    model.edit({ code: 'return 2;' })
    model.edit({ code: draft.code })
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    await model.dryRun()
    model.edit({ parameters: [param({ required: false })] })
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    expect(model.getSnapshot().dryRun).toBeNull()
  })

  it('ignores a successful dry-run response arriving after code changed', async () => {
    const { api, model } = await setup()
    const pending = deferred<unknown>()
    api.dryRun.mockReturnValue(pending.promise)
    const run = model.dryRun()
    await Promise.resolve()
    expect(api.dryRun).toHaveBeenCalledOnce()
    model.edit({ code: 'return 3;' })
    pending.resolve(dryReply)
    await run
    expect(model.getSnapshot().dryRun).toBeNull()
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    expect(model.getSnapshot().actions.dryRun).toBeUndefined()
  })

  it('does not trust a successful HTTP response with an invalid body', async () => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ok: true })
    await model.dryRun()
    expect(html(model)).toContain('Invalid dry-run response')
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
  })
})

describe('model options and input values', () => {
  it('loads real options and requires a matching choice before sending a dry run', async () => {
    const { api, model } = await setup()
    const level = param({ name: 'level_name', type: 'string', source: 'levels' })
    model.edit({ parameters: [level] })
    await model.dryRun()
    expect(api.choices).toHaveBeenCalledWith(level)
    expect(api.dryRun).not.toHaveBeenCalled()
    expect(html(model)).toContain('Level 1')
    model.setValue('level_name', 'not-in-model')
    await model.dryRun()
    expect(api.dryRun).not.toHaveBeenCalled()
    model.setValue('level_name', 'L1')
    await model.dryRun()
    expect(api.dryRun).toHaveBeenCalledWith(draft.code, [level], { level_name: 'L1' })
  })

  it('preserves false, zero, and numeric model ids as their actual types', async () => {
    const { api, model } = await setup()
    api.choices.mockResolvedValue([{ label: 'Wall 42', value: 42 }])
    const parameters = [param(), param({ name: 'flip', type: 'bool' }), param({ name: 'wall', type: 'int', source: 'elements', category: 'OST_Walls' })]
    model.edit({ parameters })
    model.setValue('height', inputValue('double', '0'))
    model.setValue('flip', inputValue('bool', 'false'))
    model.setValue('wall', 42)
    await model.dryRun()
    expect(api.dryRun).toHaveBeenCalledWith(draft.code, parameters, { height: 0, flip: false, wall: 42 })
    expect(api.choices).toHaveBeenCalledOnce()
  })

  it('rejects duplicate names, missing categories, non-integers and cleared required values', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param(), param()] })
    await model.check()
    expect(html(model)).toContain('Duplicate parameter')
    model.edit({ parameters: [param({ source: 'elements' })] })
    await model.dryRun()
    expect(html(model)).toContain('OST_*')
    model.edit({ parameters: [param({ type: 'int', default: 4 })] })
    model.setValue('height', 1.5)
    await model.dryRun()
    expect(html(model)).toContain('height must be int')
    model.setValue('height', undefined)
    await model.dryRun()
    expect(html(model)).toContain('Fill a value for height')
    expect(api.dryRun).not.toHaveBeenCalled()
  })

  it('discards stale options and preserves current errors when a declaration changes', async () => {
    const { api, model } = await setup()
    const pending = deferred<{ label: string; value: unknown }[]>()
    api.choices.mockReturnValue(pending.promise)
    model.edit({ parameters: [param({ type: 'string', source: 'levels' })] })
    const run = model.dryRun()
    model.edit({ parameters: [] })
    pending.resolve([{ label: 'Old level', value: 'old' }])
    await run
    expect(model.getSnapshot().choices).toEqual({})
    expect(model.getSnapshot().actions.dryRun).toBeUndefined()
    expect(api.dryRun).not.toHaveBeenCalled()
  })

  it('shows the API error for options and does not run with unavailable choices', async () => {
    const { api, model } = await setup()
    api.choices.mockRejectedValue(new Error('Revit is disconnected'))
    model.edit({ parameters: [param({ type: 'string', source: 'levels' })] })
    await model.dryRun()
    expect(html(model)).toContain('Revit is disconnected')
    expect(api.dryRun).not.toHaveBeenCalled()
  })

  it('handles parameter names that match Object prototype keys as ordinary fields', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param({ name: '__proto__' }), param({ name: 'constructor' })] })
    expect(html(model)).toContain('__proto__')
    model.setValue('__proto__', 1)
    model.setValue('constructor', 2)
    await model.dryRun()
    expect(Object.keys(api.dryRun.mock.calls[0][2])).toEqual(['__proto__', 'constructor'])
    expect(api.dryRun.mock.calls[0][2].constructor).toBe(2)
  })
})

describe('check diagnostics and code editing', () => {
  it('shows pending, diagnostic failure and API rejection states', async () => {
    const { api, model } = await setup()
    const pending = deferred<unknown>()
    api.check.mockReturnValue(pending.promise)
    const check = model.check()
    expect(html(model)).toContain('Checking...')
    pending.resolve({ ...checkReply, compiled: false, diagnostics: [{ id: 'CS0103', severity: 'error', line: 2, column: 3, message: 'Name is missing', symbol: 'missing', in_wrapper: false }] })
    await check
    expect(html(model)).toContain('Line 2, column 3')
    expect(html(model)).toContain('CS0103: Name is missing')
    expect(model.getSnapshot().actions.check?.status).toBe('error')
    api.check.mockRejectedValue(new Error('Check timed out'))
    await model.check()
    expect(html(model)).toContain('Check timed out')
  })

  it('locates a diagnostic in the user code and never navigates to wrapper or included-file lines', () => {
    const location = { line: 2, column: 3, in_wrapper: false }
    expect(diagnosticRange('first\nsecond\nthird', location)).toEqual([8, 12])
    expect(diagnosticRange('a\r\nbc', { ...location, column: 1 })).toEqual([3, 5])
    expect(diagnosticRange(draft.code, { ...location, in_wrapper: true })).toBeNull()
    expect(diagnosticRange(draft.code, { ...location, file: 'Helper.cs' })).toBeNull()
    expect(diagnosticRange(draft.code, { ...location, line: 99 })).toBeNull()
  })

  it('inserts Tab indentation and maintains selection offsets', () => {
    expect(indentSelection('return 1;', 0, 0)).toEqual({ code: '  return 1;', start: 2, end: 2 })
    expect(indentSelection('one\ntwo', 0, 7)).toEqual({ code: '  one\n  two', start: 2, end: 11 })
  })

  it('keeps old checks from overwriting a new draft and reports unsupported checks truthfully', async () => {
    const { api, model } = await setup()
    const pending = deferred<unknown>()
    api.check.mockReturnValue(pending.promise)
    const check = model.check()
    model.edit({ code: 'return 4;' })
    pending.resolve(checkReply)
    await check
    expect(model.getSnapshot().check).toBeNull()
    api.check.mockResolvedValue({ ...checkReply, checked: false, compiled: null, reason: 'no_check_code: unavailable' })
    await model.check()
    expect(html(model)).toContain('no_check_code: unavailable')
  })
})

describe('library and run delegation', () => {
  it('keeps built-in drafts and declarations read-only until copied', async () => {
    const { api, model } = await setup()
    api.list.mockResolvedValue([{ name: draft.name, description: draft.description, builtin: true }])
    await model.refresh()
    await model.open(draft.name)
    model.edit({ code: 'changed', name: 'changed' })
    expect(model.getSnapshot().draft).toEqual(draft)
    expect(html(model)).toContain('readOnly=""')
    expect(html(model)).toContain('<fieldset disabled=""')
    model.requestDelete(draft.name)
    await model.confirmDelete()
    expect(api.remove).not.toHaveBeenCalled()
    await model.dryRun()
    expect(saveDisabledReason(model.getSnapshot())).toContain('read-only')
    model.copyAsMine()
    expect(model.getSnapshot().draft.name).toBe('test_tool_copy')
    expect(model.getSnapshot().builtin).toBe(false)
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    model.edit({ name: draft.name })
    await model.dryRun()
    expect(saveDisabledReason(model.getSnapshot())).toContain('built-in')
  })

  it('requires a second explicit delete action and supports cancellation', async () => {
    const { api, model } = await setup()
    model.requestDelete(draft.name)
    expect(api.remove).not.toHaveBeenCalled()
    expect(html(model)).toContain('Confirm delete')
    model.cancelDelete()
    await model.confirmDelete()
    expect(api.remove).not.toHaveBeenCalled()
    model.requestDelete(draft.name)
    await model.confirmDelete()
    expect(api.remove).toHaveBeenCalledWith(draft.name)
    expect(model.getSnapshot().actions.remove?.status).toBe('success')
  })

  it('exports exact YAML for download and imports it through the injected API', async () => {
    const { api, model } = await setup()
    let downloaded = ''
    await model.exportTool(draft.name, (name, text) => { expect(name).toBe(draft.name); downloaded = text })
    await model.importTool(Promise.resolve(downloaded))
    expect(api.importYaml).toHaveBeenCalledWith('name: test_tool\ncode_template: return 1;\n')
    expect(model.getSnapshot().actions.export?.status).toBe('success')
    expect(model.getSnapshot().actions.import?.status).toBe('success')
  })

  it('passes an immutable draft and typed values to onRun without saving or executing through the API', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param()] })
    model.setValue('height', 3000)
    const onRun = vi.fn(async (copy: ToolDraft, values: Record<string, unknown>) => { copy.code = 'host copy'; values.height = 2 })
    await model.run(onRun)
    expect(onRun).toHaveBeenCalledOnce()
    expect(model.getSnapshot().draft.code).toBe(draft.code)
    expect(model.getSnapshot().values.height).toBe(3000)
    expect(api.save).not.toHaveBeenCalled()
    expect(api.dryRun).not.toHaveBeenCalled()
    expect(html(model)).toContain('Confirmation and execution are handled by the host')
  })

  it('does not replace an edited draft with a late load response', async () => {
    const { api, model } = await setup()
    const pending = deferred<ToolDraft>()
    api.load.mockReturnValue(pending.promise)
    const opening = model.open(draft.name)
    model.edit({ description: 'Edited while loading' })
    pending.resolve(draft)
    await opening
    expect(model.getSnapshot().draft.description).toBe('Edited while loading')
    expect(html(model)).toContain('The draft changed while loading')
  })

  it('uses an updated interface object without losing the current draft', async () => {
    const { model } = await setup()
    const updated = fakeApi()
    model.edit({ description: 'Work in progress' })
    model.setApi(updated)
    await model.check()
    expect(updated.check).toHaveBeenCalledWith(draft.code, [])
    expect(model.getSnapshot().draft.description).toBe('Work in progress')
  })

  it.each(['save', 'load', 'remove', 'exportYaml', 'importYaml'] as const)('shows interface exceptions from %s', async action => {
    const { api, model } = await setup()
    api[action].mockRejectedValue(new Error(`${action} rejected`))
    if (action === 'save') { await model.dryRun(); await model.save() }
    if (action === 'load') await model.open(draft.name)
    if (action === 'remove') { model.requestDelete(draft.name); await model.confirmDelete() }
    if (action === 'exportYaml') await model.exportTool(draft.name, () => {})
    if (action === 'importYaml') await model.importTool('name: test')
    expect(html(model)).toContain(`${action} rejected`)
  })
})

describe('reply field fidelity', () => {
  it('keeps check hints, warnings and nullable positions without inventing a compilation success', () => {
    expect(readCheck({ ...checkReply, checked: false, compiled: null, diagnostics: [{ id: 'X', message: 'm', line: null, column: null, in_wrapper: true }], warnings: ['note'] }))
      .toMatchObject({ checked: false, compiled: null, warnings: ['note'], diagnostics: [{ line: null, column: null, in_wrapper: true }] })
  })

  it('does not confuse dry-run changes with the ledger summary or missing counts with zero', async () => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ...dryReply, changes: { added: { count: 3, ids: [1, 2, 3] } } })
    await model.dryRun()
    expect(html(model)).toContain('<dt>added</dt><dd>3</dd>')
    expect(html(model)).toContain('<dt>modified</dt><dd>unknown</dd>')
    expect(readDryRun({ ...dryReply, changes: null }).changes).toBeNull()
    expect(readDryRun({ ...dryReply, rolled_back: undefined }).rolled_back).toBeNull()
  })
})
