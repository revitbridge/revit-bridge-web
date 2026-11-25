// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../../App'
import { loadRuntimeConfig } from '../../config'
import { cleanBrowserKey, llmHeaders, useSessionStore } from '../../store'

const endpoint = '/api/v1/settings/models'
const profile = { base_url: 'https://test.example/v1', model: 'test-model', key_set: true, key_tail: '1234' }
const details = { configured: true, saved: { shared: profile }, models: { planner: profile, analyst: profile, supervisor: profile } }

async function mount(path: string, mode = 'local', configured = true) {
  HTMLElement.prototype.scrollTo = vi.fn()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const calls = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/config.json') return Response.json({ features: { mode, byoModel: mode === 'cloud',
      serverModel: configured, models: { planner: 'test-model' } } })
    if (url === endpoint && mode === 'cloud') return Response.json({}, { status: 403 })
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
})

it('removes a legacy key once and suppresses all stale model headers', async () => {
  useSessionStore.getState().setLlm({ llmKey: 'fake-browser-only', llmModel: 'old-model' })
  localStorage.setItem('rb_llm_key', 'fake-legacy-only')
  const page = await mount('/')
  expect(page.node.querySelector('.tab-bar')?.textContent).toBe('ChatToolsWorkflowsSettings')
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
    expect(page.node.querySelector('.tab-bar')?.textContent).toBe('对话工具工作流设置')
    expect(page.node.querySelector('.runtime-chips')?.textContent).toContain('已连接')
    expect(page.node.textContent).toContain('当前使用的模型')
    await page.unmount()
  } finally { language.mockRestore() }
})
