/* Runtime configuration: fetched from /config.json before the app renders.
   One build serves any address; nothing about the deployment is baked in. */

export interface RuntimeFeatures {
  byoModel: boolean
  serverModel: boolean
  admin: boolean
  slotTokenRequired: boolean
  maxSlots: number
}

export interface RuntimeConfig {
  apiBase: string
  wsBase: string
  features: RuntimeFeatures
}

const DEFAULTS: RuntimeConfig = {
  apiBase: '',
  wsBase: '',
  features: { byoModel: true, serverModel: false, admin: false, slotTokenRequired: false, maxSlots: 5 },
}

let current: RuntimeConfig = DEFAULTS

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const resp = await fetch('/config.json', { cache: 'no-store' })
    if (resp.ok) {
      const data = await resp.json()
      current = {
        apiBase: String(data.apiBase || '').replace(/\/+$/, ''),
        wsBase: String(data.wsBase || '').replace(/\/+$/, ''),
        features: { ...DEFAULTS.features, ...(data.features || {}) },
      }
    }
  } catch {
    /* same-origin defaults keep the app usable */
  }
  return current
}

export function getConfig(): RuntimeConfig {
  return current
}

/* Where a Revit add-in should connect for a remote slot. */
export function addinWsEndpoint(): string {
  if (current.wsBase) return current.wsBase
  const origin = current.apiBase || window.location.origin
  return origin.replace(/^http/, 'ws') + '/api/v1/bridge/ws'
}
