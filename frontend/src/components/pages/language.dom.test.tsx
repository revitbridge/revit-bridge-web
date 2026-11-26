// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../../App'
import { apiFetch } from '../../api/client'
import { loadRuntimeConfig } from '../../config'
import { browserLanguage } from '../../utils/language'
import ChatPanel from '../task/ChatPanel'
import { TokenPanel } from '../hostv2/trace'
import { ExecutionMode } from '../task/ExecutionMode'
import { useExecutionMode } from '../task/executionPreference'

afterEach(() => { localStorage.clear(); vi.unstubAllGlobals() })
async function configure(brief?: string) {
  const fetch = vi.fn(async (url: string, options?: RequestInit) => {
    if (url === '/api/chat' && options?.method === 'POST' && brief) {
      const form = { request: brief, question: 'Add a tool?', choices: { authoring: 'Create tool', remote_mcp: 'Use Remote MCP', dismiss: 'Not now' } }
      return new Response('event: missing_tool\ndata: ' + JSON.stringify(form) + '\n\ndata: [DONE]\n\n',
        { headers: {'Content-Type': 'text/event-stream', 'X-Session-Id': 'trial'} })
    }
    return new Response(JSON.stringify(url === '/config.json'
    ? { features: { mode: 'local', authoring: true, serverModel: true, models: {planner: 'fixture-model'},
        chatModes: ['auto', 'single', 'roles', 'roles_analyst'], chatModeDefault: 'roles', runModes: ['confirm', 'auto'] } }
    : url.endsWith('/snapshot') ? { document: { title: '样例', revit_version: '2026' }, units: {}, levels: [], grids: {count: 0, names: []}, family_types: [], selection_count: 0, warnings: [] }
    : url.endsWith('/revit-health') ? { revit_connected: false } : url.endsWith('/remote-trials') ? {trials: []}
    : url.endsWith('/settings/remote-mcp') ? {enabled: false, calls: [], tokens: []} : []), {headers: {'Content-Type': 'application/json'}})
  })
  vi.stubGlobal('fetch', fetch)
  await loadRuntimeConfig()
  return fetch
}
it('switches the visible page, remembers the preference and sends its language to the host', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('navigator', { languages: ['zh-CN'] })
  const fetch = await configure()
  const page = document.createElement('div')
  const app = createRoot(page)
  HTMLElement.prototype.scrollTo = vi.fn()
  try {
    await act(async () => app.render(<App />))
    expect(page.textContent).toContain('发送')
    await act(async () => page.querySelector<HTMLButtonElement>('[aria-label="切换到英语"]')!.click())
    expect(page.textContent).toContain('Send')
    expect(localStorage.getItem('rb_language')).toBe('en')
    expect(browserLanguage()).toBe('en')
    await apiFetch('/fake')
    expect(fetch).toHaveBeenLastCalledWith('/fake', expect.objectContaining({headers: expect.objectContaining({'Accept-Language': 'en'})}))
  } finally { act(() => app.unmount()) }
})

it.each(['/tools?tool=sample_view', '/?tool=sample_view'])('opens a saved tool destination without sending or executing: %s', async path => {
  localStorage.setItem('rb_language', 'en')
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const fetch = await configure()
  const page = document.createElement('div')
  const app = createRoot(page)
  HTMLElement.prototype.scrollTo = vi.fn()
  try {
    await act(async () => app.render(<App path={path} />))
    if (path.startsWith('/tools')) expect(page.querySelector('.chip.info')?.textContent).toBe('sample_view')
    else expect(page.querySelector('textarea')?.value).toBe('Use the sample_view tool.')
    expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
  } finally { act(() => app.unmount()) }
})

it('renders Chinese labels across the main pages and composer states', async () => {
  localStorage.setItem('rb_language', 'zh')
  await configure()
  useExecutionMode.getState().set(true)
  const allowed = new Set(['Revit', 'revit-bridge', 'fixture-model', 'Codex CLI', 'Claude Code · .mcp.json', 'ms', 'mm'])
  const failures: string[] = []
  const views = ['/', '/tools', '/capabilities/authoring', '/capabilities/workbench', '/workflows', '/settings',
    '/settings/connection', '/settings/remote-mcp', '/token', '/evidence'].map(path => [path, <App path={path} />] as const)
  const populated = <ChatPanel title="对话" transcript={{sessionId: '12345678', streaming: true,
    messages: [{role: 'assistant', content: '请选择', form: { ask_id: 'q', questions: [{
      id: 'choice', text: '选择方向', options: [{value: 1, label: '东'}, {value: 2, label: '西'}] }] } }]}}
    onSend={() => {}} onSubmitForm={() => {}} />
  for (const [path, view] of [...views, ['populated chat', populated], ['tokens', <TokenPanel session={[]} turn={[]} />], ['execution', <ExecutionMode disabled={false} />]] as const) {
    const page = document.createElement('div')
    page.innerHTML = renderToStaticMarkup(view)
    // Program listings, example JSON and user-entered field values keep their original syntax.
    const labels = Array.from(page.querySelectorAll<HTMLElement>('[placeholder]'), el => el.getAttribute('placeholder')!)
    page.querySelectorAll('pre,textarea,.remote-mcp p > code,[aria-hidden="true"]').forEach(el => el.remove())
    const walker = document.createTreeWalker(page, NodeFilter.SHOW_TEXT)
    while (walker.nextNode()) labels.push(walker.currentNode.textContent!.trim())
    for (const label of labels) {
      if (/[a-z]/i.test(label) && /^[\x20-\x7e]+$/.test(label) && !allowed.has(label)) failures.push(path + ' ' + label)
    }
  }
  useExecutionMode.getState().set(false)
  expect(failures).toEqual([])
})

it.each(['Create tool', 'Use Remote MCP'])('opens the missing-tool destination for %s without a model call', async choice => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  localStorage.setItem('rb_language', 'en')
  const brief = 'Lay out the spiral stairs'
  const fetch = await configure(brief)
  const page = document.createElement('div')
  const app = createRoot(page)
  const button = (name: string) => [...page.querySelectorAll('button')].find(b => b.textContent === name)!
  HTMLElement.prototype.scrollTo = vi.fn()
  try {
    await act(async () => app.render(<App path="/" />))
    const box = page.querySelector('textarea')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(box, brief)
      box.dispatchEvent(new Event('input', {bubbles: true}))
    })
    await act(async () => button('Send').click())
    expect(page.textContent).toContain('Add a tool?')
    fetch.mockClear()
    await act(async () => button(choice).click())
    if (choice === 'Create tool') {
      expect(window.location.pathname).toBe('/capabilities/authoring')
      expect(page.querySelector('textarea')?.value).toBe(brief)
    } else {
      expect(window.location.pathname).toBe('/settings/remote-mcp')
      expect(page.querySelector('h2')?.textContent).toBe('Remote MCP')
    }
    expect(fetch.mock.calls.every(([, options]) => !options?.method || options.method === 'GET')).toBe(true)
  } finally { act(() => app.unmount()) }
})
