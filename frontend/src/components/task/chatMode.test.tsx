/* The chat mode: what the host lets this browser pick, the menu next to the input, the pick
   remembered in localStorage, and the `mode` every chat request of the task page carries. */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { chatEvents } from '../../api/chat'
import { loadRuntimeConfig, type RuntimeFeatures } from '../../config'
import type { ExecutionResult, HostEvent, HostRequest } from '../../types/api'
import {
  CHAT_MODE_KEY, currentRequestMode, loadChatMode, modeLabel, pickableModes, requestMode, saveChatMode, useChatMode,
} from './chatMode'
import ChatModeSelect from './ChatModeSelect'
import { createTaskFlow, type FlowApi } from './flow'

const ALL = ['auto', 'single', 'roles', 'roles_analyst'] as const
type Features = Pick<RuntimeFeatures, 'chatModes' | 'chatModeDefault'>
const local: Features = { chatModes: [...ALL], chatModeDefault: 'roles' }
const visitor: Features = { chatModes: ['auto'], chatModeDefault: 'roles' }
const older: Features = { chatModes: [], chatModeDefault: null }

/* A Storage in memory; `broken` throws on every call, as a browser that refuses storage does. */
function memoryStorage(broken = false): Storage {
  const items = new Map<string, string>()
  const guard = () => { if (broken) throw new Error('SecurityError') }
  return {
    get length() { guard(); return items.size },
    clear: () => { guard(); items.clear() },
    getItem: (key: string) => { guard(); return items.get(key) ?? null },
    key: (index: number) => { guard(); return [...items.keys()][index] ?? null },
    removeItem: (key: string) => { guard(); items.delete(key) },
    setItem: (key: string, value: string) => { guard(); items.set(key, String(value)) },
  }
}

async function host(features: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ apiBase: '', wsBase: '', features }),
    { status: 200, headers: { 'content-type': 'application/json' } })))
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
}

beforeEach(() => vi.stubGlobal('localStorage', memoryStorage()))

afterEach(async () => {
  vi.unstubAllGlobals()
  useChatMode.setState({ mode: 'auto' })
  await host({ mode: 'cloud', admin: false, maxDevices: 20 })
})

describe('the modes a browser may pick', () => {
  it('two or more modes make a choice; fewer make none, and nothing is sent', () => {
    expect(pickableModes(local)).toEqual([...ALL])
    expect(pickableModes(visitor)).toEqual([])
    expect(pickableModes(older)).toEqual([])
    expect(requestMode(local, 'single')).toBe('single')
    expect(requestMode(local, 'auto')).toBe('auto')
    expect(requestMode(visitor, 'single')).toBeUndefined()
    expect(requestMode(older, 'roles')).toBeUndefined()
    // A pick the host does not offer goes as auto.
    expect(requestMode({ chatModes: ['auto', 'roles'] }, 'single')).toBe('auto')
  })

  it('Auto names what it runs on this host', () => {
    expect(modeLabel('auto', 'roles')).toBe('Auto (Planner + supervisor)')
    expect(modeLabel('auto', 'single')).toBe('Auto (Single model)')
    expect(modeLabel('auto', null)).toBe('Auto')
    expect(modeLabel('roles_analyst', 'roles')).toBe('Analyst + planner + supervisor')
  })
})

describe('the remembered pick', () => {
  it('lives in localStorage; auto leaves nothing there; anything else read is auto', () => {
    expect(loadChatMode()).toBe('auto')
    saveChatMode('roles_analyst')
    expect(localStorage.getItem(CHAT_MODE_KEY)).toBe('roles_analyst')
    expect(loadChatMode()).toBe('roles_analyst')
    saveChatMode('auto')
    expect(localStorage.getItem(CHAT_MODE_KEY)).toBeNull()
    localStorage.setItem(CHAT_MODE_KEY, 'turbo')
    expect(loadChatMode()).toBe('auto')
  })

  it('a browser that refuses storage still picks, for this page', () => {
    vi.stubGlobal('localStorage', memoryStorage(true))
    expect(loadChatMode()).toBe('auto')
    expect(() => saveChatMode('single')).not.toThrow()
    useChatMode.getState().setMode('single')
    expect(useChatMode.getState().mode).toBe('single')
  })

  it('the store keeps it and what the page sends follows the host', async () => {
    useChatMode.getState().setMode('single')
    expect(localStorage.getItem(CHAT_MODE_KEY)).toBe('single')
    await host({ mode: 'local', chatModes: [...ALL], chatModeDefault: 'roles' })
    expect(currentRequestMode()).toBe('single')
    await host({ mode: 'cloud', chatModes: ['auto'], chatModeDefault: 'roles' })
    expect(currentRequestMode()).toBeUndefined()
  })
})

