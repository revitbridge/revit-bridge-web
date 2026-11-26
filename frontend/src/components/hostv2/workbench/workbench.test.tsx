import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { ExistingTool, ToolDraft, ToolParamDraft, WorkbenchApi } from '../../../types/hostv2'
import { BridgeApiError } from '../../shared/http'
import { errorCode, errorDetails, parseValue, saveDisabledReason, WorkbenchModel, workbenchFailure } from './model'
import { diagnosticRange, indentSelection, readCheck, readDryRun } from './results'
import { downloadYaml, REVOKE_AFTER_MS } from './download'
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
    lookup: vi.fn<WorkbenchApi['lookup']>().mockResolvedValue(null),
    save: vi.fn<WorkbenchApi['save']>().mockResolvedValue({ name: draft.name }),
    list: vi.fn<WorkbenchApi['list']>().mockResolvedValue([{ name: draft.name, description: draft.description, builtin: false, origin: 'user' }]),
    load: vi.fn<WorkbenchApi['load']>().mockResolvedValue(structuredClone(draft)),
    remove: vi.fn<WorkbenchApi['remove']>().mockResolvedValue(undefined),
    exportYaml: vi.fn<WorkbenchApi['exportYaml']>().mockResolvedValue('name: test_tool\ncode_template: return 1;\n'),
    importYaml: vi.fn<WorkbenchApi['importYaml']>().mockResolvedValue({ name: draft.name, replaced: false }),
  } satisfies WorkbenchApi
}

/* An error as components/shared/http.ts throws it: the message, and the server's body. */
function refusal(status: number, body: Record<string, unknown>) {
  return Object.assign(new Error(`${status}: ${body.error}${body.message ? `: ${body.message}` : ''}`), { status, body })
}

// What lookup answers for a saved tool of that name.
const existing = (dropped: string[] = [], unseen: string[] = []): ExistingTool => ({ name: draft.name, dropped, unseen })

// Lets the awaited name lookup of a save (and other chained promises) settle.
const flush = () => new Promise(resolve => setTimeout(resolve, 0))

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

  it.each(['edit', 'new', 'open', 'copy'] as const)('blocks another save after %s while an earlier write is still pending', async change => {
    const { api, model } = await setup()
    const pending = deferred<void>()
    let stored = ''
    api.save.mockImplementationOnce(async saving => {
      await pending.promise
      stored = saving.code
      return { name: saving.name }
    }).mockImplementation(async saving => {
      stored = saving.code
      return { name: saving.name }
    })
    await model.dryRun()
    const firstSave = model.save()
    await flush()
    expect(api.save).toHaveBeenCalledOnce()
    if (change === 'new') model.newDraft()
    if (change === 'open') await model.open(draft.name)
    if (change === 'copy') model.copyAsMine()
    model.edit({ name: draft.name, code: 'return 2;' })
    await model.dryRun()
    expect(model.getSnapshot().actions.save?.status).toBe('running')
    expect(saveDisabledReason(model.getSnapshot())).toContain('current save')
    expect(html(model)).toMatch(/disabled=""[^>]*>Saving/)
    await model.save()
    expect(api.save).toHaveBeenCalledOnce()
    expect(model.getSnapshot().actions.save?.status).toBe('running')
    pending.resolve()
    await firstSave
    expect(stored).toBe(draft.code)
    expect(model.getSnapshot().draft.code).toBe('return 2;')
    expect(model.getSnapshot().actions.save?.message).toContain('current draft has not been saved')
    expect(saveDisabledReason(model.getSnapshot())).toBeNull()
    await model.save()
    expect(api.save).toHaveBeenCalledTimes(2)
    expect(stored).toBe('return 2;')
  })

  it('releases the save lock after an earlier write fails without losing edited code', async () => {
    const { api, model } = await setup()
    const pending = deferred<{ name: string }>()
    api.save.mockReturnValueOnce(pending.promise)
    await model.dryRun()
    const firstSave = model.save()
    await flush()
    model.edit({ code: 'return 2;' })
    await model.dryRun()
    pending.reject(new Error('Save connection failed'))
    await firstSave
    expect(model.getSnapshot().actions.save?.status).toBe('error')
    expect(model.getSnapshot().draft.code).toBe('return 2;')
    expect(saveDisabledReason(model.getSnapshot())).toBeNull()
    await model.save()
    expect(api.save).toHaveBeenCalledTimes(2)
    expect(api.save.mock.calls[1][0].code).toBe('return 2;')
  })
})

