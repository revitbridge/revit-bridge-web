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

/* Parse the body of /config.json. A static host without the file answers the
   SPA fallback (index.html) with 200, which must not silently become defaults. */
export function parseRuntimeConfig(body: string, contentType = ''): RuntimeConfig {
  if (body.trimStart().startsWith('<') || (contentType && !contentType.includes('json'))) {
    throw new Error('/config.json is not JSON (a static host answered with a page instead of the file)')
  }
  const data = JSON.parse(body)
  return {
    apiBase: String(data.apiBase || '').replace(/\/+$/, ''),
    wsBase: String(data.wsBase || '').replace(/\/+$/, ''),
    features: { ...DEFAULTS.features, ...(data.features || {}) },
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const resp = await fetch('/config.json', { cache: 'no-store' })
    if (resp.ok) {
      current = parseRuntimeConfig(await resp.text(), resp.headers.get('content-type') || '')
    } else {
      console.warn(`/config.json answered ${resp.status}; using same-origin defaults`)
    }
  } catch (e) {
    // Same-origin defaults keep the app usable; the console says why they apply
    // (split deployment: put a static config.json next to index.html).
    console.warn(`runtime config unavailable, using same-origin defaults: ${e instanceof Error ? e.message : String(e)}`)
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