describe('the menu next to the input', () => {
  const render = (features: Features, mode: Parameters<typeof ChatModeSelect>[0]['mode'] = 'auto') =>
    renderToStaticMarkup(<ChatModeSelect features={features} mode={mode} onChange={() => {}} />)

  it('lists the modes the host offers, Auto with what it runs, the pick selected', () => {
    const html = render(local, 'roles_analyst')
    expect(html).toContain('<span class="chat-mode-label">Mode</span>')
    expect(html).toContain('<option value="auto">Auto (Planner + supervisor)</option>')
    expect(html).toContain('<option value="single">Single model</option>')
    expect(html).toContain('<option value="roles">Planner + supervisor</option>')
    expect(html).toContain('<option value="roles_analyst" selected="">Analyst + planner + supervisor</option>')
  })

  it('is not there with fewer than two modes', () => {
    expect(render(visitor)).toBe('')
    expect(render(older)).toBe('')
  })

  it('a pick the host does not offer shows as Auto', () => {
    expect(render({ chatModes: ['auto', 'roles'], chatModeDefault: 'roles' }, 'single'))
      .toContain('<option value="auto" selected="">Auto (Planner + supervisor)</option>')
  })
})

describe('the task page sends the mode', () => {
  const report: ExecutionResult = { success: true, error: null, evidence_id: 'ev_1' }
  const api: FlowApi = {
    reconcile: async () => ({ conflicts: [], questions: [], interpretations_required: [], ready: true }),
    confirm: async () => ({ token: 't', spec_hash: 'h', expires_at: '', card: '' }),
    runTool: async () => report,
  }

  function scripted() {
    const requests: HostRequest[] = []
    const events = async function* (request: HostRequest): AsyncGenerator<HostEvent> {
      requests.push(request)
      yield { type: 'session', id: 's1' }
      yield { type: 'token', text: 'ok' }
      yield { type: 'done' }
    }
    return { events, requests }
  }

  it('with every bridge request, read when it goes out; never with the comparison', async () => {
    let mode: HostRequest['mode'] = 'roles'
    const { events, requests } = scripted()
    const flow = createTaskFlow({ events, api, mode: () => mode })
    flow.setCompare(true)
    await flow.send('levels?')
    mode = 'single'
    flow.store.setState({ execution: report })
    await flow.report()
    expect(requests.map(r => [r.bridge ?? null, r.mode ?? null])).toEqual([
      [true, 'roles'], [false, null], [null, 'single']])
    expect(requests[2].execution).toEqual(report)
  })

  it('nothing when there is no mode to send', async () => {
    const { events, requests } = scripted()
    await createTaskFlow({ events, api, mode: () => undefined }).send('hi')
    await createTaskFlow({ events, api }).send('hi')
    expect(requests.every(r => !('mode' in r))).toBe(true)
  })

  it('chatEvents puts it in the body', async () => {
    const fetchMock = vi.fn(async () => new Response('event: done\ndata: [DONE]\n\n', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    for await (const event of chatEvents({ message: 'hi', session_id: null, mode: 'roles_analyst' })) void event
    for await (const event of chatEvents({ message: 'hi', session_id: null })) void event
    const bodies = fetchMock.mock.calls.map(call => JSON.parse(String((call as unknown as [string, RequestInit])[1].body)))
    expect(bodies[0].mode).toBe('roles_analyst')
    expect('mode' in bodies[1]).toBe(false)
  })
})
