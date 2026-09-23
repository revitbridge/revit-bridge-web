/* Per-tab session state. Everything secret (model key, browser keys, admin token)
   lives in sessionStorage only: gone when the tab closes, never sent anywhere
   but the request headers of this host.

   A paired Revit is a device: the browser holds its key, the add-in holds its
   own token, and neither can stand in for the other. The selected device rides
   on every request as X-Device-Id / X-Device-Key; no device selected means the
   local add-in over TCP. */

import { create } from 'zustand'

const KEYS = {
  deviceId: 'rb_device_id',
  browserKey: 'rb_browser_key',
  devices: 'rb_devices',
  llmBaseUrl: 'rb_llm_base_url',
  llmModel: 'rb_llm_model',
  llmKey: 'rb_llm_key',
  adminToken: 'rb_admin_token',
} as const

function read(key: string): string {
  try { return sessionStorage.getItem(key) || '' } catch { return '' }
}

function write(key: string, value: string) {
  try {
    if (value) sessionStorage.setItem(key, value)
    else sessionStorage.removeItem(key)
  } catch { /* private mode: state stays in memory for this tab */ }
}

/* What this browser knows about a device it paired: enough to drive and revoke it. */
export interface PairedDevice {
  device_id: string
  browser_key: string
  label: string
}

function readDevices(): PairedDevice[] {
  try {
    const parsed: unknown = JSON.parse(read(KEYS.devices) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed.filter((d): d is PairedDevice =>
      !!d && typeof d === 'object' && typeof (d as PairedDevice).device_id === 'string'
        && typeof (d as PairedDevice).browser_key === 'string')
      .map(d => ({ device_id: d.device_id, browser_key: d.browser_key, label: String(d.label || '') }))
  } catch {
    return []
  }
}

function writeDevices(devices: PairedDevice[]) {
  write(KEYS.devices, devices.length ? JSON.stringify(devices) : '')
}

interface SessionState {
  deviceId: string          // '' = the local add-in over TCP
  browserKey: string
  devices: PairedDevice[]   // the devices this browser holds keys for
  llmBaseUrl: string
  llmModel: string
  llmKey: string
  adminToken: string
  chatSessionId: string
  selectDevice: (deviceId: string) => void
  rememberDevice: (device: PairedDevice) => void
  forgetDevice: (deviceId: string) => void
  setLlm: (patch: Partial<Pick<SessionState, 'llmBaseUrl' | 'llmModel' | 'llmKey'>>) => void
  setAdminToken: (token: string) => void
  setChatSessionId: (id: string) => void
}

export const useSessionStore = create<SessionState>((set, get) => ({
  deviceId: read(KEYS.deviceId),
  browserKey: read(KEYS.browserKey),
  devices: readDevices(),
  llmBaseUrl: read(KEYS.llmBaseUrl),
  llmModel: read(KEYS.llmModel),
  llmKey: read(KEYS.llmKey),
  adminToken: read(KEYS.adminToken),
  chatSessionId: '',

  /* Selecting a device takes its key from the list; '' goes back to the local add-in. */
  selectDevice: (deviceId) => {
    const device = get().devices.find(d => d.device_id === deviceId)
    const browserKey = device?.browser_key || ''
    write(KEYS.deviceId, device ? deviceId : '')
    write(KEYS.browserKey, browserKey)
    set({ deviceId: device ? deviceId : '', browserKey })
  },

  rememberDevice: (device) => {
    const devices = [...get().devices.filter(d => d.device_id !== device.device_id), device]
    writeDevices(devices)
    set({ devices })
  },

  forgetDevice: (deviceId) => {
    const devices = get().devices.filter(d => d.device_id !== deviceId)
    writeDevices(devices)
    const selected = get().deviceId === deviceId
    if (selected) {
      write(KEYS.deviceId, '')
      write(KEYS.browserKey, '')
    }
    set(selected ? { devices, deviceId: '', browserKey: '' } : { devices })
  },

  setLlm: (patch) => {
    if (patch.llmBaseUrl !== undefined) write(KEYS.llmBaseUrl, patch.llmBaseUrl)
    if (patch.llmModel !== undefined) write(KEYS.llmModel, patch.llmModel)
    if (patch.llmKey !== undefined) write(KEYS.llmKey, patch.llmKey)
    set(patch)
  },
  setAdminToken: (adminToken) => { write(KEYS.adminToken, adminToken); set({ adminToken }) },
  setChatSessionId: (chatSessionId) => set({ chatSessionId }),
}))

/* Headers derived from the session, used by the API client. */
export function deviceHeaders(): Record<string, string> {
  const { deviceId, browserKey } = useSessionStore.getState()
  if (!deviceId || !browserKey) return {}     // no device: the host's local add-in
  return { 'X-Device-Id': deviceId, 'X-Device-Key': browserKey }
}

export function llmHeaders(): Record<string, string> {
  const { llmBaseUrl, llmModel, llmKey } = useSessionStore.getState()
  const h: Record<string, string> = {}
  if (llmBaseUrl) h['X-LLM-Base-Url'] = llmBaseUrl
  if (llmModel) h['X-LLM-Model'] = llmModel
  if (llmKey) h['X-LLM-Key'] = llmKey
  return h
}

export function adminHeaders(): Record<string, string> {
  const { adminToken } = useSessionStore.getState()
  return adminToken ? { 'X-Admin-Token': adminToken } : {}
}
