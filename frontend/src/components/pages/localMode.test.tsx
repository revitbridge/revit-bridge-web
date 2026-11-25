/* A local host (features.mode 'local'): the Connect page has the local add-in and the model
   settings only, no request carries a device or admin header, and the admin-only controls
   work without a password. A cloud host keeps all of it, as before. */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../../App'
import { bridgeApi } from '../../api/bridge'
import { apiStream } from '../../api/client'
import { skillsApi } from '../../api/skills'
import { loadRuntimeConfig, type DeployMode } from '../../config'
import { adminHeaders, deviceHeaders, useSessionStore } from '../../store'
import SkillsView from '../capabilities/SkillsView'
import ToolLibrary from '../capabilities/ToolLibrary'
import { packGate } from '../shared/packAdmin'
import { taskApi } from '../task/api'
import ConnectPage from './ConnectPage'

type Call = { url: string; init: RequestInit }

function stubFetch(status: number, body: unknown) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  }))
  return calls
}

/* The host's /config.json as the backend writes it for each mode. */
async function host(mode: DeployMode, admin = mode === 'cloud') {
  stubFetch(200, { apiBase: '', wsBase: '', features: { mode, byoModel: true, serverModel: false, admin, maxDevices: mode === 'local' ? 0 : 20 } })
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
}

const pairingDeps = {
  api: {
    pair: async () => { throw new Error('a local host pairs nothing') },
    status: async () => { throw new Error('a local host has no devices') },
    revoke: async () => ({}),
    all: async () => [],
    capacity: async () => ({ max_devices: 0, connected: 0 }),
  },
  wait: async () => {},
  now: () => 0,
}

/* What a tab may still hold from a cloud host on the same origin: a device and a token. */
function tabWithADeviceAndAToken() {
  useSessionStore.getState().rememberDevice({ device_id: 'dev_one', browser_key: 'bk_secret', label: 'studio-01' })
  useSessionStore.getState().selectDevice('dev_one')
  useSessionStore.setState({ adminToken: 'pw' })
}

const SECRET_HEADERS = ['X-Device-Id', 'X-Device-Key', 'X-Admin-Token']

beforeEach(async () => {
  await host('local')
  tabWithADeviceAndAToken()
})

afterEach(async () => {
  vi.unstubAllGlobals()
  useSessionStore.setState({ deviceId: '', browserKey: '', devices: [], adminToken: '' })
  await host('cloud', false)            // the defaults every other test file sees
})

describe('the Connect page of a local host', () => {
  it('shows the local add-in and the model settings, nothing to pair, no devices, no admin password', () => {
    const html = renderToStaticMarkup(<ConnectPage deps={pairingDeps} />)
    expect(html).toContain('Check the connection')
    expect(html).toContain('Local add-in (TCP)')
    expect(html).toContain('This host runs in local mode')
    expect(html).not.toContain('Bring your own model')
    expect(html).not.toContain('Clear model settings')
    expect(html).not.toContain('Pair a Revit')
    expect(html).not.toContain('Name this device')
    expect(html).not.toContain('My devices')
    expect(html).not.toContain('devices connected')
    expect(html).not.toContain('placeholder="Admin password"')
    expect(html).not.toContain('>Admin<')
  })

  it('a cloud host keeps pairing, the device list and the admin field', async () => {
    await host('cloud')
    const html = renderToStaticMarkup(<ConnectPage deps={pairingDeps} />)
    expect(html).toContain('Pair a Revit')
    expect(html).toContain('My devices')
    expect(html).toContain('placeholder="Admin password"')
    expect(html).not.toContain('This host runs in local mode')
  })

  it('the app opens Chat with a link to Settings', () => {
    const html = renderToStaticMarkup(<App path="/" />)
    expect(html).toContain('href="/settings"')
    expect(html).not.toContain('Pair a Revit')
  })
})

describe('requests from a local host', () => {
  it('carry no device header and no admin token', async () => {
    expect(deviceHeaders()).toEqual({})
    expect(adminHeaders()).toEqual({})
    const calls = stubFetch(200, [])
    await bridgeApi.listTools()                                    // api/client.ts
    await taskApi.evidence()                                       // components/shared/http.ts
    await bridgeApi.deleteTool('create_beam')                      // an admin route
    await skillsApi.list()
    await apiStream('/api/chat', { message: 'hi' }, { 'X-LLM-Model': 'm' })
    expect(calls).toHaveLength(5)
    for (const c of calls) {
      for (const name of SECRET_HEADERS) expect(c.init.headers).not.toHaveProperty(name)
    }
  })

  it('on a cloud host the same calls carry them, as before', async () => {
    await host('cloud')
    expect(deviceHeaders()).toEqual({ 'X-Device-Id': 'dev_one', 'X-Device-Key': 'bk_secret' })
    const calls = stubFetch(200, [])
    await taskApi.evidence()
    await bridgeApi.deleteTool('create_beam')
    for (const c of calls) {
      expect(c.init.headers).toMatchObject({ 'X-Device-Id': 'dev_one', 'X-Device-Key': 'bk_secret', 'X-Admin-Token': 'pw' })
    }
  })
})

describe('admin-only controls on a local host', () => {
  it('are on without a password', () => {
    useSessionStore.setState({ adminToken: '' })
    expect(packGate(false, '', 'local')).toEqual({ canEdit: true, note: '' })
    expect(packGate(false, '')).toEqual({ canEdit: false,
      note: 'This host has no admin password configured; packs are read-only here (running them still works).' })

    const library = renderToStaticMarkup(<ToolLibrary />)
    expect(library).not.toContain('read-only')
    expect(library).not.toContain('admin password')

    const skills = renderToStaticMarkup(<SkillsView />)
    expect(skills).toContain('+ Add skill')
    expect(skills).toContain('Import from GitHub')
    expect(skills).not.toContain('admin password')
  })

  it('stay behind the password on a cloud host', async () => {
    useSessionStore.setState({ adminToken: '' })
    await host('cloud')
    const skills = renderToStaticMarkup(<SkillsView />)
    expect(skills).not.toContain('+ Add skill')
    expect(skills).toContain('Enter the admin password in Settings to add or change skills.')
  })
})