describe('saving never replaces a tool silently', () => {
  it('asks before replacing a tool of that name; Cancel keeps it, Replace saves', async () => {
    const { api, model } = await setup()
    api.lookup.mockResolvedValue(existing())
    await model.dryRun()
    await model.save()
    expect(api.lookup).toHaveBeenCalledWith('test_tool')
    expect(api.save).not.toHaveBeenCalled()
    expect(html(model)).toContain('A tool named test_tool exists. Replace it with this draft?')
    expect(saveDisabledReason(model.getSnapshot())).toBe('Answer whether to replace test_tool first.')
    model.cancelReplace()
    expect(model.getSnapshot().replace).toBeNull()
    expect(api.save).not.toHaveBeenCalled()
    await model.save()
    await model.confirmReplace()
    expect(api.save).toHaveBeenCalledOnce()
    expect(api.save).toHaveBeenCalledWith(draft)
    expect(model.getSnapshot().actions.save).toEqual({ status: 'success', message: 'Saved test_tool.' })
    expect(html(model)).not.toContain('Replace it with this draft?')
  })

  it('names what the saved tool loses and needs its own click for that; Cancel there saves nothing', async () => {
    const { api, model } = await setup()
    api.lookup.mockResolvedValue(existing(['preconditions (1)', 'applies_when (2)'], ['revit_versions', 'fixtures']))
    await model.dryRun()
    await model.save()
    expect(html(model)).toContain('this page cannot see revit_versions, fixtures, which are not kept if the saved file has them.')
    await model.confirmReplace()
    expect(api.save).not.toHaveBeenCalled()
    expect(html(model)).toContain('These parts of the saved test_tool will not be kept: preconditions (1); applies_when (2).')
    expect(html(model)).toContain('Replace and drop them')
    model.cancelReplace()
    expect(api.save).not.toHaveBeenCalled()
    await model.save()
    await model.confirmReplace()
    await model.confirmReplace()
    expect(api.save).toHaveBeenCalledOnce()
  })

  it('an edit withdraws the question; a new name is looked up again', async () => {
    const { api, model } = await setup()
    api.lookup.mockResolvedValueOnce(existing())
    await model.dryRun()
    await model.save()
    model.edit({ name: 'other_tool' })
    expect(model.getSnapshot().replace).toBeNull()
    await model.confirmReplace()
    expect(api.save).not.toHaveBeenCalled()
    await model.save()
    expect(api.lookup).toHaveBeenLastCalledWith('other_tool')
    expect(api.save.mock.calls[0][0].name).toBe('other_tool')
  })

  it('shows the server refusal of a built-in name as it is, and frees the button', async () => {
    const { api, model } = await setup()
    api.lookup.mockResolvedValue(existing())
    api.save.mockRejectedValue(refusal(409, { error: 'builtin_read_only', message: "'test_tool' is a built-in tool and built-in tools are read-only", tool: 'test_tool' }))
    await model.dryRun()
    await model.save()
    await model.confirmReplace()
    expect(model.getSnapshot().actions.save?.status).toBe('error')
    expect(html(model)).toContain('409: builtin_read_only: &#x27;test_tool&#x27; is a built-in tool and built-in tools are read-only')
    expect(html(model)).toMatch(/<button[^>]*class="btn-primary">Save as tool</)
  })

  it('a failed name lookup is shown and nothing is saved', async () => {
    const { api, model } = await setup()
    api.lookup.mockRejectedValue(refusal(422, { error: 'invalid_tool_name', message: 'a tool name is letters, digits and _' }))
    await model.dryRun()
    await model.save()
    expect(api.save).not.toHaveBeenCalled()
    expect(html(model)).toContain('422: invalid_tool_name: a tool name is letters, digits and _')
    expect(model.getSnapshot().actions.save?.status).toBe('error')
  })

  it('an edit while the name is looked up stops the save', async () => {
    const { api, model } = await setup()
    const pending = deferred<ExistingTool | null>()
    api.lookup.mockReturnValueOnce(pending.promise)
    await model.dryRun()
    const saving = model.save()
    model.edit({ description: 'changed' })
    pending.resolve(null)
    await saving
    expect(api.save).not.toHaveBeenCalled()
    expect(model.getSnapshot().actions.save?.message).toBe('The draft changed while its name was looked up. Save again.')
  })
})

