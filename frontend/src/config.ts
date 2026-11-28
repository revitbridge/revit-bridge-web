/* Runtime configuration: fetched from /config.json before the app renders.
   One build serves any address; nothing about the deployment is baked in. */

/* local: this machine only - no pairing, no devices, no admin password; the pages
   talk to the add-in on this machine. cloud: pairing, the device list and the
   admin password. A config that does not say (an older host, a static
   config.json written before modes) reads as cloud: the pages as they were. */
export type DeployMode = 'local' | 'cloud'

/* The model NAME of each role (no address, no key); null where none is configured. */
export const ROLES = ['analyst', 'planner', 'supervisor'] as const
export type RoleName = typeof ROLES[number]
export type RoleModels = Record<RoleName, string | null> & { optimizer?: string | null }
export const SELECTOR_ROLES = ['planner', 'supervisor', 'analyst', 'optimizer'] as const
export type SelectionRole = typeof SELECTOR_ROLES[number]
export type ModelSelection = Record<SelectionRole, string | null>
export type LibraryEntry = { name: string; model: string; source: 'file' | 'saved'; vision: boolean }

/* How the chat runs a request (components/task/chatMode.ts): auto is the host's default. */
export const CHAT_MODES = ['auto', 'single', 'roles', 'roles_analyst'] as const
export type ChatMode = typeof CHAT_MODES[number]
export type ResolvedMode = Exclude<ChatMode, 'auto'>

export interface RuntimeFeatures {
  runModes?: ('confirm' | 'auto')[]
  authoring?: boolean
  autoRun?: { max_steps: number; excluded_tools: string[] }
  mode: DeployMode
  byoModel: boolean
  serverModel: boolean
  admin: boolean
  maxDevices: number
  models: RoleModels
  modelLibrary: LibraryEntry[]
  modelSelection: ModelSelection
  modelsFile: boolean     // the host loaded a deployment models file
  roles: boolean          // the chat runs in roles by default; false: one model loop (LLM_SINGLE_MODEL=1, or an older host)
  chatModes: ChatMode[]   // the modes this browser may pick (none on an older host)
  chatModeDefault: ResolvedMode | null    // what auto runs on this host (null: an older host)
}

export interface RuntimeConfig {
  apiBase: string
  wsBase: string          // the relay address the server puts in install_command / ws_url
  features: RuntimeFeatures
}

const NO_MODELS: ModelSelection = { analyst: null, planner: null, supervisor: null, optimizer: null }

const DEFAULTS: RuntimeConfig = {
  apiBase: '',
  wsBase: '',
  features: {
    mode: 'cloud', byoModel: true, serverModel: false, admin: false, maxDevices: 20, models: NO_MODELS, modelsFile: false,
    modelLibrary: [], modelSelection: NO_MODELS, roles: false, chatModes: [], chatModeDefault: null,
  },
}

const isChatMode = (value: unknown): value is ChatMode =>
  typeof value === 'string' && (CHAT_MODES as readonly string[]).includes(value)

/* features.chatModes as the host wrote it: the known modes, each once, in the host's order. */
function readChatModes(raw: unknown): ChatMode[] {
  if (!Array.isArray(raw)) return []
  return [...new Set(raw.filter(isChatMode))]
}

function readChatModeDefault(raw: unknown): ResolvedMode | null {
  return isChatMode(raw) && raw !== 'auto' ? raw : null
}

/* features.models as the host wrote it: a text name per role, anything else (an older host
   without the field, a value that is not text) is no name. */
function readRoleModels(raw: unknown): ModelSelection {
  const source = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const models = { ...NO_MODELS }
  for (const role of SELECTOR_ROLES) {
    const name = source[role]
    models[role] = typeof name === 'string' && name ? name : null
  }
  return models
}

function readModels(raw: unknown): RoleModels {
  const { optimizer, ...chat } = readRoleModels(raw)
  // An older host only advertises the three chat roles.
  return raw && typeof raw === 'object' && 'optimizer' in raw ? { ...chat, optimizer } : chat
}

