/* Runtime configuration: fetched from /config.json before the app renders.
   One build serves any address; nothing about the deployment is baked in. */

/* local: this machine only - no pairing, no devices, no admin password; the pages
   talk to the add-in on this machine. cloud: pairing, the device list and the
   admin password. A config that does not say (an older host, a static
   config.json written before modes) reads as cloud: the pages as they were. */
export type DeployMode = 'local' | 'cloud'

export interface RuntimeFeatures {
  mode: DeployMode
  byoModel: boolean
  serverModel: boolean
  admin: boolean
  maxDevices: number
}

export interface RuntimeConfig {
  apiBase: string
  wsBase: string          // the relay address the server puts in install_command / ws_url
  features: RuntimeFeatures
}

const DEFAULTS: RuntimeConfig = {
  apiBase: '',
  wsBase: '',
  features: { mode: 'cloud', byoModel: true, serverModel: false, admin: false, maxDevices: 20 },
}

let current: RuntimeConfig = DEFAULTS

/* Parse the body of /config.json. A static host without the file answers the
   SPA fallback (index.html) with 200, which must not silently become defaults. */
export function parseRuntimeConfig(body: string, contentType = ''): RuntimeConfig {
  if (body.trimStart().startsWith('<') || (contentType && !contentType.includes('json'))) {
    throw new Error('/config.json is not JSON (a static host answered with a page instead of the file)')
  }
  const data = JSON.parse(body)
  const features = { ...DEFAULTS.features, ...(data.features || {}) }
  return {
    apiBase: String(data.apiBase || '').replace(/\/+$/, ''),
    wsBase: String(data.wsBase || '').replace(/\/+$/, ''),
    features: { ...features, mode: features.mode === 'local' ? 'local' : 'cloud' },
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

/* A local host: no device headers, no admin token, no pairing on the Connect page. */
export function isLocalHost(): boolean {
  return current.features.mode === 'local'
}