describe('server refusals are shown as they are, in place', () => {
  it.each([
    ['check', refusal(422, { error: 'invalid_pack', problems: ['parameter height: unknown type "length"', 'code_template is empty'] }),
      ['422: invalid_pack', 'parameter height: unknown type &quot;length&quot;', 'code_template is empty']],
    ['dryRun', refusal(422, { error: 'render_failed', message: "The package could not fill the draft's template with these values", errors: ["Template still contains placeholder(s) ['width']"] }),
      ['422: render_failed: The package could not fill the draft&#x27;s template with these values', 'Template still contains placeholder(s) [&#x27;width&#x27;]']],
    ['check', refusal(429, { error: 'rate_limited', message: 'Too many requests from this address; try again in a minute' }),
      ['429: rate_limited: Too many requests from this address; try again in a minute']],
    ['dryRun', refusal(503, { error: 'revit_unreachable', message: 'Revit add-in not reachable' }),
      ['503: revit_unreachable: Revit add-in not reachable']],
  ] as const)('%s: %s', async (action, error, shown) => {
    const { api, model } = await setup()
    model.edit({ parameters: [param({ required: false, default: 3000 })] })
    api[action].mockRejectedValue(error)
    await model[action]()
    const page = html(model)
    for (const text of shown) expect(page).toContain(text)
    expect(model.getSnapshot().actions[action]?.status).toBe('error')
    expect(page).not.toContain('Checking...')
    expect(page).not.toContain('Dry-running...')
  })

  it('code with a leftover placeholder and no parameters is refused by the dry run, so Save stays off', async () => {
    const { api, model } = await setup()
    model.edit({ code: 'return $"{x}";' })
    api.dryRun.mockRejectedValue(refusal(422, { error: 'render_failed', message: "The package could not fill the draft's template with these values",
      errors: ["Template still contains placeholder(s) ['x']: declare them as parameters or remove them from the code"] }))
    await model.dryRun()
    expect(api.dryRun).toHaveBeenCalledWith('return $"{x}";', [], {})
    expect(html(model)).toContain('Template still contains placeholder(s) [&#x27;x&#x27;]')
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
    await model.save()
    expect(api.save).not.toHaveBeenCalled()
  })

  it('a save the sandbox blocks lists its warnings', async () => {
    const { api, model } = await setup()
    api.save.mockRejectedValue(refusal(400, { error: 'blocked', message: 'Code review failed', warnings: ['System.IO.File is not allowed'] }))
    await model.dryRun()
    await model.save()
    expect(html(model)).toContain('400: blocked: Code review failed')
    expect(html(model)).toContain('<li>System.IO.File is not allowed</li>')
  })

  it('reads spec errors, text errors and unknown items without dropping any', () => {
    expect(errorDetails(refusal(422, { error: 'invalid_spec', errors: [{ code: 'missing_param', param: 'height', message: 'height is required' }, 'plain', 7] })))
      .toEqual(['missing_param (height): height is required', 'plain', '7'])
    expect(errorDetails(new Error('no body'))).toEqual([])
    const real = new BridgeApiError(422, JSON.stringify({ error: 'invalid_pack', problems: ['parameters: not a list'] }), 'application/json')
    expect(workbenchFailure(real)).toEqual({ message: '422: invalid_pack', details: ['parameters: not a list'] })
    expect(errorCode(new BridgeApiError(409, JSON.stringify({ error: 'pack_exists', tool: 'mine' })))).toBe('pack_exists')
  })
})

