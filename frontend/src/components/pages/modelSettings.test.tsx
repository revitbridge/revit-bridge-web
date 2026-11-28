// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../../App'
import { loadRuntimeConfig } from '../../config'
import { cleanBrowserKey, llmHeaders, useSessionStore } from '../../store'
import ModelSettings from './ModelSettings'
import type { LibraryModel, ModelDetails } from '../connect/modelLibrary'

const endpoint = '/api/v1/settings/models'
const profile = { base_url: 'https://test.example/v1', model: 'test-model', key_set: true, key_tail: '1234' }
const details = { configured: true, saved: { shared: profile }, models: { planner: profile, analyst: profile, supervisor: profile } }

async function mount(path: string, mode = 'local', configured = true, storeUnavailable = false) {
  HTMLElement.prototype.scrollTo = vi.fn()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const calls = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/config.json') return Response.json({ features: { mode, byoModel: mode === 'cloud',
      serverModel: configured, models: { planner: 'test-model' } } })
    if (url === endpoint && mode === 'cloud') return Response.json({}, { status: 403 })
    if (url === endpoint && storeUnavailable) return Response.json({ detail: 'fake-private-diagnostic' }, { status: 503 })
    if (url === endpoint) return Response.json(details)
    if (url.includes('health')) return Response.json({ revit_connected: true })
    if (url.includes('snapshot')) return Response.json({}, { status: 503 })
    if (init?.method === 'PUT') throw Error('unexpected endpoint')
    return Response.json([])
  })
  vi.stubGlobal('fetch', calls)
  await loadRuntimeConfig()
  const node = document.createElement('div')
  document.body.append(node)
  const root = createRoot(node)
  await act(async () => root.render(<App path={path} />))
  return { node, calls, unmount: () => act(() => root.unmount()) }
}
afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
  localStorage.clear()
  useSessionStore.setState(useSessionStore.getInitialState())
})

it('adds and edits a saved library entry, assigns a role, then deletes it', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const library: LibraryModel[] = [{ ...profile, name: 'Host', source: 'file', api: 'chat', vision: true, shadowed: false }]
  const selection = { entry: null, selected_missing: false }
  const state: ModelDetails = { ...details, library, models: { ...details.models, optimizer: profile },
    selection: { analyst: selection, planner: selection, supervisor: selection, optimizer: selection } }
  const calls = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/config.json') return Response.json({ features: { mode: 'local' } })
    if (url === endpoint) return Response.json(state)
    if (url === endpoint + '/library/Studio') {
      const index = library.findIndex(e => e.name === 'Studio')
      if (init?.method === 'DELETE') {
        library.splice(index, 1)
        state.selection!.analyst = selection
      } else {
        const { model, base_url, api, vision } = JSON.parse(String(init?.body))
        const entry = { ...profile, name: 'Studio', source: 'saved' as const, model, base_url, api, vision, shadowed: false }
        library.splice(index < 0 ? library.length : index, index < 0 ? 0 : 1, entry)
      }
    } else if (url === endpoint + '/selection') {
      state.selection!.analyst = { entry: 'Studio', selected_missing: false }
    } else throw Error('Unexpected request')
    return Response.json(state)
  })
  vi.stubGlobal('fetch', calls)
  await loadRuntimeConfig()
  const node = document.createElement('div')
  document.body.append(node)
  const root = createRoot(node)
  await act(async () => root.render(<ModelSettings />))
  const section = node.querySelector<HTMLElement>('.model-library')!
  const inputs = [...section.querySelectorAll<HTMLInputElement>('.library-fields input')]
  const enter = (input: HTMLInputElement, value: string) => act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  async function click(label: string) {
    await act(async () => [...section.querySelectorAll('button')].find(b => b.textContent === label)!.click())
  }
  expect(section.textContent).toContain('models file')
  expect(section.querySelector('.library-entries button')).toBeNull()
  expect(node.querySelector('details summary')?.textContent).toBe('Advanced: one saved group per role')
  enter(inputs[0], 'Studio')
  enter(inputs[1], 'https://studio.example/v1')
  enter(inputs[2], 'sketch-model')
  act(() => inputs[3].click())
  enter(inputs[4], 'fixture-library-key')
  const api = section.querySelector('form select') as HTMLSelectElement
  act(() => { api.value = 'responses'; api.dispatchEvent(new Event('change', { bubbles: true })) })
  await click('Add entry')
  expect(JSON.parse(String(calls.mock.calls.at(-1)![1]?.body))).toEqual({ api: 'responses', vision: true,
    api_key: 'fixture-library-key', model: 'sketch-model', base_url: 'https://studio.example/v1' })
  expect(section.querySelectorAll('li')).toHaveLength(2)
  await click('Edit')
  expect(inputs[0].disabled).toBe(true)
  expect(inputs[4].value).toBe('')
  enter(inputs[2], 'revised')
  await click('Save entry')
  expect(JSON.parse(String(calls.mock.calls.at(-1)![1]?.body))).not.toHaveProperty('api_key')
  expect(section.textContent).toContain('revised')
  const analyst = section.querySelectorAll('select')[3]
  await act(async () => { analyst.value = 'Studio'; analyst.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(JSON.parse(String(calls.mock.calls.at(-1)![1]?.body))).toEqual({ role: 'analyst', entry: 'Studio' })
  await click('Delete')
  expect(calls.mock.calls.at(-1)![1]?.method).toBe('DELETE')
  expect(section.querySelectorAll('li')).toHaveLength(1)
  expect(analyst.value).toBe('')
  act(() => root.unmount())
  node.remove()
})

