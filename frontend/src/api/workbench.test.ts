/* api/workbench.ts against a mocked fetch: each WorkbenchApi method calls its route with the
   right body and returns what the workbench component expects. */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loadRuntimeConfig } from '../config'
import { useSessionStore } from '../store'
import { BridgeApiError } from '../components/shared/http'
import { WorkbenchModel } from '../components/hostv2/workbench/model'
import type { ToolDetail } from '../types/api'
import {
  choicesFromOf, defaultDisplayName, draftPack, draftParameter, packParameter, parseValidator, sampleValues, workbenchApi,
  type ToolParamDraft, type WorkbenchApi,
} from './workbench'

type Call = { url: string; init: RequestInit }

function stubFetch(...replies: Array<{ status?: number; body: unknown; type?: string }>) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init })
    const reply = replies.length > 1 ? replies.shift()! : replies[0]
    const type = reply.type ?? 'application/json'
    const text = typeof reply.body === 'string' && type !== 'application/json' ? reply.body : JSON.stringify(reply.body)
    return new Response(text, { status: reply.status ?? 200, headers: { 'content-type': type } })
  }))
  return calls
}

const body = (c: Call) => JSON.parse(String(c.init.body))

// GET /tools/{name} of a plain user tool; tests override what they need.
const detail = (): ToolDetail => ({
  name: 'mine', display_name: 'Mine', description: 'Mine', version: '1.0.0', code_template: 'return 1;', parameters: [],
  tags: [], execution_count: 0, source_query: '', preconditions: [], applies_when: [], not_for: [], validator: null,
})