describe('importing asks before replacing', () => {
  it('asks when the server says a tool of yours has that name, then imports with overwrite', async () => {
    const { api, model } = await setup()
    api.importYaml.mockRejectedValueOnce(refusal(409, { error: 'pack_exists', message: "A tool named 'mine' exists", tool: 'mine' }))
      .mockResolvedValueOnce({ name: 'mine', replaced: true })
    await model.importTool('name: mine')
    expect(html(model)).toContain('A tool named mine exists. Replace it with the imported file?')
    expect(model.getSnapshot().actions.import?.status).toBe('idle')
    await model.confirmImport()
    expect(api.importYaml).toHaveBeenLastCalledWith('name: mine', { overwrite: true })
    expect(model.getSnapshot().actions.import).toEqual({ status: 'success', message: 'Imported mine (replaced).' })
  })

  it('Cancel keeps the tool there; a built-in name is refused as the server says', async () => {
    const { api, model } = await setup()
    api.importYaml.mockRejectedValueOnce(refusal(409, { error: 'pack_exists', tool: 'mine' }))
    await model.importTool('name: mine')
    model.cancelImport()
    expect(api.importYaml).toHaveBeenCalledOnce()
    expect(model.getSnapshot().importReplace).toBeNull()
    api.importYaml.mockRejectedValueOnce(refusal(409, { error: 'builtin_read_only', message: "'create_wall' is a built-in tool" }))
    await model.importTool('name: create_wall')
    expect(html(model)).toContain('409: builtin_read_only: &#x27;create_wall&#x27; is a built-in tool')
    expect(model.getSnapshot().importReplace).toBeNull()
  })
})