it('removes a legacy key once and suppresses all stale model headers', async () => {
  useSessionStore.getState().setLlm({ llmKey: 'fake-browser-only', llmModel: 'old-model' })
  localStorage.setItem('rb_llm_key', 'fake-legacy-only')
  const page = await mount('/')
  expect(page.node.querySelector('.runtime-chips')?.textContent).toContain('test-model')
  expect(page.node.querySelector('input[type=password]')).toBeNull()
  expect(llmHeaders()).toEqual({})
  act(() => cleanBrowserKey())
  expect(sessionStorage.getItem('rb_llm_key')).toBeNull()
  expect(localStorage.getItem('rb_llm_key')).toBeNull()
  expect(cleanBrowserKey()).toBe(false)
  expect(page.node.textContent).toContain('removed')
  await page.unmount()
})

it('preserves legacy keys while the local host is unconfigured and suppresses their headers', async () => {
  useSessionStore.getState().setLlm({ llmKey: 'fake-tab-key' })
  localStorage.setItem('rb_llm_key', 'fake-legacy-key')
  const page = await mount('/', 'local', false)
  expect(sessionStorage.getItem('rb_llm_key')).toBe('fake-tab-key')
  expect(localStorage.getItem('rb_llm_key')).toBe('fake-legacy-key')
  expect(llmHeaders()).toEqual({})
  expect(page.node.textContent).not.toContain('removed')
  await page.unmount()
})

it('opens the ledger under Evidence in the navigation and at its own address', async () => {
  const page = await mount('/')
  const tabs = [...page.node.querySelectorAll<HTMLButtonElement>('.tab-bar button')]
  expect(tabs.map(b => b.textContent)).toEqual(['Chat', 'Tools', 'Workflows', 'Evidence', 'Settings'])
  await act(async () => tabs.find(b => b.textContent === 'Evidence')!.click())
  expect(window.location.pathname).toBe('/evidence')
  expect(page.node.querySelector('main h3')?.textContent).toBe('Evidence')
  await page.unmount()
  const linked = await mount('/evidence')
  expect(linked.node.querySelector('.tab-bar .is-active')?.textContent).toBe('Evidence')
  await linked.unmount()
})

it('reports an unreadable settings store without displaying its diagnostic', async () => {
  const page = await mount('/settings', 'local', true, true)
  expect(page.node.textContent).toContain('Could not read saved model settings')
  expect(page.node.textContent).not.toContain('fake-private-diagnostic')
  await page.unmount()
})

it.each(['local', 'cloud'])('keeps the %s API key input blank and outside password-manager forms', async mode => {
  useSessionStore.getState().setLlm({ llmKey: 'fake-tab-key' })
  const page = await mount('/settings', mode, mode === 'local')
  const label = [...page.node.querySelectorAll('label')].find(l => l.textContent === 'API key')!
  const input = label.querySelector('input')!
  expect(input.value).toBe('')
  expect(input.type).toBe('password')
  expect(input.autocomplete).toBe('off')
  expect(input.form).toBeNull()
  expect(input.closest('form')).toBeNull()
  expect(input.hasAttribute('data-1p-ignore')).toBe(true)
  expect(input.getAttribute('data-lpignore')).toBe('true')
  await page.unmount()
})

it('saves a shared profile without sending the displayed tail back as a key', async () => {
  const page = await mount('/settings')
  expect(page.node.textContent).toContain('1234')
  const password = page.node.querySelector<HTMLInputElement>('input[type=password]')!
  expect(password.value).toBe('')
  const save = [...page.node.querySelectorAll('button')].find(b => b.textContent === 'Save')!
  await act(async () => save.click())
  const request = page.calls.mock.calls.find(([url, init]) => url === endpoint && init?.method === 'PUT')
  expect(JSON.parse(String(request?.[1]?.body))).toEqual({ role: 'shared', base_url: profile.base_url, model: profile.model })
  await page.unmount()
})

it('offers the guest form only on an unconfigured host that allows browser keys', async () => {
  const guest = await mount('/settings', 'cloud', false)
  expect(guest.node.textContent).toContain('Bring your own model')
  expect(guest.node.textContent).not.toContain('1234')
  await guest.unmount()
  const configured = await mount('/settings', 'cloud', true)
  expect(configured.node.textContent).not.toContain('Bring your own model')
  await configured.unmount()
})

it('shows Chinese navigation, connection status and model settings', async () => {
  const language = vi.spyOn(navigator, 'languages', 'get').mockReturnValue(['zh-CN'])
  try {
    const page = await mount('/settings')
    expect(page.node.querySelector('.tab-bar')?.textContent).toBe('对话工具工作流证据设置')
    expect(page.node.querySelector('.runtime-chips')?.textContent).toContain('已连接')
    expect(page.node.textContent).toContain('当前使用的模型')
    await page.unmount()
  } finally { language.mockRestore() }
})
