// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, onTestFinished, vi } from 'vitest'
import { loadRuntimeConfig } from '../../config'
import RemoteMcpSettings from './RemoteMcpSettings'

const storageKey = 'revit-bridge-web.mcpClient'
const clientNames = ['Claude Code', 'Codex CLI', 'Cursor', 'VS Code']

afterEach(() => localStorage.removeItem(storageKey))

it('shows the bearer only on creation and keeps all configurations safe to copy', async () => {
  const token = 'one-time-test-value'
  const info = { id: 'abc', device_id: 'device-a', label: 'Studio', created_at: 'today', last_used_at: null }
  let exists = false
  const fetch = vi.fn(async (url: string, opts?: RequestInit) => {
    if (url === '/config.json') return Response.json({ features: { mode: 'cloud' } })
    if (opts?.method === 'POST') {
      expect(JSON.parse(String(opts.body))).toEqual({ label: 'Studio' })
      exists = true
      return Response.json({ info, token })
    }
    if (opts?.method === 'DELETE') {
      expect(url.endsWith('/tokens/abc')).toBe(true)
      exists = false
      return Response.json({ revoked: true })
    }
    return Response.json({ enabled: true, device_id: 'device-a', tokens: exists ? [info] : [],
      calls: [{ time: 'today', tool: 'dry_run_code', success: true, evidence_id: 'ev_example' }] })
  })
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('fetch', fetch)
  const div = document.createElement('div')
  const root = createRoot(div)
  const button = (name: string) => [...div.querySelectorAll('button')].find(b => b.textContent === name)!
  try {
    await loadRuntimeConfig()
    await act(async () => root.render(<RemoteMcpSettings />))
    expect(div.textContent).toContain('Enabled')
    expect(div.textContent).toContain('ev_example')
    const input = div.querySelector('input')!
    expect(input.maxLength).toBe(64)
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'Studio')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => div.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true })))
    expect(div.textContent).toContain(token)
    for (const name of clientNames) {
      act(() => button(name).click())
      for (const snippet of div.querySelectorAll('pre')) {
        expect(snippet.textContent).toContain('REVIT_MCP_TOKEN')
        expect(snippet.textContent).not.toContain(token)
      }
    }
    await act(async () => button('Dismiss').click())
    await act(async () => button('Refresh').click())
    expect(div.textContent).not.toContain(token)
    expect(JSON.stringify(sessionStorage) + JSON.stringify(localStorage)).not.toContain(token)
    await act(async () => button('Revoke').click())
    expect(exists).toBe(false)
    expect(button('Revoke')).toBeUndefined()
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})

async function mountLocal() {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('fetch', async (url: string) => url === '/config.json'
    ? Response.json({ features: { mode: 'local' } })
    : Response.json({ enabled: true, device_id: null, tokens: [], calls: [] }))
  await loadRuntimeConfig()
  const page = document.createElement('div')
  document.body.append(page)
  const root = createRoot(page)
  onTestFinished(() => {
    act(() => root.unmount())
    page.remove()
    vi.unstubAllGlobals()
  })
  await act(async () => root.render(<RemoteMcpSettings />))
  return page
}

it('leads with neutral JSON and shows the selected client configuration with or without a token', async () => {
  const page = await mountLocal()
  const address = window.location.origin + '/mcp'
  const firstSnippet = page.querySelector('.snippet')!
  expect(firstSnippet.querySelector('h3')?.textContent).toBe('MCP JSON (mcpServers)')
  const tablist = page.querySelector('[role="tablist"]')!
  expect(document.getElementById(tablist.getAttribute('aria-labelledby')!)?.textContent).toBe('Client setup')
  const tabs = [...tablist.querySelectorAll<HTMLButtonElement>('button[role="tab"]')]
  expect(tabs.map(tab => tab.textContent)).toEqual(clientNames)
  expect(tablist.outerHTML).not.toMatch(/recommended/i)
  expect(tabs[0].getAttribute('aria-selected')).toBe('true')
  const checkbox = page.querySelector<HTMLInputElement>('input[type="checkbox"]')!
  for (const withToken of [false, true]) {
    if (withToken) act(() => checkbox.click())
    expect(checkbox.checked).toBe(withToken)
    const entry = (authorization: string) => withToken ? { url: address, headers: { Authorization: authorization } } : { url: address }
    const http = { type: 'http', ...entry('Bearer ${REVIT_MCP_TOKEN}') }
    const env = entry('Bearer ${env:REVIT_MCP_TOKEN}')
    expect(JSON.parse(firstSnippet.querySelector('pre')!.textContent!)).toEqual({ mcpServers: { revit: http } })
    const expected = [
      'claude mcp add --transport http revit ' + address + (withToken ? " --header 'Authorization: Bearer ${REVIT_MCP_TOKEN}'" : ''),
      'codex mcp add revit --url ' + address + (withToken ? ' --bearer-token-env-var REVIT_MCP_TOKEN' : ''),
      { mcpServers: { revit: env } },
      { servers: { revit: { type: 'http', ...env } } },
    ]
    for (const [index, tab] of tabs.entries()) {
      act(() => tab.click())
      expect(localStorage.getItem(storageKey)).toBe(clientNames[index])
      expect(tablist.querySelectorAll('[aria-selected="true"]')).toHaveLength(1)
      expect(tab.getAttribute('aria-selected')).toBe('true')
      const panels = page.querySelectorAll('[role="tabpanel"]')
      expect(panels).toHaveLength(1)
      expect(panels[0].id).toBe(tab.getAttribute('aria-controls'))
      expect(panels[0].getAttribute('aria-labelledby')).toBe(tab.id)
      const text = panels[0].querySelector('pre')!.textContent!
      expect(typeof expected[index] === 'string' ? text : JSON.parse(text)).toEqual(expected[index])
    }
  }
})

it.each([
  ['Cursor', 'Cursor'],
  ['unknown-client', 'Claude Code'],
])('restores the client preference %s as %s', async (stored, selected) => {
  localStorage.setItem(storageKey, stored)
  const page = await mountLocal()
  expect(page.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe(selected)
})

it('moves selection, keyboard focus and the tab stop together with the arrow keys', async () => {
  const page = await mountLocal()
  const tabs = [...page.querySelectorAll<HTMLButtonElement>('[role="tab"]')]
  tabs[0].focus()
  for (const [key, next] of [['ArrowLeft', 3], ['ArrowRight', 0], ['ArrowRight', 1]] as const) {
    act(() => document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })))
    expect(document.activeElement).toBe(tabs[next])
    expect(tabs.filter(tab => tab.tabIndex === 0)).toEqual([tabs[next]])
    expect(tabs.filter(tab => tab.tabIndex === -1)).toHaveLength(3)
    expect(tabs.filter(tab => tab.getAttribute('aria-selected') === 'true')).toEqual([tabs[next]])
    expect(localStorage.getItem(storageKey)).toBe(clientNames[next])
  }
})

it('shows the disabled cloud endpoint without reporting a pairing or storage failure', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url === '/config.json'
    ? Response.json({ features: { mode: 'cloud' } })
    : Response.json({ error: 'remote_mcp_disabled' }, { status: 404 })))
  const div = document.createElement('div')
  const root = createRoot(div)
  try {
    await loadRuntimeConfig()
    await act(async () => root.render(<RemoteMcpSettings />))
    expect(div.textContent).toContain('Disabled')
    expect(div.querySelector('[role="alert"], form')).toBeNull()
    expect(div.textContent).not.toContain('Select a paired device')
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