describe('the tool list and the edit gate', () => {
  it('lists your tools with their directory; built-in tools are read-only, open and export only', async () => {
    const { api, model } = await setup()
    api.list.mockResolvedValue([
      { name: 'mine', description: 'Mine', builtin: false, origin: 'user', directory: 'C:/data/capabilities' },
      { name: 'create_wall', description: 'Wall', builtin: true, origin: 'builtin' },
    ])
    await model.refresh()
    const page = html(model)
    const [own, builtins] = page.split('Built-in tools (1, read-only')
    expect(own).toContain('<strong>mine</strong>')
    expect(own).toContain('<small>C:/data/capabilities</small>')
    expect(own).toContain('>Delete</button>')
    expect(builtins).toContain('<strong>create_wall</strong>')
    expect(builtins).toContain('>Open</button>')
    expect(builtins).toContain('>Export YAML</button>')
    expect(builtins).not.toContain('>Delete</button>')
  })

  it('a host that does not let the page change tools hides import and delete and says why', async () => {
    const { api, model } = await setup()
    model.setGate(false, 'Enter the admin password in Settings to edit, delete or save packs.')
    await model.dryRun()
    const page = html(model)
    expect(page).not.toContain('Import YAML')
    expect(page).not.toContain('>Delete</button>')
    expect(page).toContain('Enter the admin password in Settings')
    expect(saveDisabledReason(model.getSnapshot())).toBe('Enter the admin password in Settings to edit, delete or save packs.')
    await model.save()
    await model.importTool('name: x')
    model.requestDelete(draft.name)
    expect(api.save).not.toHaveBeenCalled()
    expect(api.importYaml).not.toHaveBeenCalled()
    expect(model.getSnapshot().deleteName).toBeNull()
    expect(renderToStaticMarkup(<Workbench api={api} onRun={() => {}} canEdit={false} editNote="read-only here" />)).not.toContain('Import YAML')
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
    model.setValue('height', '0')
    model.setValue('flip', false)
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
    model.edit({ parameters: [param({ type: 'int' })] })
    model.setValue('height', '1.5')
    await model.dryRun()
    expect(html(model)).toContain('height: &quot;1.5&quot; is not a whole number.')
    model.setValue('height', '')
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

describe('number fields keep the typed text and read it when it is used', () => {
  it.each([
    ['2.5', 2.5], ['0.5', 0.5], ['-1.25', -1.25], [' 3000 ', 3000], ['2.', 2], ['.5', 0.5], ['1e3', 1000], ['', undefined],
  ])('reads %j as a double: %j', (text, value) => {
    expect(parseValue('double', text)).toEqual({ value })
  })

  it.each(['abc', '2,5', '1.2.3', '0x10', 'Infinity', 'NaN', '-', '.', '5 mm'])('refuses %j as a number instead of reading it as another value', text => {
    expect(parseValue('double', text)).toEqual({ error: `"${text}" is not a number` })
    expect(parseValue('int', text)).toEqual({ error: `"${text}" is not a whole number` })
  })

  it('reads whole numbers, booleans and loaded values without changing them', () => {
    expect(parseValue('int', '-4')).toEqual({ value: -4 })
    expect(parseValue('int', '2.5')).toEqual({ error: '"2.5" is not a whole number' })
    expect(parseValue('int', 7)).toEqual({ value: 7 })
    expect(parseValue('double', 2.5)).toEqual({ value: 2.5 })
    expect(parseValue('bool', 'false')).toEqual({ value: false })
    expect(parseValue('string', '2.50')).toEqual({ value: '2.50' })
    expect(parseValue('string', '')).toEqual({ value: undefined })
  })

  it.each([['2.5', 2.5], ['0.5', 0.5], ['-1.25', -1.25]])('a value typed key by key as %j reaches the dry run as %j', async (text, value) => {
    const { api, model } = await setup()
    model.edit({ parameters: [param()] })
    for (let end = 1; end <= text.length; end++) {
      model.setValue('height', text.slice(0, end))
      expect(html(model)).toContain(`value="${text.slice(0, end)}"`)
    }
    await model.dryRun()
    expect(api.dryRun).toHaveBeenCalledWith(draft.code, [param()], { height: value })
  })

  it('an empty required value or a non-number is an error shown in place, never sent', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param()] })
    model.setValue('height', '')
    await model.dryRun()
    expect(html(model)).toContain('Fill a value for height')
    model.setValue('height', '2,5')
    await model.dryRun()
    expect(html(model)).toContain('height: &quot;2,5&quot; is not a number.')
    expect(model.getSnapshot().actions.dryRun?.status).toBe('error')
    await model.run(() => {})
    expect(model.getSnapshot().actions.run?.message).toContain('is not a number')
    expect(api.dryRun).not.toHaveBeenCalled()
  })

  it('a default typed as 0.5 is declared, checked and saved as 0.5; a bad default blocks all three', async () => {
    const { api, model } = await setup()
    for (const text of ['0', '0.', '0.5']) model.edit({ parameters: [param({ required: false, default: text })] })
    expect(html(model)).toContain('value="0.5"')
    await model.check()
    expect(api.check).toHaveBeenCalledWith(draft.code, [param({ required: false, default: 0.5 })])
    await model.dryRun()
    expect(api.dryRun).toHaveBeenCalledWith(draft.code, [param({ required: false, default: 0.5 })], { height: 0.5 })
    await model.save()
    expect(api.save.mock.calls[0][0].parameters).toEqual([param({ required: false, default: 0.5 })])

    model.edit({ parameters: [param({ required: false, default: 'abc' })] })
    expect(saveDisabledReason(model.getSnapshot())).toBe('height: the default "abc" is not a number.')
    await model.check()
    expect(html(model)).toContain('height: the default &quot;abc&quot; is not a number.')
    expect(api.check).toHaveBeenCalledOnce()
    model.edit({ parameters: [param({ required: false, default: '' })] })
    await model.check()
    expect(api.check).toHaveBeenLastCalledWith(draft.code, [param({ required: false })])
  })
})

