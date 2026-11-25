/* The Token page: the measured runs, per role, two of them side by side. An unknown count
   is '-', and a sum or a difference with an unknown in it is unknown - never 0. */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from '../../App'
import { usageApi } from '../../api/usage'
import { loadRuntimeConfig } from '../../config'
import { pathOf, TABS, TOKEN_PATH, viewOf } from '../../route'
import { useSessionStore } from '../../store'
import type { UsageRun } from '../../types/api'
import TokenPage, { TokenView } from '../pages/TokenPage'
import {
  compareRoles, compareRows, countsOf, difference, formatCount, formatDifference, roleTotals, sumCounts,
  toggleSelection, usageGate,
} from './runs'
import { createTokenFlow, type TokenFlow } from './tokenFlow'

/* The page as a flow's state draws it (a server render reads a store's initial state). */
function view(flow: TokenFlow, gate = usageGate('local', false, '')): string {
  const state = flow.store.getState()
  return renderToStaticMarkup(<TokenView state={state} pair={flow.pair(state)} gate={gate} adminToken=""
    onRefresh={() => {}} onToggle={() => {}} onAdminToken={() => {}} />)
}

const runA: UsageRun = {
  run_label: 'baseline-1', calls: 3, input: 1000, output: 120, cache_read: 800, cache_write: null, ms: 900,
  first_at: '2026-11-23 10:00:00', last_at: '2026-11-23 10:00:09', mode: 'single',
  by_role: [
    { role: 'planner', model: 'main-model', calls: 2, input: 900, output: 100, cache_read: 800, cache_write: null, ms: 700 },
    { role: 'planner', model: 'other-model', calls: 1, input: 100, output: 20, cache_read: 0, cache_write: null, ms: 200 },
  ],
}

const runB: UsageRun = {
  run_label: 'roles-1', calls: 5, input: 1120, output: null, cache_read: 900, cache_write: 0, ms: 1100,
  first_at: '2026-11-23 11:00:00', last_at: '2026-11-23 11:00:00', mode: 'roles_analyst',
  by_role: [
    { role: 'analyst', model: 'small-model', calls: 2, input: 200, output: 10, cache_read: 0, cache_write: 0, ms: 300 },
    { role: 'planner', model: 'main-model', calls: 3, input: 920, output: null, cache_read: 900, cache_write: 0, ms: 800 },
  ],
}

const older: UsageRun = {      // a host before by_role
  run_label: 'old', calls: 1, input: 5, output: 5, cache_read: null, cache_write: null, ms: 1,
  first_at: '2026-11-22 09:00:00', last_at: '2026-11-22 09:00:00',
}

async function host(features: Record<string, unknown>) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ apiBase: '', wsBase: '', features }),
    { status: 200, headers: { 'content-type': 'application/json' } })))
  await loadRuntimeConfig()
  vi.unstubAllGlobals()
}

afterEach(async () => {
  vi.unstubAllGlobals()
  useSessionStore.setState({ adminToken: '' })
  await host({ mode: 'cloud', admin: false, maxDevices: 20 })
})

