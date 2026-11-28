import { SELECTOR_ROLES, updateModels, type LibraryEntry, type RoleName, type SelectionRole } from '../../config'
import { cleanBrowserKey } from '../../store'

export const endpoint = '/api/v1/settings/models'
export type Scope = 'shared' | RoleName
type Summary = { base_url: string; model: string; key_set: boolean; key_tail: string | null }
export type LibraryModel = LibraryEntry & Summary & { shadowed: boolean; api: 'chat' | 'responses' }
export type ModelDetails = {
  configured: boolean
  saved: Partial<Record<Scope, Summary>>
  models: Record<SelectionRole, Summary>
  library?: LibraryModel[]
  selection?: Record<SelectionRole, { entry: string | null; selected_missing: boolean }>
}

const listeners = new Set<(details: ModelDetails) => void>()
export function subscribeModelDetails(listener: (details: ModelDetails) => void) {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

export function applyModelDetails(details: ModelDetails, defaultsChanged = false) {
  const library = details.library ?? []
  const collisions = new Set(library.filter(entry => entry.shadowed).map(entry => entry.name))
  const entries = library.filter(entry => entry.name && !collisions.has(entry.name))
    .map(({ name, model, source, vision }) => ({ name, model, source, vision }))
  const models: Record<SelectionRole, string | null> = { planner: null, analyst: null, supervisor: null, optimizer: null }
  const selection = { ...models }
  for (const role of SELECTOR_ROLES) {
    models[role] = details.models[role]?.model || null
    selection[role] = details.selection?.[role]?.entry ?? null
  }
  updateModels(details.configured, models, entries, selection, defaultsChanged)
  cleanBrowserKey()
  listeners.forEach(listener => listener(details))
}