describe('values and defaults', () => {
  it('a value field follows the default until the designer sets it, then keeps what was set', async () => {
    const { api, model } = await setup()
    const optional = (fallback: unknown) => param({ required: false, default: fallback })
    model.edit({ parameters: [optional('1.5')] })
    model.edit({ parameters: [optional('2.5')] })
    await model.dryRun()
    expect(api.dryRun).toHaveBeenLastCalledWith(draft.code, [optional(2.5)], { height: 2.5 })
    model.setValue('height', '7.25')
    model.edit({ parameters: [optional('3.5')] })
    await model.dryRun()
    expect(api.dryRun).toHaveBeenLastCalledWith(draft.code, [optional(3.5)], { height: 7.25 })
    expect(html(model)).not.toContain('will be used')
  })

  it('a cleared optional value sends nothing, and the field says the default will be used', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param({ required: false, default: '3.5' })] })
    model.setValue('height', '')
    expect(html(model)).toContain('default 3.5 will be used')
    await model.dryRun()
    expect(api.dryRun).toHaveBeenLastCalledWith(draft.code, [param({ required: false, default: 3.5 })], {})
    model.edit({ parameters: [param({ required: false, type: 'string', default: 'abc' })] })
    model.setValue('height', '')
    expect(html(model)).toContain('default &quot;abc&quot; will be used')
  })

  it('entering a default clears Required; a declaration with both is refused before any request', async () => {
    const { api, model } = await setup()
    model.edit({ parameters: [param()] })
    expect(html(model)).toMatch(/<input type="checkbox" checked=""\/>Required/)
    model.setDefault(0, '5')
    expect(model.getSnapshot().draft.parameters[0]).toMatchObject({ default: '5', required: false })
    expect(html(model)).toMatch(/<input type="checkbox"\/>Required/)
    model.setDefault(0, undefined)
    expect(model.getSnapshot().draft.parameters[0]).toMatchObject({ default: undefined, required: false })
    model.edit({ parameters: [param({ required: true, default: '5' })] })
    expect(saveDisabledReason(model.getSnapshot())).toBe('height: a parameter with a default is not required.')
    await model.check()
    await model.dryRun()
    expect(html(model)).toContain('height: a parameter with a default is not required.')
    expect(api.check).not.toHaveBeenCalled()
    expect(api.dryRun).not.toHaveBeenCalled()
  })

  it('a default of null is a default: kept apart from none, sent as null, and its field says so', async () => {
    const { api, model } = await setup()
    const withNull = param({ type: 'string', required: false, default: null })
    model.edit({ parameters: [withNull] })
    expect(parseValue('string', null)).toEqual({ value: null })
    expect(html(model)).toContain('placeholder="null (the saved default)"')
    expect(html(model)).toContain('default null will be used')
    await model.check()
    expect(api.check).toHaveBeenLastCalledWith(draft.code, [withNull])
    await model.dryRun()
    expect(api.dryRun).toHaveBeenLastCalledWith(draft.code, [withNull], {})
    model.edit({ parameters: [{ ...withNull, required: true }] })
    expect(saveDisabledReason(model.getSnapshot())).toBe('height: a parameter with a default is not required.')
  })

  it('a model option is selected by its typed value: a default typed as "3" selects the option 3', async () => {
    const { api, model } = await setup()
    api.choices.mockResolvedValue([{ label: 'Wall 2', value: 2 }, { label: 'Wall 3', value: 3 }])
    model.edit({ parameters: [param({ name: 'wall', type: 'int', source: 'elements', category: 'OST_Walls', required: false, default: '3' })] })
    await model.loadChoices()
    expect(html(model)).toContain('<option value="1" selected="">Wall 3</option>')
    await model.dryRun()
    expect(api.dryRun.mock.calls[0][2]).toEqual({ wall: 3 })
  })
})

describe('export download', () => {
  it('keeps the file URL for a minute, so a browser that starts the download late still gets it', () => {
    vi.useFakeTimers()
    const anchor = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
    const revoke = vi.fn()
    vi.stubGlobal('document', { createElement: () => anchor, body: { appendChild: vi.fn() } })
    vi.stubGlobal('URL', { createObjectURL: () => 'blob:1', revokeObjectURL: revoke })
    try {
      downloadYaml('my tool', 'name: my_tool\n')
      expect(anchor).toMatchObject({ href: 'blob:1', download: 'my_tool.yaml' })
      expect(anchor.click).toHaveBeenCalledOnce()
      vi.advanceTimersByTime(REVOKE_AFTER_MS - 1)
      expect(revoke).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(revoke).toHaveBeenCalledWith('blob:1')
      expect(REVOKE_AFTER_MS).toBe(60_000)
    } finally {
      vi.unstubAllGlobals()
      vi.useRealTimers()
    }
  })
})