describe('the arithmetic', () => {
  it('shows an unknown count as -, never as 0', () => {
    expect(formatCount(null)).toBe('-')
    expect(formatCount(undefined)).toBe('-')
    expect(formatCount(0)).toBe('0')
    expect(formatCount(1234567)).toBe('1,234,567')
    expect(countsOf({ ...older, input: 'lots' as unknown as number, output: -1 })).toEqual(
      { calls: 1, input: null, cache_read: null, cache_write: null, output: null, ms: 1 })
  })

  it('a sum with an unknown in it is unknown', () => {
    expect(sumCounts([1, 2, 3])).toBe(6)
    expect(sumCounts([1, null, 3])).toBeNull()
    expect(sumCounts([0, undefined])).toBeNull()
  })

  it('a difference is B - A, unknown when either is; the percentage against a known A', () => {
    expect(difference(100, 112)).toBe(12)
    expect(difference(null, 112)).toBeNull()
    expect(difference(100, null)).toBeNull()
    expect(formatDifference(100, 112)).toBe('+12 (+12.0%)')
    expect(formatDifference(1000, 950)).toBe('-50 (-5.0%)')
    expect(formatDifference(0, 5)).toBe('+5')
    expect(formatDifference(7, 7)).toBe('0')
    expect(formatDifference(null, 0)).toBe('-')
  })

  it('sums each role over its models, in the order the run used them', () => {
    expect(roleTotals(runA)).toEqual([{ role: 'planner', models: ['main-model', 'other-model'],
      counts: { calls: 3, input: 1000, cache_read: 800, cache_write: null, output: 120, ms: 900 } }])
    expect(roleTotals(runB).map(total => [total.role, total.counts.output])).toEqual([['analyst', 10], ['planner', null]])
    expect(roleTotals(older)).toEqual([])
  })

  it('compares two runs metric by metric, unknown where either side is', () => {
    const rows = compareRows(countsOf(runA), countsOf(runB))
    const byKey = Object.fromEntries(rows.map(row => [row.key, row]))
    expect(byKey.input).toMatchObject({ a: '1,000', b: '1,120', diff: '+120 (+12.0%)', direction: 'up' })
    expect(byKey.output).toMatchObject({ a: '120', b: '-', diff: '-', direction: 'unknown' })
    expect(byKey.cache_write).toMatchObject({ a: '-', b: '0', diff: '-', direction: 'unknown' })
    expect(byKey.calls).toMatchObject({ diff: '+2 (+66.7%)', direction: 'up' })

    const roles = compareRoles(runA, runB)
    expect(roles.map(role => [role.role, role.modelsA, role.modelsB])).toEqual([
      ['planner', 'main-model, other-model', 'main-model'], ['analyst', '-', 'small-model']])
    const analyst = Object.fromEntries(roles[1].rows.map(row => [row.key, row]))
    expect(analyst.input).toMatchObject({ a: '-', b: '200', diff: '-' })
  })

  it('picks two runs at most; a third lets go of the first, picking again unpicks', () => {
    expect(toggleSelection([], 'a')).toEqual(['a'])
    expect(toggleSelection(['a'], 'b')).toEqual(['a', 'b'])
    expect(toggleSelection(['a', 'b'], 'c')).toEqual(['b', 'c'])
    expect(toggleSelection(['a', 'b'], 'a')).toEqual(['b'])
  })

  it('reads on a local host; on a cloud host only with the admin password', () => {
    expect(usageGate('local', false, '')).toMatchObject({ canRead: true, askToken: false })
    expect(usageGate('cloud', false, '')).toMatchObject({ canRead: false, askToken: false })
    expect(usageGate('cloud', true, '')).toMatchObject({ canRead: false, askToken: true })
    expect(usageGate('cloud', true, 'pw')).toMatchObject({ canRead: true, askToken: true, note: '' })
  })
})

describe('the token flow', () => {
  it('loads the runs, picks two, forgets a pick whose run is gone, keeps the reason of a failure', async () => {
    const api = { runs: vi.fn().mockResolvedValue([runA, runB, older]) }
    const flow = createTokenFlow(api)
    expect(flow.store.getState().runs).toBeNull()
    await flow.load()
    expect(flow.store.getState().runs).toEqual([runA, runB, older])
    flow.toggle('roles-1')
    expect(flow.pair()).toBeNull()
    flow.toggle('baseline-1')
    expect(flow.pair()?.map(run => run.run_label)).toEqual(['roles-1', 'baseline-1'])

    api.runs.mockResolvedValueOnce([runA, older])
    await flow.load()
    expect(flow.store.getState().selected).toEqual(['baseline-1'])
    expect(flow.pair()).toBeNull()

    api.runs.mockRejectedValueOnce(new Error('403: Unauthorized'))
    await flow.load()
    expect(flow.store.getState().notice).toBe('Cannot read the runs: 403: Unauthorized')
    expect(flow.store.getState().runs).toEqual([runA, older])          // the last list stays
  })
})

