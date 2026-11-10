/* Changing capability packs needs the admin password: the gate the Capabilities page and
   the Solidify panel show, the header the calls carry, the server's refusal in its words. */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { bridgeApi } from '../../api/bridge'
import { loadRuntimeConfig } from '../../config'
import { useSessionStore } from '../../store'
import type { TaskSpec } from '../../types/api'
import ToolLibrary from '../capabilities/ToolLibrary'
import SolidifyPanel from '../task/SolidifyPanel'
import { taskApi } from '../task/api'
import { BridgeApiError } from './http'
import { packGate } from './packAdmin'

type Call = { url: string; init: RequestInit }

function stubFetch(status: number, body: unknown) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  }))
  return calls
}

async function hostWithAdmin(admin: boolean) {
  stubFetch(200, { apiBase: '', wsBase: '', features: { byoModel: true, serverModel: false, admin, maxDevices: 20 } })
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
}

const spec: TaskSpec = { task: 'count walls', action: { kind: 'execute_code', code: 'return 1;' }, parameters: [] }
const solidify = async () => ({ status: 'solidified', name: 'n', display_name: 'N', version: '1.0.0', revit_synced: false })

beforeEach(() => useSessionStore.setState({ adminToken: '' }))
afterEach(async () => {
  vi.unstubAllGlobals()
  useSessionStore.setState({ adminToken: '' })
  await hostWithAdmin(false)
})

describe('packGate', () => {
  it('opens only with an admin password configured and entered, and says why otherwise', () => {
    expect(packGate(true, 'pw')).toEqual({ canEdit: true, note: '' })
    expect(packGate(true, '')).toEqual({ canEdit: false,
      note: 'Enter the admin password on the Connect page to edit, delete or save packs.' })
    expect(packGate(false, 'pw')).toEqual({ canEdit: false,
      note: 'This host has no admin password configured; packs are read-only here (running them still works).' })
  })
})

describe('the pack calls', () => {
  it('carry the admin token from the session like the skill and log calls', async () => {
    const calls = stubFetch(200, {})
    useSessionStore.setState({ adminToken: 'pw' })
    await bridgeApi.updateTool('query_levels', { description: 'd' })
    await bridgeApi.deleteTool('create_beam')
    await taskApi.solidify({ name: 'n', code: 'c', description: '', parameters: [], source_query: '' })
    expect(calls.map(c => [c.init.method, c.url])).toEqual([
      ['PUT', '/api/v1/bridge/tools/query_levels'],
      ['DELETE', '/api/v1/bridge/tools/create_beam'],
      ['POST', '/api/v1/bridge/solidify'],
    ])
    for (const c of calls) expect(c.init.headers).toMatchObject({ 'X-Admin-Token': 'pw' })
  })

  it('a refusal reads as the server said it', async () => {
    stubFetch(403, { error: 'admin_required', message: 'Wrong or missing X-Admin-Token' })
    await expect(bridgeApi.deleteTool('create_beam')).rejects.toThrow('403: admin_required: Wrong or missing X-Admin-Token')
    const err = await taskApi.solidify({ name: 'n', code: 'c', description: '', parameters: [], source_query: '' })
      .catch((e: unknown) => e as BridgeApiError)
    expect((err as BridgeApiError).message).toBe('403: admin_required: Wrong or missing X-Admin-Token')
  })
})

/* Server rendering reads the session store's initial state (no token in this tab), so
   these show the two closed states; packGate above covers the open one. */
describe('the Solidify panel', () => {
  it('keeps Save as pack off with a note until the admin password is entered', async () => {
    let html = renderToStaticMarkup(<SolidifyPanel spec={spec} solidify={solidify} />)
    expect(html).toMatch(/<button class="btn-primary" disabled="">Save as pack<\/button>/)
    expect(html).toContain('This host has no admin password configured; packs are read-only here')
    expect(html).toContain('Pack name')                       // the draft itself is still there to read

    await hostWithAdmin(true)
    html = renderToStaticMarkup(<SolidifyPanel spec={spec} solidify={solidify} />)
    expect(html).toMatch(/<button class="btn-primary" disabled="">Save as pack<\/button>/)
    expect(html).toContain('Enter the admin password on the Connect page to edit, delete or save packs.')
  })
})

describe('the pack library', () => {
  it('says why packs are read-only, the way the skills view does', async () => {
    expect(renderToStaticMarkup(<ToolLibrary />)).toContain('This host has no admin password configured; packs are read-only here')
    await hostWithAdmin(true)
    expect(renderToStaticMarkup(<ToolLibrary />)).toContain('Enter the admin password on the Connect page to edit, delete or save packs.')
  })
})