describe('the host learns of every draft change', () => {
  it('edit, setValue, open, copy and new each call the listener; a refused edit of a built-in does not', async () => {
    const { api, model } = await setup()
    const changed = vi.fn()
    model.setDraftListener(changed)
    model.edit({ code: 'return 7;' })
    model.edit({ description: 'only the text' })
    model.setValue('height', '1')
    expect(changed).toHaveBeenCalledTimes(3)
    await model.open(draft.name)
    model.copyAsMine()
    model.newDraft()
    expect(changed).toHaveBeenCalledTimes(6)
    api.list.mockResolvedValue([{ name: draft.name, description: '', builtin: true, origin: 'builtin' }])
    await model.refresh()
    await model.open(draft.name)
    changed.mockClear()
    model.edit({ code: 'changed' })
    expect(changed).not.toHaveBeenCalled()
    model.setDraftListener(null)
    model.setValue('height', '2')
    expect(changed).not.toHaveBeenCalled()
  })

  it('Run is off while the host says a run is in progress', async () => {
    const { model } = await setup()
    const locked = renderToStaticMarkup(<WorkbenchView model={model} onRun={() => {}} runLocked />)
    expect(locked).toMatch(/<button[^>]*disabled=""[^>]*title="A run is in progress; wait for its result."[^>]*>Run</)
    expect(locked).toContain('A run is in progress; wait for its result.')
  })
})