describe('the Token page', () => {
  it('lists the runs with their roles and compares the two picked', async () => {
    const flow = createTokenFlow({ runs: async () => [runA, runB, older] })
    await flow.load()
    flow.toggle('baseline-1')
    flow.toggle('roles-1')
    const page = view(flow)

    expect(page).toContain('data-run="baseline-1"')
    expect(page).toContain('2026-11-23 10:00:00 - 2026-11-23 10:00:09 UTC')
    expect(page).toContain('2026-11-23 11:00:00 UTC')
    // The run's own line: unknown output and cache write are '-', not 0.
    expect(page).toMatch(/data-run="roles-1">.*?<td>5<\/td><td>1,120<\/td><td>900<\/td><td>0<\/td><td>-<\/td><td>1,100<\/td>/)
    expect(page).toMatch(/data-run="baseline-1">.*?<td>3<\/td><td>1,000<\/td><td>800<\/td><td>-<\/td><td>120<\/td>/)
    expect(page).toMatch(/data-role="planner"><td><\/td><td colSpan="3">planner <span class="mono">\(main-model, other-model\)/i)
    expect(page).toContain('data-run="old"')
    expect(page).not.toContain('Admin password')
    // The chat mode of each run; '-' for a run the host recorded none for.
    expect(page).toContain('<th>Run</th><th>Mode</th><th>Time</th>')
    expect(page).toMatch(/data-run="baseline-1">.*?<td class="mono">baseline-1<\/td><td class="token-mode">Single model<\/td>/)
    expect(page).toMatch(/data-run="roles-1">.*?<td class="token-mode">Analyst \+ planner \+ supervisor<\/td>/)
    expect(page).toMatch(/data-run="old">.*?<td class="token-mode">-<\/td>/)

    expect(page).toContain('A: baseline-1 (Single model) - B: roles-1 (Analyst + planner + supervisor)')
    expect(page).toMatch(/data-metric="input"><td>Input<\/td><td>1,000<\/td><td>1,120<\/td><td class="token-diff up">\+120 \(\+12\.0%\)/)
    expect(page).toMatch(/data-metric="output"><td>Output<\/td><td>120<\/td><td>-<\/td><td class="token-diff unknown">-<\/td>/)
    expect(page).toContain('data-role="analyst"')                           // the role only B has
  })

  it('says when one more run is needed, and when there are none', async () => {
    const one = createTokenFlow({ runs: async () => [runA] })
    await one.load()
    one.toggle('baseline-1')
    const page = view(one)
    expect(page).toContain('Pick one more run to compare.')
    expect(page).not.toContain('token-comparison')

    const none = createTokenFlow({ runs: async () => [] })
    await none.load()
    expect(view(none)).toContain('No measured runs yet')
    // Before the first answer: neither a list nor the empty note.
    expect(view(createTokenFlow({ runs: async () => [] }))).not.toContain('No measured runs yet')
  })

  it('asks a cloud host for the admin password before it reads', async () => {
    await host({ mode: 'cloud', admin: true, maxDevices: 20 })
    const page = renderToStaticMarkup(<TokenPage flow={createTokenFlow({ runs: async () => [] })} />)
    expect(page).toContain('placeholder="Admin password"')
    expect(page).toContain('Token usage is admin only on this host')
    expect(page).toMatch(/<button class="btn-secondary" disabled="">Refresh<\/button>/)
  })

  it('reads GET /api/usage/runs with the admin token on a cloud host, without it on a local one', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    const answer = () => vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init })
      return new Response(JSON.stringify({ items: [runA] }), { status: 200, headers: { 'content-type': 'application/json' } })
    }))
    await host({ mode: 'cloud', admin: true, maxDevices: 20 })
    useSessionStore.setState({ adminToken: 'pw' })
    answer()
    expect(await usageApi.runs()).toEqual([runA])
    expect(calls[0].url).toBe('/api/usage/runs')
    expect(calls[0].init.headers).toMatchObject({ 'X-Admin-Token': 'pw' })

    vi.unstubAllGlobals()
    await host({ mode: 'local', admin: false, maxDevices: 0 })
    answer()
    await usageApi.runs()
    expect(calls[1].init.headers).not.toHaveProperty('X-Admin-Token')
  })

  it('has its own tab and address, /token', () => {
    expect(TABS).toContain('Token')
    expect(viewOf('/token')).toEqual({ tab: TABS.indexOf('Token'), capabilities: 'packs' })
    expect(viewOf('/Token/')).toEqual(viewOf(TOKEN_PATH))
    expect(pathOf(viewOf('/token'))).toBe('/token')
    expect(pathOf({ tab: 0, capabilities: 'packs' })).toBe('/')
    const html = renderToStaticMarkup(<App path="/token" />)
    expect(html).toContain('<button class="tab-button is-active">Token</button>')
    expect(html).toContain('<h3 class="heading-display section-title">Token</h3>')
  })
})
