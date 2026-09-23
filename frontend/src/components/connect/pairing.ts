/* The Connect page's device state: pair, wait for the add-in, drive, revoke.

   Transport and clock are injected (the device API, a wait function), so the
   whole three-step flow - code, install, online - runs under vitest without
   fetch and without real time. What the browser keeps of a device is its id,
   its key and its label, in sessionStorage through the session store; the key
   never goes anywhere but this host's request headers. */

import { createStore } from 'zustand/vanilla'
import type { DeviceStatus, PairingResponse } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import { useSessionStore, type PairedDevice } from '../../store'
import { BridgeApiError } from '../shared/http'
import { deviceApi, type DeviceApi } from './api'

export const POLL_INTERVAL_MS = 3000
export const POLL_LIMIT_MS = 10 * 60 * 1000

export interface PairingState {
  pairing: PairingResponse | null      // the code being waited on; the key is already remembered
  pairingLabel: string
  waiting: boolean                     // polling for the add-in to redeem the code
  pairError: string
  gaveUp: boolean                      // ten minutes without the add-in
  statuses: Record<string, DeviceStatus>
  listError: string
  busy: Record<string, string>         // device id -> what is happening to it
  admin: DeviceStatus[] | null
  adminError: string
}

export const initialPairingState = (): PairingState => ({
  pairing: null, pairingLabel: '', waiting: false, pairError: '', gaveUp: false,
  statuses: {}, listError: '', busy: {}, admin: null, adminError: '',
})

export interface PairingDeps {
  api?: DeviceApi
  wait?: (ms: number) => Promise<void>
  now?: () => number
}

export function createPairingFlow(deps: PairingDeps = {}) {
  const api = deps.api ?? deviceApi
  const wait = deps.wait ?? ((ms: number) => new Promise<void>(resolve => { setTimeout(resolve, ms) }))
  const now = deps.now ?? (() => Date.now())
  const store = createStore<PairingState>(() => initialPairingState())
  const { getState, setState } = store
  const session = () => useSessionStore.getState()

  const setBusy = (deviceId: string, what: string) => setState(s => {
    const busy = { ...s.busy }
    if (what) busy[deviceId] = what
    else delete busy[deviceId]
    return { busy }
  })

  /* Step 1: a code, an install command, and this browser's key for the device. */
  async function pair(label: string) {
    if (getState().waiting) return
    setState({ pairError: '', gaveUp: false, pairingLabel: label })
    let issued: PairingResponse
    try {
      issued = await api.pair(label.trim())
    } catch (e: unknown) {
      setState({ pairError: describe(e), pairing: null })
      return
    }
    // remember the key before the wait: a reload mid-pairing must not lose the device
    session().rememberDevice({ device_id: issued.device_id, browser_key: issued.browser_key, label: label.trim() })
    setState({ pairing: issued, waiting: true })
    await waitForAddin(issued)
  }

  /* Steps 2-3: the designer runs the install command (or types the code in the
     add-in's settings), the add-in redeems it and connects; we ask every three
     seconds for ten minutes. */
  async function waitForAddin(issued: PairingResponse) {
    const started = now()
    while (getState().pairing?.device_id === issued.device_id) {
      await wait(POLL_INTERVAL_MS)
      if (getState().pairing?.device_id !== issued.device_id) return      // cancelled
      let status: DeviceStatus
      try {
        status = await api.status(issued.device_id, issued.browser_key)
      } catch {
        // an unredeemed code may answer 404/403: that is the wait, not a failure
        if (now() - started >= POLL_LIMIT_MS) { setState({ waiting: false, gaveUp: true }); return }
        continue
      }
      setState(s => ({ statuses: { ...s.statuses, [issued.device_id]: status } }))
      if (status.online) {
        if (status.label) session().rememberDevice({ device_id: issued.device_id, browser_key: issued.browser_key, label: status.label })
        session().selectDevice(issued.device_id)                          // the new device becomes the one in use
        setState({ pairing: null, waiting: false })
        return
      }
      if (now() - started >= POLL_LIMIT_MS) {
        setState({ waiting: false, gaveUp: true })
        return
      }
    }
  }

  /* Stop waiting; the code stays valid until it expires and the key is already kept. */
  function cancelPairing() {
    setState({ pairing: null, waiting: false })
  }

  /* Where each remembered device stands right now. */
  async function refresh() {
    const devices = session().devices
    if (!devices.length) { setState({ statuses: {}, listError: '' }); return }
    const results = await Promise.all(devices.map(async (d): Promise<[string, DeviceStatus | null, string]> => {
      try {
        return [d.device_id, await api.status(d.device_id, d.browser_key), '']
      } catch (e: unknown) {
        // a device revoked elsewhere, or one this host no longer knows: drop it from the browser
        if (e instanceof BridgeApiError && (e.status === 404 || e.status === 403)) {
          session().forgetDevice(d.device_id)
          return [d.device_id, null, '']
        }
        return [d.device_id, null, describe(e)]
      }
    }))
    const statuses: Record<string, DeviceStatus> = {}
    let listError = ''
    for (const [id, status, error] of results) {
      if (status) statuses[id] = status
      if (error) listError = error
    }
    setState({ statuses, listError })
  }

  const select = (deviceId: string) => session().selectDevice(deviceId)

  /* Revoking closes the add-in's connection; the browser forgets the device either way. */
  async function revoke(device: PairedDevice) {
    setBusy(device.device_id, 'Revoking...')
    try {
      await api.revoke(device.device_id, device.browser_key)
    } catch (e: unknown) {
      if (!(e instanceof BridgeApiError && (e.status === 404 || e.status === 403))) {
        setState({ listError: describe(e) })
        setBusy(device.device_id, '')
        return
      }
    }
    session().forgetDevice(device.device_id)
    setState(s => {
      const statuses = { ...s.statuses }
      delete statuses[device.device_id]
      return { statuses, listError: '' }
    })
    setBusy(device.device_id, '')
    if (getState().admin) await loadAdmin()
  }

  /* The admin view: every device this host knows, with the same Revoke. */
  async function loadAdmin() {
    try {
      setState({ admin: await api.all(), adminError: '' })
    } catch (e: unknown) {
      setState({ admin: null, adminError: describe(e) })
    }
  }

  async function adminRevoke(deviceId: string) {
    setBusy(deviceId, 'Revoking...')
    try {
      await api.revoke(deviceId, '')            // the admin token authorises this one
      session().forgetDevice(deviceId)
    } catch (e: unknown) {
      setState({ adminError: describe(e) })
    } finally {
      setBusy(deviceId, '')
    }
    await loadAdmin()
  }

  return { store, pair, cancelPairing, refresh, select, revoke, loadAdmin, adminRevoke }
}

function describe(e: unknown): string {
  if (e instanceof BridgeApiError && e.code) return e.body?.message ? `${e.code}: ${e.body.message}` : e.code
  return getErrorMessage(e)
}

export type PairingFlow = ReturnType<typeof createPairingFlow>
