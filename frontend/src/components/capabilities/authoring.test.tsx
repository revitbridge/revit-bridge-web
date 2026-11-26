// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AuthoringPage from './AuthoringPage'
import { bridgeApi } from '../../api/bridge'
import { BridgeApiError } from '../../api/client'
import * as http from '../shared/http'
import { AUTHORING_PATH, pathOf, viewOf } from '../../route'

const box = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  document.body.append(box); root = createRoot(box)
  vi.spyOn(bridgeApi, 'listTools').mockResolvedValue([])
  vi.spyOn(http, 'get').mockResolvedValue({ trials: [] })
})

it('loads a selected remote rollback into an editable draft that still needs a compile and trial', async () => {
  vi.mocked(http.get).mockImplementation(async url => url.endsWith('/remote-trials')
    ? { trials: [{ time: '2026-11-25T00:00:00Z', code_sha256: 'abcd1234', evidence_id: 'ev_test', rolled_back: true }] }
    : { code: 'return 73;' })
  await act(async () => root.render(<AuthoringPage connected />))
  expect(box.textContent).toContain('abcd1234')
  await press('Turn into a tool')
  expect(box.querySelector<HTMLTextAreaElement>('.authoring-code')?.value).toBe('return 73;')
  expect(button('Dry run and roll back').disabled).toBe(true)
  expect(button('Add to tools').disabled).toBe(true)
})
afterEach(() => { act(() => root.unmount()); box.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const button = (name: string) => [...box.querySelectorAll('button')].find(b => b.textContent === name)!
const press = async (name: string) => { await act(async () => button(name).click()) }
const input = (selector: string, value: string) => act(() => {
  const el = box.querySelector<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(selector)!
  Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value')!.set!.call(el, value)
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }))
})
it('enables saving only for the checked, successfully rolled-back draft and values', async () => {
  const pack = { schema_version: 1, version: '1.0.0', name: 'test_tool', description: 'Test', parameters: [], code_template: 'initial' }
  let rolledBack = false
  const posted = vi.spyOn(http, 'post').mockImplementation(async (url, data) => {
    const body = data as { pack: typeof pack }
    if (url.endsWith('/draft')) return { pack, problems: [] }
    if (url.endsWith('/check')) return { checked: true, compiled: body.pack.code_template === 'correct', diagnostics: [] }
    if (url.endsWith('/dry-run')) return { success: true, rolled_back: rolledBack }
    if (url.endsWith('/solidify')) return { name: 'test_tool' }
    throw Error(url)
  })
  await act(async () => root.render(<AuthoringPage connected />))
  input('textarea', 'My description'); await press('Generate draft')
  expect(button('Add to tools').disabled).toBe(true)
  input('.authoring-code', 'wrong'); await press('Compile check')
  expect(button('Dry run and roll back').disabled).toBe(true)
  input('.authoring-code', 'correct'); await press('Compile check'); await press('Dry run and roll back')
  expect(button('Add to tools').disabled).toBe(true)
  rolledBack = true
  await press('Dry run and roll back')
  expect(button('Add to tools').disabled).toBe(false)
  input('label:has(.authoring-code) textarea', 'correct ')
  expect(button('Add to tools').disabled).toBe(true)
  input('.authoring-code', 'correct'); await press('Compile check'); await press('Dry run and roll back'); await press('Add to tools')
  expect(posted.mock.calls.filter(([url]) => url.endsWith('/solidify'))).toHaveLength(1)
  expect(box.textContent).toContain('Saved to tools')
  expect(box.querySelector('a[href="/tools?tool=test_tool"]')?.textContent).toContain('test_tool')
  expect(box.querySelector('a[href="/?tool=test_tool"]')?.textContent).toBe('Use it in chat')
  expect(pathOf(viewOf(AUTHORING_PATH))).toBe(AUTHORING_PATH)
})

const draftPack = { schema_version: 1, version: '1.0.0', name: 'sample_view', description: 'Create a view',
  code_template: 'var view = doc.ActiveView;\nreturn view;', parameters: [
    { name: 'view_name', type: 'string', description: 'View name', source: 'default', required: false, default: 'Perspective' },
  ] }
const draftReply = { pack: draftPack, reference: 'create_level', repair_rounds: 2, problems: [], diagnostics: [], compiled: null }
const stepStates = () => [...box.querySelectorAll('.steps-indicator li')].map(el => el.getAttribute('data-state'))
const field = (path: string, tag = 'input') => `[data-field="${path}"] ${tag}`

it('lists every server problem and attaches validator and dotted paths to the corresponding input', async () => {
  const problems = ['name: choose a name', 'description: must be a string', 'parameters[0] (view_name): unknown type text',
    'parameters[0].source: missing', 'code_template: undeclared placeholder', 'unknown banner']
  const post = vi.spyOn(http, 'post').mockResolvedValueOnce(draftReply)
    .mockRejectedValueOnce(new BridgeApiError(422, JSON.stringify({ error: 'invalid_pack', problems })))
  await act(async () => root.render(<AuthoringPage brief="创建三维视图" connected />))
  await press('Generate draft'); await press('Compile check')
  expect([...box.querySelectorAll('[role=alert] li')].map(li => li.textContent)).toEqual(problems)
  for (const [i, path] of ['name','description','parameters[0].type','parameters[0].source','code_template'].entries()) {
    expect(box.querySelector(field(path, 'small'))?.textContent).toBe(problems[i])
  }
  expect(box.textContent).toContain('reference: create_level')
  expect(box.textContent).toContain('Repair rounds: 2')
  expect(post).toHaveBeenCalledTimes(2)
})