describe('Run is never left waiting', () => {
  it('a draft edited while the model options load resets Run with a reason, and Run works again', async () => {
    const { api, model } = await setup()
    const pending = deferred<{ label: string; value: unknown }[]>()
    api.choices.mockReturnValueOnce(pending.promise)
    const level = param({ name: 'level_name', type: 'string', source: 'levels' })
    model.edit({ parameters: [level] })
    const onRun = vi.fn()
    const running = model.run(onRun)
    expect(model.getSnapshot().actions.run?.status).toBe('running')
    model.edit({ code: 'return 5;' })
    pending.resolve([{ label: 'Level 1', value: 'L1' }])
    await running
    expect(model.getSnapshot().actions.run).toEqual({ status: 'error', message: 'The draft changed while the run was being prepared. Review the values and run again.' })
    expect(html(model)).not.toContain('Requesting run...')
    expect(onRun).not.toHaveBeenCalled()

    await model.run(onRun)
    expect(model.getSnapshot().actions.run?.message).toBe('Fill a value for level_name.')
    model.setValue('level_name', 'L1')
    await model.run(onRun)
    expect(onRun).toHaveBeenCalledWith({ ...draft, code: 'return 5;', parameters: [level] }, { level_name: 'L1' })
    expect(model.getSnapshot().actions.run?.status).toBe('success')
  })

  it('a run whose draft is replaced while preparing leaves no busy Run behind', async () => {
    const { api, model } = await setup()
    const pending = deferred<{ label: string; value: unknown }[]>()
    api.choices.mockReturnValueOnce(pending.promise)
    model.edit({ parameters: [param({ name: 'level_name', type: 'string', source: 'levels' })] })
    const onRun = vi.fn()
    const running = model.run(onRun)
    model.newDraft()
    pending.resolve([{ label: 'Level 1', value: 'L1' }])
    await running
    expect(model.getSnapshot().actions.run).toBeUndefined()
    expect(onRun).not.toHaveBeenCalled()
  })

  it('a host that rejects the run shows its error and frees the button', async () => {
    const { model } = await setup()
    await model.run(async () => { throw new Error('Save the tool first') })
    expect(model.getSnapshot().actions.run).toEqual({ status: 'error', message: 'Save the tool first' })
    expect(html(model)).toMatch(/<button[^>]*class="btn-primary">Run</)
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
    expect(diagnosticRange('a\r\nbc', { ...location, column: 1 })).toEqual([2, 4])
    expect(diagnosticRange(draft.code, { ...location, in_wrapper: true })).toBeNull()
    expect(diagnosticRange(draft.code, { ...location, file: 'Helper.cs' })).toBeNull()
    expect(diagnosticRange(draft.code, { ...location, line: 99 })).toBeNull()
  })

  it('inserts Tab indentation and maintains selection offsets', () => {
    expect(indentSelection('return 1;', 0, 0)).toEqual({ code: '  return 1;', start: 2, end: 2 })
    expect(indentSelection('one\ntwo', 0, 7)).toEqual({ code: '  one\n  two', start: 2, end: 11 })
    expect(indentSelection('one\r\ntwo', 4, 4)).toEqual({ code: 'one\n  two', start: 6, end: 6 })
  })

  it.each(['\r\n', '\r'])('normalizes loaded and edited %j code before diagnostics, Tab and API calls', async newline => {
    const { api, model } = await setup()
    const loaded = { ...draft, code: ['first', 'second', 'third'].join(newline) }
    api.load.mockResolvedValue(loaded)
    await model.open(draft.name)
    const code = model.getSnapshot().draft.code
    expect(code).toBe('first\nsecond\nthird')
    expect(loaded.code).toBe(['first', 'second', 'third'].join(newline))
    expect(diagnosticRange(code, { line: 2, column: 3, in_wrapper: false })).toEqual([8, 12])
    const indented = indentSelection(code, 6, 6)
    expect(indented).toEqual({ code: 'first\n  second\nthird', start: 8, end: 8 })
    model.edit({ code: indented.code.replaceAll('\n', newline) })
    expect(model.getSnapshot().draft.code).toBe(indented.code)
    await model.check()
    expect(api.check).toHaveBeenCalledWith(indented.code, [])
    expect(diagnosticRange(model.getSnapshot().draft.code, { line: 2, column: 3, in_wrapper: false })).toEqual([8, 14])
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

describe('dry-run and check status lines', () => {
  it('a compile failure with an empty error still says what happened', async () => {
    const { api, model } = await setup()
    const diagnostic = { id: 'CS0103', severity: 'error', line: 1, column: 5, message: 'Name is missing', symbol: null, in_wrapper: false }
    api.dryRun.mockResolvedValue({ ...dryReply, success: false, rolled_back: true, error: '', diagnostics: [diagnostic] })
    await model.dryRun()
    expect(model.getSnapshot().actions.dryRun).toEqual({ status: 'error', message: 'Dry run failed. See diagnostics.' })
    api.check.mockResolvedValue({ ...checkReply, compiled: false, reason: '' })
    await model.check()
    expect(model.getSnapshot().actions.check?.message).toBe('Compilation failed. See diagnostics.')
  })

  it.each([
    { error: 'blocked', hint: null, warnings: ['System.IO.File'] },
    { error: 'no_dry_run', hint: 'This add-in cannot dry-run' },
    { error: 'include_not_found', hint: 'include takes paths' },
    { error: 'unreachable: connection refused', hint: null },
  ])('a refusal before anything reached Revit is not an unconfirmed rollback: $error', async refusal => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ...dryReply, success: false, rolled_back: null, result: null, changes: null, evidence_id: null, warnings: [], ...refusal })
    await model.dryRun()
    const page = html(model)
    expect(page).toContain(`Nothing was sent to Revit (${refusal.error})`)
    expect(page).not.toContain('Rollback is not confirmed')
    expect(model.getSnapshot().actions.dryRun).toEqual({ status: 'error', message: `Nothing was sent to Revit: ${refusal.error}.` })
    for (const warning of refusal.warnings ?? []) expect(page).toContain(warning)
    expect(saveDisabledReason(model.getSnapshot())).toContain('Dry-run')
  })

  it.each([
    { error: 'connection lost', hint: 'No dry-run reply came back, so the rollback is not confirmed', evidence_id: 'ev-1' },
    { error: 'timeout: no reply', hint: 'No dry-run reply came back', evidence_id: null, warnings: ['evidence not recorded: disk full'] },
    { error: 'dry_run_not_rolled_back', hint: 'The add-in did not report', evidence_id: 'ev-2', rolled_back: false },
  ])('a request that was sent without a confirmed rollback stays a warning: $error', async reply => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ...dryReply, success: false, rolled_back: null, warnings: [], ...reply })
    await model.dryRun()
    expect(html(model)).toContain('Rollback is not confirmed. Check the model before continuing.')
    expect(html(model)).not.toContain('Nothing was sent to Revit')
    expect(model.getSnapshot().actions.dryRun?.message).toBe(`${reply.error}. Rollback is not confirmed. Check the model before continuing.`)
  })

  it('keeps the note the add-in gives with its counts', async () => {
    const { api, model } = await setup()
    api.dryRun.mockResolvedValue({ ...dryReply, changes: { ...dryReply.changes, note: 'Counts exclude elements Revit regenerated.' } })
    await model.dryRun()
    expect(model.getSnapshot().dryRun?.changes?.note).toBe('Counts exclude elements Revit regenerated.')
    expect(html(model)).toContain('Counts exclude elements Revit regenerated.')
    expect(readDryRun({ ...dryReply, changes: { note: 42 } }).changes).toEqual({})
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
