/* The Connect page's "Models by role": the three roles and their model names, read-only,
   and one line saying where they come from - nothing else of the host's configuration. */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadRuntimeConfig } from '../../config'
import ConnectPage from '../pages/ConnectPage'
import { MODELS_FROM_ENV, MODELS_FROM_FILE, ROLES_NO_ANALYST, ROLES_OFF, ROLES_ON, roleRows } from './models'
import RoleModels from './RoleModels'

const html = (text: string) => text.replace(/'/g, '&#x27;')

async function host(features: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ apiBase: '', wsBase: '', features }),
    { status: 200, headers: { 'content-type': 'application/json' } })))
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
}

const pairingDeps = {
  api: {
    pair: async () => { throw new Error('not here') },
    status: async () => { throw new Error('not here') },
    revoke: async () => ({}),
    all: async () => [],
    capacity: async () => ({ max_devices: 20, connected: 0 }),
  },
  wait: async () => {},
  now: () => 0,
}

afterEach(async () => {
  vi.unstubAllGlobals()
  await host({ mode: 'cloud', admin: false, maxDevices: 20 })      // the defaults other tests see
})

describe('the roles and their models', () => {
  it('one row per role, in order; a role without a model says so', () => {
    expect(roleRows({ analyst: 'small-model', planner: 'main-model', supervisor: null })).toEqual([
      { role: 'analyst', label: 'Analyst', does: 'sorts the request', model: 'small-model' },
      { role: 'planner', label: 'Planner', does: 'picks the tool and fills it in (the chat)', model: 'main-model' },
      { role: 'supervisor', label: 'Supervisor', does: 'reports the result', model: 'not set' },
    ])
  })

  it('says where the names come from: the models file, or the environment and this browser', () => {
    const fromFile = renderToStaticMarkup(
      <RoleModels models={{ analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' }} modelsFile roles />)
    expect(fromFile).toContain('Models by role')
    expect(fromFile).toContain(html(MODELS_FROM_FILE))
    expect(fromFile).not.toContain(html(MODELS_FROM_ENV))
    expect(fromFile).toContain('<tr data-role="analyst"><td>Analyst')
    expect(fromFile).toMatch(/data-role="planner">.*<td class="mono">main-model<\/td>/)
    expect(fromFile).toMatch(/data-role="supervisor">.*<td class="mono">small-model<\/td>/)
    expect(fromFile).not.toContain('<input')                              // read-only

    const fromEnv = renderToStaticMarkup(
      <RoleModels models={{ analyst: null, planner: null, supervisor: null }} modelsFile={false} roles />)
    expect(fromEnv).toContain(html(MODELS_FROM_ENV))
    expect(fromEnv.match(/<td class="mono">not set<\/td>/g)).toHaveLength(3)
  })

  it('says whether the roles run or one loop does (LLM_SINGLE_MODEL=1)', () => {
    const models = { analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' }
    const on = renderToStaticMarkup(<RoleModels models={models} modelsFile roles />)
    expect(on).toContain(html(ROLES_ON))
    expect(on).not.toContain(html(ROLES_OFF))
    const off = renderToStaticMarkup(<RoleModels models={models} modelsFile roles={false} />)
    expect(off).toContain(html(ROLES_OFF))
    expect(off).not.toContain('Only the planner runs in this version')
  })

  it('says what runs by default: planner + supervisor, the analyst too, or one loop', async () => {
    const models = { analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' }
    const shown = (chatModeDefault: string | null, roles = true) =>
      renderToStaticMarkup(<RoleModels models={models} modelsFile roles={roles} chatModeDefault={chatModeDefault} />)
    expect(shown('roles')).toContain(html(ROLES_NO_ANALYST))
    expect(shown('roles')).not.toContain(html(ROLES_ON))
    expect(shown('roles_analyst')).toContain(html(ROLES_ON))
    expect(shown('single', false)).toContain(html(ROLES_OFF))
    expect(shown(null)).toContain(html(ROLES_ON))           // a host from before the modes: roles meant the analyst too
    await host({ mode: 'local', admin: false, maxDevices: 0, models, modelsFile: true, roles: true,
      chatModes: ['auto', 'single', 'roles', 'roles_analyst'], chatModeDefault: 'roles' })
    expect(renderToStaticMarkup(<ConnectPage deps={pairingDeps} />)).toContain(html(ROLES_NO_ANALYST))
  })

  it('the Connect page shows them on a local and on a cloud host, from /config.json', async () => {
    const models = { analyst: 'small-model', planner: 'main-model', supervisor: 'small-model' }
    await host({ mode: 'local', admin: false, maxDevices: 0, models, modelsFile: true, roles: true })
    const local = renderToStaticMarkup(<ConnectPage deps={pairingDeps} />)
    expect(local).toContain('Models by role')
    expect(local).toContain(html(ROLES_ON))
    expect(local).toContain(html(MODELS_FROM_FILE))
    expect(local.indexOf('Models by role')).toBeLessThan(local.indexOf('Bring your own model'))
    expect(local).toMatch(/data-role="planner">.*main-model/)

    await host({ mode: 'cloud', admin: true, maxDevices: 20, models: { ...models, planner: 'env-model' }, modelsFile: false })
    const cloud = renderToStaticMarkup(<ConnectPage deps={pairingDeps} />)
    expect(cloud).toContain(html(MODELS_FROM_ENV))
    expect(cloud).toMatch(/data-role="planner">.*env-model/)
    expect(cloud).toContain(html(ROLES_OFF))                // no roles field: an older host, one loop
    // No path of the server, no address: the host sends names only and the page adds none.
    expect(cloud).not.toContain('models.yml')
  })
})
