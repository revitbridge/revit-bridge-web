import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSessionStore } from '../../store'
import type { DeviceStatus, PairingResponse } from '../../types/api'
import { BridgeApiError } from '../shared/http'
import { CODE_EXPIRED, createPairingFlow, POLL_INTERVAL_MS } from './pairing'
import { lastSeen, timeLeft } from './format'

const issued: PairingResponse = {
  code: 'K7QD-2M9X',
  device_id: 'dev_a1b2c3d4e5f6',
  expires_at: '2026-09-23T09:10:00Z',
  browser_key: 'bk_secret_value',
  install_command: '& ([scriptblock]::Create((irm https://host/install.ps1))) -Mode remote -Server https://host -Pair K7QD-2M9X',
}

const offline: DeviceStatus = { device_id: issued.device_id, label: 'studio-01', online: false, last_seen: null, requests: 0, revoked: false }
const online: DeviceStatus = { ...offline, online: true, last_seen: '2026-09-23T09:01:00Z', requests: 3 }

/* An API whose status() answers from a script; every call is recorded with its key. */
function fakeApi(statuses: DeviceStatus[] | Error[], overrides: Record<string, unknown> = {}) {
  const script = [...statuses]
  const statusCalls: Array<[string, string]> = []
  return {
    statusCalls,
    pair: vi.fn().mockResolvedValue(issued),
    status: vi.fn(async (id: string, key: string) => {
      statusCalls.push([id, key])
      const next = script.length > 1 ? script.shift()! : script[0]
      if (next instanceof Error) throw next
      return next
    }),
    revoke: vi.fn().mockResolvedValue({ status: 'revoked' }),
    all: vi.fn().mockResolvedValue([]),
    capacity: vi.fn().mockResolvedValue({ max_devices: 20, connected: 1 }),
    ...overrides,
  }
}

/* No real waiting; the clock moves by the interval each time the flow waits. */
function fakeClock() {
  let time = 0
  return {
    now: () => time,
    wait: async (ms: number) => { time += ms },
    advance: (ms: number) => { time += ms },
  }
}

beforeEach(() => {
  useSessionStore.setState({ deviceId: '', browserKey: '', devices: [] })
  try { sessionStorage.clear() } catch { /* no storage in this environment */ }
})

