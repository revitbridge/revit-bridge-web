// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import AuthoringPage from './AuthoringPage'
import { bridgeApi } from '../../api/bridge'
import * as http from '../shared/http'
import { AUTHORING_PATH, pathOf, viewOf } from '../../route'

const box = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  document.body.append(box); root = createRoot(box)
  vi.spyOn(bridgeApi, 'listTools').mockResolvedValue([])
})
afterEach(() => { act(() => root.unmount()); box.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const button = (name: string) => [...box.querySelectorAll('button')].find(b => b.textContent === name)!
const press = async (name: string) => { await act(async () => button(name).click()) }
const input = (selector: string, value: string) => act(() => {
  const el = box.querySelector<HTMLTextAreaElement>(selector)!
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
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
  await act(async () => root.render(<AuthoringPage />))
  input('textarea', 'My description'); await press('Generate draft')
  expect(button('Save to tools').disabled).toBe(true)
  input('.authoring-code', 'wrong'); await press('Compile check')
  expect(button('Dry run and roll back').disabled).toBe(true)
  input('.authoring-code', 'correct'); await press('Compile check'); await press('Dry run and roll back')
  expect(button('Save to tools').disabled).toBe(true)
  rolledBack = true
  await press('Dry run and roll back')
  expect(button('Save to tools').disabled).toBe(false)
  input('label:has(.authoring-code) textarea', 'correct ')
  expect(button('Save to tools').disabled).toBe(true)
  input('.authoring-code', 'correct'); await press('Compile check'); await press('Dry run and roll back'); await press('Save to tools')
  expect(posted.mock.calls.filter(([url]) => url.endsWith('/solidify'))).toHaveLength(1)
  expect(box.textContent).toContain('Saved to tools')
  expect(pathOf(viewOf(AUTHORING_PATH))).toBe(AUTHORING_PATH)
})
