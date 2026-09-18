/* Per-tab session state. Everything secret (model key, slot token, admin token)
   lives in sessionStorage only: gone when the tab closes, never sent anywhere
   but the request headers of this host. */

import { create } from 'zustand'

const KEYS = {
  slot: 'rb_slot',
  slotToken: 'rb_slot_token',
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

interface SessionState {
  slot: string
  slotToken: string
  llmBaseUrl: string
  llmModel: string
  llmKey: string
  adminToken: string
  chatSessionId: string
  setSlot: (slot: string) => void
  setSlotToken: (token: string) => void
  setLlm: (patch: Partial<Pick<SessionState, 'llmBaseUrl' | 'llmModel' | 'llmKey'>>) => void
  setAdminToken: (token: string) => void
  setChatSessionId: (id: string) => void
}

export const useSessionStore = create<SessionState>((set, get) => ({
  slot: read(KEYS.slot),
  slotToken: read(KEYS.slotToken),
  llmBaseUrl: read(KEYS.llmBaseUrl),
  llmModel: read(KEYS.llmModel),
  llmKey: read(KEYS.llmKey),
  adminToken: read(KEYS.adminToken),
  chatSessionId: '',
  setSlot: (slot) => {
    const changed = slot !== get().slot
    write(KEYS.slot, slot)
    if (changed) write(KEYS.slotToken, '')
    set(changed ? { slot, slotToken: '' } : { slot })
  },
  setSlotToken: (slotToken) => { write(KEYS.slotToken, slotToken); set({ slotToken }) },
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
export function slotHeaders(): Record<string, string> {
  const { slot, slotToken } = useSessionStore.getState()
  if (!slot) return {}
  return { 'X-Slot-Id': slot, ...(slotToken ? { 'X-Slot-Token': slotToken } : {}) }
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