describe('pairing a device', () => {
  it('pairs, polls every three seconds with the device key, and selects it when it comes online', async () => {
    const api = fakeApi([offline, offline, online])
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('  studio-01  ')

    expect(api.pair).toHaveBeenCalledWith('studio-01')
    expect(api.status).toHaveBeenCalledTimes(3)
    expect(api.statusCalls[0]).toEqual([issued.device_id, issued.browser_key])   // its own key, not the session's
    expect(clock.now()).toBe(3 * POLL_INTERVAL_MS)

    const s = flow.store.getState()
    expect(s.pairing).toBeNull()
    expect(s.waiting).toBe(false)
    expect(s.statuses[issued.device_id]).toEqual(online)

    // the browser keeps the key and drives the new device from now on
    const session = useSessionStore.getState()
    expect(session.devices).toEqual([{ device_id: issued.device_id, browser_key: issued.browser_key, label: 'studio-01' }])
    expect(session.deviceId).toBe(issued.device_id)
    expect(session.browserKey).toBe(issued.browser_key)
  })

  it('keeps waiting while the code is unredeemed, and gives up after ten minutes', async () => {
    const api = fakeApi([offline])                           // 200, online false: nobody has redeemed it yet
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('slow')

    const s = flow.store.getState()
    expect(s.gaveUp).toBe(true)
    expect(s.waiting).toBe(false)
    expect(s.pairError).toBe('')
    expect(api.status).toHaveBeenCalledTimes(200)            // 600 s / 3 s
    expect(useSessionStore.getState().deviceId).toBe('')     // nothing selected
    // the key is kept anyway: the designer may still redeem the code before it expires
    expect(useSessionStore.getState().devices).toHaveLength(1)
  })

  it('stops when the pairing expired and the host purged it (404)', async () => {
    const api = fakeApi([offline, new BridgeApiError(404, JSON.stringify({ error: 'unknown_device' }))])
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('late')

    expect(api.status).toHaveBeenCalledTimes(2)
    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false, gaveUp: false, pairError: CODE_EXPIRED })
    expect(useSessionStore.getState().deviceId).toBe('')
  })

  it('stops and says why when the key is not that device\'s (403)', async () => {
    const api = fakeApi([new BridgeApiError(403, JSON.stringify({ error: 'invalid_device_key', message: 'not this device' }))])
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('wrong')

    expect(api.status).toHaveBeenCalledTimes(1)
    const s = flow.store.getState()
    expect(s).toMatchObject({ waiting: false, gaveUp: false, pairError: 'invalid_device_key: not this device' })
    expect(s.pairing).toEqual(issued)      // the code stays on screen: it may still be good
  })

  it('a 403 after the code expired reads as expired, not as a wrong key', async () => {
    // a host that refuses to say which device an id belongs to answers 403 for a purged pairing
    const soon = { ...issued, expires_at: new Date(2000).toISOString() }
    const api = fakeApi([new BridgeApiError(403, JSON.stringify({ error: 'invalid_device_key' }))], { pair: vi.fn().mockResolvedValue(soon) })
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('purged')

    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false, pairError: CODE_EXPIRED })
  })

  it('stops once the code is past its expiry, even while the host still answers', async () => {
    // the clock starts at 0 and moves 3 s per poll: this code dies during the third
    const soon = { ...issued, expires_at: new Date(7000).toISOString() }
    const api = fakeApi([offline], { pair: vi.fn().mockResolvedValue(soon) })
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('slowpoke')

    expect(api.status).toHaveBeenCalledTimes(3)
    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false, gaveUp: false, pairError: CODE_EXPIRED })
  })

  it('keeps polling when the host itself cannot be reached', async () => {
    const api = fakeApi([new Error('502: backend unreachable'), new Error('502: backend unreachable'), online])
    const clock = fakeClock()
    const flow = createPairingFlow({ api, wait: clock.wait, now: clock.now })

    await flow.pair('flaky')

    expect(api.status).toHaveBeenCalledTimes(3)
    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false, pairError: '' })
    expect(useSessionStore.getState().deviceId).toBe(issued.device_id)
  })

  it('reports a refused pairing and stops', async () => {
    const api = fakeApi([offline], {
      pair: vi.fn().mockRejectedValue(new BridgeApiError(429, JSON.stringify({ error: 'rate_limited', message: 'try again in a minute' }))),
    })
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })
    await flow.pair('x')
    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false, pairError: 'rate_limited: try again in a minute' })
    expect(api.status).not.toHaveBeenCalled()
    expect(useSessionStore.getState().devices).toEqual([])
  })

  it('stops waiting when the designer cancels', async () => {
    const api = fakeApi([offline])
    const clock = fakeClock()
    const flow = createPairingFlow({
      api,
      wait: async (ms: number) => { clock.advance(ms); flow.cancelPairing() },   // cancelled during the first wait
      now: clock.now,
    })
    await flow.pair('gone')
    expect(api.status).not.toHaveBeenCalled()
    expect(flow.store.getState()).toMatchObject({ pairing: null, waiting: false })
  })
})

