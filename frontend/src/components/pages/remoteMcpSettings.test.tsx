// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import { loadRuntimeConfig } from '../../config'
import RemoteMcpSettings from './RemoteMcpSettings'

it('shows the bearer only on creation and keeps both configurations safe to copy', async () => {
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
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'Studio')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await act(async () => div.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true })))
    expect(div.textContent).toContain(token)
    for (const snippet of div.querySelectorAll('pre')) {
      expect(snippet.textContent).toContain('REVIT_MCP_TOKEN')
      expect(snippet.textContent).not.toContain(token)
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