const fileName = /^[A-Za-z0-9_.-]{1,16}$/ // models_file.SHOWN_ENTRY_MAX
const savedName = /^[A-Za-z][A-Za-z0-9_-]{0,39}$/
const hiddenSelection = /^<(?:entry #[1-9][0-9]*|missing entry)>$/

function readSelection(raw: unknown): ModelSelection {
  const selection = readRoleModels(raw)
  for (const role of SELECTOR_ROLES) {
    const name = selection[role]
    if (name && !fileName.test(name) && !savedName.test(name) && !hiddenSelection.test(name)) selection[role] = null
  }
  return selection
}

export function readLibrary(raw: unknown): LibraryEntry[] {
  if (!Array.isArray(raw)) return []
  const seen = new Set<string>()
  const entries: LibraryEntry[] = []
  for (const item of raw) {
    if (!item || typeof item.name !== 'string' || typeof item.model !== 'string'
      || (item.model !== '' && !item.model.trim())
      || !['file', 'saved'].includes(item.source) || typeof item.vision !== 'boolean'
      || !(item.source === 'file' ? fileName : savedName).test(item.name)
      || seen.has(item.name)) continue
    seen.add(item.name)
    const { name, model, source, vision } = item
    entries.push({ name, model, source, vision })
  }
  return entries
}

let current: RuntimeConfig = DEFAULTS
// A host only publishes effective names. Remember a role's default when it is visible.
let defaults: Partial<ModelSelection> = {}
function rememberDefaults(features: RuntimeFeatures) {
  for (const role of SELECTOR_ROLES) {
    if (!features.modelSelection[role]) defaults[role] = features.models[role] ?? null
  }
}

export function defaultModel(role: SelectionRole) {
  if (role === 'optimizer' && !current.features.modelsFile) return current.features.models.planner
  return defaults[role]
}

const listeners = new Set<() => void>()

export function subscribeConfig(listener: () => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function updateModels(serverModel: boolean, models: ModelSelection, modelLibrary: LibraryEntry[],
  modelSelection: ModelSelection, defaultsChanged = false) {
  if (defaultsChanged) defaults = {}
  else if (models.planner !== current.features.models.planner) delete defaults.optimizer
  current = { ...current, features: { ...current.features, serverModel, models, modelLibrary, modelSelection } }
  rememberDefaults(current.features)
  listeners.forEach(listener => listener())
}

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
    features: {
      ...features, mode: features.mode === 'local' ? 'local' : 'cloud',
      serverModel: features.serverModel === true,
      authoring: features.authoring === true,
      models: readModels(data.features?.models), modelsFile: features.modelsFile === true,
      modelLibrary: readLibrary(features.modelLibrary), modelSelection: readSelection(features.modelSelection),
      roles: features.roles === true,
      runModes: Array.isArray(features.runModes) && features.runModes.includes('auto') ? ['confirm', 'auto'] : ['confirm'],
      chatModes: readChatModes(features.chatModes), chatModeDefault: readChatModeDefault(features.chatModeDefault),
    },
  }
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const resp = await fetch('/config.json', { cache: 'no-store' })
    if (resp.ok) {
      current = parseRuntimeConfig(await resp.text(), resp.headers.get('content-type') || '')
      if (current.apiBase) {
        // The static shell supplies the addresses. Model configuration and the
        // browser-key policy can change on its API host between page loads.
        if (current.features.serverModel) {
          // A stale static flag must neither clear the tab key nor start sending it.
          current = { ...current, features: { ...current.features, serverModel: false, byoModel: false } }
        }
        const live = await fetch(current.apiBase + '/config.json', {
          cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(5000),
        })
        if (live.ok) {
          const { serverModel, byoModel, models, modelsFile, modelLibrary, modelSelection } = parseRuntimeConfig(
            await live.text(), live.headers.get('content-type') || '',
          ).features
          current = { ...current, features: { ...current.features, serverModel, byoModel, models, modelsFile, modelLibrary, modelSelection } }
        }
      }
    } else {
      console.warn(`/config.json answered ${resp.status}; using same-origin defaults`)
    }
  } catch (e) {
    // Same-origin defaults keep the app usable; the console says why they apply
    // (split deployment: put a static config.json next to index.html).
    console.warn(`runtime config unavailable: ${e instanceof Error ? e.message : String(e)}`)
  }
  defaults = {}
  rememberDefaults(current.features)
  return current
}

export function getConfig(): RuntimeConfig {
  return current
}

/* A local host: no device headers, no admin token, no pairing in Settings. */
export function isLocalHost(): boolean {
  return current.features.mode === 'local'
}