it('edits and reorders parameters, round-trips JSON, removes unsupported fields and uses typed defaults', async () => {
  const post = vi.spyOn(http, 'post').mockResolvedValueOnce({ ...draftReply, pack: { ...draftPack, display_name: 'Custom', tags: ['view'] } })
    .mockResolvedValueOnce({ checked: true, compiled: true, diagnostics: [] })
  await act(async () => root.render(<AuthoringPage brief="view" connected />))
  await press('Generate draft')
  expect(box.querySelector('[role=note]')?.textContent).toContain('display_name, tags')
  expect(button('Compile check').disabled).toBe(true)
  await press('Remove listed fields'); await press('Add parameter')
  input(field('parameters[1].name'), 'length')
  input(field('parameters[1].type','select'), 'double')
  input(field('parameters[1].unit','select'), 'mm')
  input(field('parameters[1].default'), '1200')
  input(field('parameters[1].description'), 'Offset')
  input(field('parameters[1].source','select'), 'default')
  await act(async () => box.querySelectorAll<HTMLButtonElement>('[aria-label="Move up"]')[1].click())
  await press('Edit as JSON')
  const json = box.querySelector<HTMLTextAreaElement>('.authoring-json')!.value
  const pack = JSON.parse(json)
  expect(pack.parameters[0]).toEqual({ name: 'length', type: 'double', unit: 'mm', default: 1200, required: false, source: 'default', description: 'Offset' })
  expect(pack.parameters[1]).toEqual(draftPack.parameters[0])
  expect(pack).not.toHaveProperty('display_name'); expect(pack).not.toHaveProperty('tags')
  input('.authoring-json', '{')
  expect(box.querySelector('[aria-invalid=true]')).toBeTruthy()
  expect(button('Compile check').disabled).toBe(true)
  input('.authoring-json', JSON.stringify({ ...pack, name: 'round_trip' }))
  await press('Edit as JSON')
  expect(box.querySelector<HTMLInputElement>(field('name'))!.value).toBe('round_trip')
  expect(box.querySelector<HTMLInputElement>('[aria-label="Trial value for length"]')!.value).toBe('1200')
  await press('Remove'); await press('Compile check')
  expect(post.mock.calls.at(-1)?.[1]).toMatchObject({ pack: { parameters: draftPack.parameters }, values: { view_name: 'Perspective' } })
  input('[aria-label="Trial value for view_name"]', '')
  post.mockResolvedValueOnce({ checked: true, compiled: true }).mockResolvedValueOnce({ success: true, rolled_back: true })
  await press('Compile check'); await press('Dry run and roll back')
  expect(post.mock.calls.at(-1)?.[1]).toMatchObject({ values: { view_name: '' } })
})

it('gates on connectivity, locates compiler messages, and invalidates completed steps on a value edit', async () => {
  const code = 'var view = document.ActiveView;\nreturn "{view_name}" + doc.Title;'
  const line = code.split('\n')[1]
  const compilerColumn = line.replace('{view_name}', '3D').indexOf('doc') + 1
  const diagnostic = { id: 'CS0103', severity: 'error', line: 2, column: compilerColumn,
    template_column: line.indexOf('doc') + 1, message: "Unknown name 'doc'" }
  vi.spyOn(http, 'post').mockResolvedValueOnce({ ...draftReply, pack: { ...draftPack, code_template: code,
    parameters: [{ ...draftPack.parameters[0], default: '3D' }] } })
    .mockResolvedValueOnce({ checked: true, compiled: false, diagnostics: [diagnostic] })
    .mockResolvedValueOnce({ checked: true, compiled: true })
    .mockResolvedValueOnce({ success: false, rolled_back: true, error: 'REFUSED: Choose a unique view name.' })
    .mockResolvedValueOnce({ success: true, rolled_back: true })
    .mockRejectedValueOnce(new BridgeApiError(422, JSON.stringify({ error: 'invalid_pack', problems: ['description: cannot save'] })))
  await act(async () => root.render(<AuthoringPage brief="view" connected={false} />))
  expect(stepStates()).toEqual(['current', 'pending', 'pending'])
  await press('Generate draft')
  expect(box.textContent).toContain('Revit is not connected.')
  expect(button('Compile check').disabled).toBe(true)
  await act(async () => root.render(<AuthoringPage connected />))
  await press('Compile check'); await press('Line 2, column ' + compilerColumn)
  const editor = box.querySelector<HTMLTextAreaElement>('.authoring-code')!
  expect(document.activeElement).toBe(editor)
  expect(editor.selectionStart).toBe(editor.value.indexOf('doc.Title'))
  await press('Compile check')
  expect(stepStates()).toEqual(['done','done','current'])
  await press('Dry run and roll back')
  expect(box.querySelector('[role=alert]')?.textContent).toContain('REFUSED: Choose a unique view name.')
  expect(button('Add to tools').disabled).toBe(true)
  await press('Dry run and roll back')
  expect(stepStates()).toEqual(['done','done','done'])
  await press('Add to tools')
  expect(box.querySelector(field('description', 'small'))?.textContent).toBe('description: cannot save')
  input('[aria-label="Trial value for view_name"]','Perspective 2')
  expect(stepStates()).toEqual(['done','current','pending'])
  expect(button('Add to tools').disabled).toBe(true)
})