describe('the device list', () => {
  it('asks about every remembered device with its own key', async () => {
    const api = fakeApi([online])
    useSessionStore.getState().rememberDevice({ device_id: 'dev_one', browser_key: 'k1', label: 'one' })
    useSessionStore.getState().rememberDevice({ device_id: 'dev_two', browser_key: 'k2', label: 'two' })
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })

    await flow.refresh()

    expect(api.statusCalls).toEqual([['dev_one', 'k1'], ['dev_two', 'k2']])
    expect(Object.keys(flow.store.getState().statuses)).toEqual(['dev_one', 'dev_two'])
  })

  it('forgets a device the host no longer accepts (revoked elsewhere)', async () => {
    const api = fakeApi([new BridgeApiError(403, JSON.stringify({ error: 'invalid_device_key' }))])
    useSessionStore.getState().rememberDevice({ device_id: 'dev_gone', browser_key: 'k', label: 'gone' })
    useSessionStore.getState().selectDevice('dev_gone')
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })

    await flow.refresh()

    expect(useSessionStore.getState().devices).toEqual([])
    expect(useSessionStore.getState().deviceId).toBe('')      // back to the local add-in
    expect(flow.store.getState().listError).toBe('')
  })

  it('keeps the device when the host itself is unreachable', async () => {
    const api = fakeApi([new Error('503: backend unreachable')])
    useSessionStore.getState().rememberDevice({ device_id: 'dev_one', browser_key: 'k1', label: 'one' })
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })
    await flow.refresh()
    expect(useSessionStore.getState().devices).toHaveLength(1)
    expect(flow.store.getState().listError).toBe('503: backend unreachable')
  })

  it('selecting a device sets the headers, and the local option clears them', () => {
    const flow = createPairingFlow({ api: fakeApi([online]), wait: async () => {}, now: () => 0 })
    useSessionStore.getState().rememberDevice({ device_id: 'dev_one', browser_key: 'k1', label: 'one' })
    flow.select('dev_one')
    expect(useSessionStore.getState()).toMatchObject({ deviceId: 'dev_one', browserKey: 'k1' })
    flow.select('')
    expect(useSessionStore.getState()).toMatchObject({ deviceId: '', browserKey: '' })
  })

  it('revoking closes the device and forgets it here', async () => {
    const api = fakeApi([online])
    const device = { device_id: 'dev_one', browser_key: 'k1', label: 'one' }
    useSessionStore.getState().rememberDevice(device)
    useSessionStore.getState().selectDevice('dev_one')
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })
    await flow.refresh()

    await flow.revoke(device)

    expect(api.revoke).toHaveBeenCalledWith('dev_one', 'k1')
    expect(useSessionStore.getState().devices).toEqual([])
    expect(useSessionStore.getState().deviceId).toBe('')
    expect(flow.store.getState().statuses.dev_one).toBeUndefined()
    expect(flow.store.getState().busy).toEqual({})
  })

  it('a device already revoked on the host is still forgotten here', async () => {
    const api = fakeApi([online], { revoke: vi.fn().mockRejectedValue(new BridgeApiError(404, JSON.stringify({ error: 'unknown_device' }))) })
    const device = { device_id: 'dev_one', browser_key: 'k1', label: 'one' }
    useSessionStore.getState().rememberDevice(device)
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })
    await flow.revoke(device)
    expect(useSessionStore.getState().devices).toEqual([])
    expect(flow.store.getState().listError).toBe('')
  })
})

describe('the admin view', () => {
  it('lists every device and revokes one with the admin token', async () => {
    const all = [{ ...online, device_id: 'dev_other', label: 'other' }]
    const api = fakeApi([online], { all: vi.fn().mockResolvedValue(all) })
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })

    await flow.loadAdmin()
    expect(flow.store.getState().admin).toEqual(all)

    await flow.adminRevoke('dev_other')
    expect(api.revoke).toHaveBeenCalledWith('dev_other', '')
    expect(api.all).toHaveBeenCalledTimes(2)
  })

  it('says why the list is refused without an admin token', async () => {
    const api = fakeApi([online], { all: vi.fn().mockRejectedValue(new BridgeApiError(403, JSON.stringify({ error: 'admin_required' }))) })
    const flow = createPairingFlow({ api, wait: async () => {}, now: () => 0 })
    await flow.loadAdmin()
    expect(flow.store.getState()).toMatchObject({ admin: null, adminError: 'admin_required' })
  })
})

describe('the display helpers', () => {
  it('counts down to the code\'s expiry', () => {
    const now = Date.parse('2026-09-23T09:00:00Z')
    expect(timeLeft('2026-09-23T09:09:58Z', now)).toBe('9:58')
    expect(timeLeft('2026-09-23T09:00:05Z', now)).toBe('0:05')
    expect(timeLeft('2026-09-23T08:59:59Z', now)).toBe('expired')
  })

  it('ages last_seen', () => {
    const now = Date.parse('2026-09-23T09:00:00Z')
    expect(lastSeen(null, now)).toBe('never')
    expect(lastSeen('2026-09-23T08:59:50Z', now)).toBe('just now')
    expect(lastSeen('2026-09-23T08:45:00Z', now)).toBe('15 min ago')
    expect(lastSeen('2026-09-23T04:00:00Z', now)).toBe('5 h ago')
    expect(lastSeen('2026-09-20T09:00:00Z', now)).toBe('3 d ago')
  })
})