const level: ToolParamDraft = { name: 'level_name', type: 'string', description: 'Level', source: 'levels', required: true }
const height: ToolParamDraft = { name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', required: false, default: 3000 }
const wallType: ToolParamDraft = { name: 'type_name', type: 'string', description: 'Type', source: 'family_types', category: 'OST_Walls', required: true }
const wall: ToolParamDraft = { name: 'wall_id', type: 'int', description: 'Wall', source: 'elements', category: 'OST_Walls', required: true }

beforeEach(async () => {
  stubFetch({ body: { apiBase: '', wsBase: '', features: { mode: 'cloud', byoModel: true, serverModel: false, admin: true, maxDevices: 20 } } })
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
  useSessionStore.setState({ adminToken: 'pw' })
})
afterEach(() => {
  vi.unstubAllGlobals()
  useSessionStore.setState({ adminToken: '' })
})

describe('workbenchApi', () => {
  it('satisfies the WorkbenchApi interface of the workbench component', () => {
    const api: WorkbenchApi = workbenchApi
    expect(Object.keys(api).sort()).toEqual(
      ['check', 'choices', 'dryRun', 'exportYaml', 'importYaml', 'list', 'load', 'lookup', 'remove', 'save'])
  })

  it('lookup asks GET /tools/{name}: what a save over it drops, null on 404 unknown_tool, any other refusal thrown', async () => {
    const calls = stubFetch(
      { body: { ...detail(), display_name: 'My tool', version: '1.2.0', preconditions: [{ kind: 'levels_min', value: 2 }], applies_when: ['a', 'b'], not_for: ['c'], tags: ['t'] } },
      { status: 404, body: { error: 'unknown_tool', message: "Tool 'nope' not found", tool: 'nope' } },
      { status: 422, body: { error: 'invalid_tool_name', message: 'bad name' } },
    )
    expect(await workbenchApi.lookup('mine')).toEqual({
      name: 'mine',
      dropped: ['display name "My tool"', 'version 1.2.0 (saved again as 1.0.0)', 'preconditions (1)', 'applies_when (2)', 'not_for (1)', 'tags (1)'],
      unseen: ['revit_versions', 'fixtures', 'approved_by', 'approved_at'],
    })
    expect(await workbenchApi.lookup('nope')).toBeNull()
    const err = await workbenchApi.lookup('con').catch((e: unknown) => e as BridgeApiError)
    expect(err.code).toBe('invalid_tool_name')
    expect(calls.map(c => [c.init.method ?? 'GET', c.url])).toEqual([
      ['GET', '/api/v1/bridge/tools/mine'], ['GET', '/api/v1/bridge/tools/nope'], ['GET', '/api/v1/bridge/tools/con'],
    ])
  })

  it('check sends a draft with parameters as pack + sample values, and returns the package reply as it is', async () => {
    const reply = { schema_version: 1, checked: true, compiled: false, diagnostics: [{ id: 'CS1061', line: 2 }] }
    const calls = stubFetch({ body: reply })
    expect(await workbenchApi.check('return {height} + wall.Lenght;', [level, height, wall])).toEqual(reply)
    expect(calls[0].url).toBe('/api/v1/bridge/check')
    expect(calls[0].init.method).toBe('POST')
    expect(body(calls[0])).toEqual({
      pack: draftPack('return {height} + wall.Lenght;', [level, height, wall]),
      values: { level_name: '', height: 3000, wall_id: 0 },
    })
  })

  it('check and dryRun send a draft without parameters as a pack too, so a leftover {x} is the package\'s refusal', async () => {
    const leftover = 'return $"{x}";'
    const refused = { error: 'render_failed', message: "The package could not fill the draft's template with these values",
      errors: ["Template still contains placeholder(s) ['x']: declare them as parameters or remove them from the code"] }
    const calls = stubFetch({ status: 422, body: refused })
    const checked = await workbenchApi.check(leftover, []).catch((e: unknown) => e as BridgeApiError)
    const dryRun = await workbenchApi.dryRun(leftover, [], {}).catch((e: unknown) => e as BridgeApiError)
    expect(calls.map(body)).toEqual([{ pack: draftPack(leftover, []), values: {} }, { pack: draftPack(leftover, []), values: {} }])
    for (const err of [checked, dryRun]) {
      expect(err.code).toBe('render_failed')
      expect(err.body?.errors).toEqual(refused.errors)
    }
  })

  it('dryRun sends the draft as pack with the designer values, no token', async () => {
    const reply = { success: true, rolled_back: true, changes: { modified: { count: 3 } } }
    const calls = stubFetch({ body: reply })
    expect(await workbenchApi.dryRun('code {height}', [level, height, wall], { height: 500, level_name: 'L1', wall_id: 7 })).toEqual(reply)
    expect(calls[0].url).toBe('/api/v1/bridge/dry-run')
    expect(body(calls[0])).toEqual({ pack: draftPack('code {height}', [level, height, wall]), values: { height: 500, level_name: 'L1', wall_id: 7 } })
    expect(body(calls[0])).not.toHaveProperty('token')
    expect(body(calls[0])).not.toHaveProperty('code')
  })

  it('a draft pack is the v1 pack format the server validates', () => {
    expect(draftPack('return {height};', [height])).toEqual({
      schema_version: 1, name: 'workbench_draft', version: '1.0.0', code_template: 'return {height};',
      parameters: [{ name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', default: 3000, required: false }],
    })
    expect(sampleValues([{ ...level }, { ...height, default: undefined }, { ...wall }, { name: 'b', type: 'bool', description: '', source: 'designer', required: true }]))
      .toEqual({ level_name: '', height: 0, wall_id: 0, b: false })
  })

  it('a refusal comes back as a BridgeApiError in the server words', async () => {
    stubFetch({ status: 503, body: { error: 'revit_unreachable', message: 'refused' } })
    const err = await workbenchApi.dryRun('code', [], {}).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(BridgeApiError)
    expect((err as BridgeApiError).message).toBe('503: revit_unreachable: refused')
  })

  it('choices asks /choices with the parameter choices_from and returns the server options as they are', async () => {
    const levels = [{ label: 'L1 (0.0mm)', value: 'L1' }, { label: 'L2 (3500.0mm)', value: 'L2' }]
    const calls = stubFetch({ body: levels }, { body: [{ label: 'Generic - 200mm', value: 'Generic - 200mm' }] },
      { body: [{ label: 'Wall (ID: 4242)', value: 4242 }] })
    expect(await workbenchApi.choices(level)).toEqual(levels)
    expect(await workbenchApi.choices(wallType)).toEqual([{ label: 'Generic - 200mm', value: 'Generic - 200mm' }])
    expect(await workbenchApi.choices(wall)).toEqual([{ label: 'Wall (ID: 4242)', value: 4242 }])
    expect(calls.map(body)).toEqual([
      { choices_from: 'levels' }, { choices_from: 'family_types:OST_Walls' }, { choices_from: 'elements:OST_Walls' },
    ])
    expect(calls.every(c => c.url === '/api/v1/bridge/choices')).toBe(true)
  })

  it('choices of a designer value asks nothing; a refusal is a BridgeApiError', async () => {
    const calls = stubFetch({ status: 503, body: { error: 'revit_unreachable', message: 'refused' } })
    expect(await workbenchApi.choices(height)).toEqual([])
    expect(calls).toHaveLength(0)
    await expect(workbenchApi.choices(level)).rejects.toThrow('503: revit_unreachable: refused')
  })

  it('save posts the draft as a v1 pack to /solidify with the admin token', async () => {
    const calls = stubFetch({ body: { status: 'solidified', name: 'raise_walls', display_name: 'Raise Walls', version: '1.0.0', revit_synced: false } })
    const saved = await workbenchApi.save({
      name: 'raise_walls', description: 'Raise the selected walls', code: 'return {height};',
      parameters: [level, height, wallType, wall], validator: '{"kind": "param_equals", "category": "OST_Walls", "checks": []}',
    })
    expect(saved).toEqual({ name: 'raise_walls' })
    expect(calls[0].url).toBe('/api/v1/bridge/solidify')
    expect(calls[0].init.headers).toMatchObject({ 'X-Admin-Token': 'pw' })
    expect(body(calls[0])).toEqual({
      name: 'raise_walls', code: 'return {height};', description: 'Raise the selected walls', source_query: '',
      validator: { kind: 'param_equals', category: 'OST_Walls', checks: [] },
      parameters: [
        { name: 'level_name', type: 'string', description: 'Level', source: 'tool:levels', choices_from: 'levels', required: true },
        { name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', default: 3000, required: false },
        { name: 'type_name', type: 'string', description: 'Type', source: 'tool:family_types', choices_from: 'family_types:OST_Walls', required: true },
        { name: 'wall_id', type: 'int', description: 'Wall', source: 'tool:elements', choices_from: 'elements:OST_Walls', required: true },
      ],
    })
  })

  it('save refuses a validator that is not a JSON object before anything is sent', async () => {
    const calls = stubFetch({ body: {} })
    await expect(workbenchApi.save({ name: 'x', description: '', code: 'return 1;', parameters: [], validator: 'created_ids' }))
      .rejects.toThrow('The validator must be a JSON object')
    expect(calls).toHaveLength(0)
  })

  it('list gives name, description, builtin, origin and the directory when the host names it', async () => {
    const calls = stubFetch({ body: [
      { name: 'create_wall', description: 'Wall', version: '1.0.0', parameters: [], preconditions: [], validator: null, used: 0, builtin: true, origin: 'builtin' },
      { name: 'mine', description: 'Mine', version: '1.0.0', parameters: [], preconditions: [], validator: null, used: 2, builtin: false, origin: 'user', directory: 'C:/x/capabilities' },
    ] })
    expect(await workbenchApi.list()).toEqual([
      { name: 'create_wall', description: 'Wall', builtin: true, origin: 'builtin' },
      { name: 'mine', description: 'Mine', builtin: false, origin: 'user', directory: 'C:/x/capabilities' },
    ])
    expect(calls[0].url).toBe('/api/v1/bridge/tools')
  })

  it('load reads a pack back as a draft, each parameter with the declaration it came from', async () => {
    const stored = [
      { name: 'level_name', type: 'string', source: 'tool:levels', choices_from: 'levels', required: true, description: 'Level' },
      { name: 'height', type: 'double', source: 'default', default: 3000, unit: 'mm', required: false, description: 'Height (mm)' },
      { name: 'floor_type', type: 'string', source: 'tool:floor_types', choices_from: 'floor_types', required: true },
      { name: 'count', type: 'integer', source: 'designer', required: true },
    ]
    const calls = stubFetch({ body: {
      name: 'create_wall', display_name: 'Create Wall', description: 'Wall', version: '1.0.0', code_template: 'return 1;',
      parameters: stored, source_query: 'a wall on a level',
      validator: { kind: 'count_delta', category: 'OST_Walls', expected: 1 },
    } })
    expect(await workbenchApi.load('create wall')).toEqual({
      name: 'create_wall', description: 'Wall', code: 'return 1;', validator: '{"kind":"count_delta","category":"OST_Walls","expected":1}',
      keep: { source_query: 'a wall on a level' },
      parameters: [
        { name: 'level_name', type: 'string', description: 'Level', source: 'levels', required: true, loaded: stored[0] },
        { name: 'height', type: 'double', description: 'Height (mm)', source: 'designer', unit: 'mm', required: false, default: 3000, loaded: stored[1] },
        { name: 'floor_type', type: 'string', description: '', source: 'family_types', category: 'OST_Floors', required: true, loaded: stored[2] },
        { name: 'count', type: 'int', description: '', source: 'designer', required: true, loaded: stored[3] },
      ],
    })
    expect(calls[0].url).toBe('/api/v1/bridge/tools/create%20wall')
  })

  it('an opened tool saves its parameters as declared while the designer leaves their source as shown', () => {
    const stored = [
      { name: 'level_name', type: 'string', description: 'Level', source: 'answer', choices_from: 'levels', required: true },
      { name: 'height', type: 'number', description: 'Height', source: 'default', default: 3000, unit: 'mm', required: false },
      { name: 'floor', type: 'string', description: 'Floor', source: 'tool:floor_types', choices_from: 'floor_types', required: true },
      { name: 'count', type: 'integer', description: 'Count', source: 'designer', required: true },
      { name: 'note', type: 'string', description: 'Note', source: 'designer', default: null, required: false },
    ]
    for (const p of stored) expect(packParameter(draftParameter(p))).toEqual(p)
    const floor = draftParameter(stored[2])
    expect(choicesFromOf(floor)).toBe('floor_types')
    // The designer changes the source or category: the workbench's own mapping.
    expect(packParameter({ ...floor, category: 'OST_Roofs' })).toMatchObject({ source: 'tool:family_types', choices_from: 'family_types:OST_Roofs' })
    expect(packParameter({ ...draftParameter(stored[0]), source: 'designer' })).toMatchObject({ source: 'designer' })
    expect(packParameter({ ...draftParameter(stored[0]), source: 'designer' })).not.toHaveProperty('choices_from')
    // A removed default cannot stay source default.
    expect(packParameter({ ...draftParameter(stored[1]), default: undefined, required: true })).toMatchObject({ source: 'designer', required: true })
    expect(packParameter({ ...draftParameter(stored[3]), type: 'double' }).type).toBe('double')
  })

  it('open, edit the description, save: what /solidify carries survives, what it cannot is named first', async () => {
    const saved = {
      ...detail(), name: 'raise_floors', display_name: 'Raise Floors', description: 'Old text', code_template: 'return "{floor}";',
      source_query: 'raise the floors', preconditions: [{ kind: 'levels_min', value: 2 }], applies_when: ['raise floors'],
      validator: { kind: 'count_delta', category: 'OST_Floors', expected: 0 },
      parameters: [{ name: 'floor', type: 'string', description: 'Floor type', source: 'tool:floor_types', choices_from: 'floor_types', required: true }],
    }
    const calls: Call[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
      calls.push({ url, init })
      const reply = url.endsWith('/tools') ? [{ name: 'raise_floors', description: 'Old text', builtin: false, origin: 'user' }]
        : url.endsWith('/tools/raise_floors') ? saved
        : url.endsWith('/choices') ? [{ label: 'Generic 150', value: 'Generic 150' }]
        : url.endsWith('/dry-run') ? { success: true, rolled_back: true, result: null, changes: null, diagnostics: [], warnings: [] }
        : { status: 'solidified', name: 'raise_floors', display_name: 'Raise Floors', version: '1.0.0', revit_synced: false }
      return new Response(JSON.stringify(reply), { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    const model = new WorkbenchModel(workbenchApi)
    await model.refresh()
    await model.open('raise_floors')
    model.edit({ description: 'New text' })
    model.setValue('floor', 'Generic 150')
    await model.dryRun()
    await model.save()
    expect(model.getSnapshot().replace).toMatchObject({ step: 'replace', dropped: ['preconditions (1)', 'applies_when (1)'] })
    await model.confirmReplace()
    expect(model.getSnapshot().replace?.step).toBe('drop')
    expect(calls.some(c => c.url.endsWith('/solidify'))).toBe(false)
    await model.confirmReplace()
    const choices = calls.filter(c => c.url.endsWith('/choices')).map(body)
    expect(choices).toEqual([{ choices_from: 'floor_types' }])
    const solidify = calls.find(c => c.url.endsWith('/solidify'))!
    expect(body(solidify)).toEqual({
      name: 'raise_floors', code: 'return "{floor}";', description: 'New text', source_query: 'raise the floors',
      validator: { kind: 'count_delta', category: 'OST_Floors', expected: 0 },
      parameters: [{ name: 'floor', type: 'string', description: 'Floor type', source: 'tool:floor_types', choices_from: 'floor_types', required: true }],
    })
    expect(model.getSnapshot().actions.save?.status).toBe('success')
  })

  it("the package's display name for a pack name (str.title)", () => {
    expect(['raise_walls', 'make_2d_plan', 'ABC_def'].map(defaultDisplayName)).toEqual(['Raise Walls', 'Make 2D Plan', 'Abc Def'])
  })

  it('remove deletes the pack', async () => {
    const calls = stubFetch({ body: { status: 'deleted', name: 'mine' } })
    expect(await workbenchApi.remove('mine')).toBeUndefined()
    expect([calls[0].init.method, calls[0].url]).toEqual(['DELETE', '/api/v1/bridge/tools/mine'])
    expect(calls[0].init.headers).toMatchObject({ 'X-Admin-Token': 'pw' })
  })

  it('exportYaml returns the file as text; a 404 is a BridgeApiError', async () => {
    const yaml = 'schema_version: 1\nname: mine\n'
    const calls = stubFetch({ body: yaml, type: 'text/yaml; charset=utf-8' }, { status: 404, body: { error: 'unknown_tool', message: "Tool 'nope' not found" } })
    expect(await workbenchApi.exportYaml('mine')).toBe(yaml)
    expect(calls[0].url).toBe('/api/v1/bridge/tools/mine/export')
    const err = await workbenchApi.exportYaml('nope').catch((e: unknown) => e as BridgeApiError)
    expect(err.code).toBe('unknown_tool')
  })

  it('importYaml posts the text as text/yaml; overwrite only when asked', async () => {
    const yaml = 'schema_version: 1\nname: mine\nversion: 1.0.0\ncode_template: return 1;\n'
    const calls = stubFetch(
      { body: { status: 'imported', name: 'mine', version: '1.0.0', replaced: false, revit_synced: false } },
      { body: { status: 'imported', name: 'mine', version: '1.0.0', replaced: true, revit_synced: false } },
    )
    expect((await workbenchApi.importYaml(yaml)).name).toBe('mine')
    expect((await workbenchApi.importYaml(yaml, { overwrite: true })).replaced).toBe(true)
    expect(calls.map(c => c.url)).toEqual(['/api/v1/bridge/tools/import', '/api/v1/bridge/tools/import?overwrite=true'])
    expect(calls[0].init.body).toBe(yaml)
    expect(calls[0].init.headers).toMatchObject({ 'Content-Type': 'text/yaml', 'X-Admin-Token': 'pw' })
  })
})

describe('the mapping helpers', () => {
  it('packParameter and draftParameter round-trip a draft', () => {
    for (const p of [level, height, wallType, wall]) {
      const pack = packParameter(p)
      const back = draftParameter({ ...pack, unit: pack.unit ?? undefined })
      expect(back.loaded).toEqual({ ...pack, unit: pack.unit ?? undefined })
      expect({ ...back, loaded: undefined }).toEqual({ ...p, loaded: undefined })
      expect(packParameter(back)).toEqual(pack)
    }
  })

  it('choicesFromOf and parseValidator', () => {
    expect([level, height, wallType, wall].map(choicesFromOf)).toEqual(['levels', null, 'family_types:OST_Walls', 'elements:OST_Walls'])
    expect(parseValidator('  ')).toBeNull()
    expect(parseValidator(undefined)).toBeNull()
    expect(() => parseValidator('[1]')).toThrow('JSON object')
  })
})
