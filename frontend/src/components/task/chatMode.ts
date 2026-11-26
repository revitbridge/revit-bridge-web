/* How the chat runs a request: one model loop, planner + supervisor, analyst + planner +
   supervisor, or the host's own default (auto). /config.json says which this browser may pick
   (features.chatModes) and what auto is (features.chatModeDefault); the pick is remembered in
   this browser (localStorage) and goes with every chat request as `mode`. With fewer than two
   modes to pick (a cloud host for a visitor, an older host) there is no choice: nothing is sent
   and the host runs its default. */

import { uiText } from '../../utils/strings'
import { create } from 'zustand'
import { CHAT_MODES, getConfig, type ChatMode, type ResolvedMode, type RuntimeFeatures } from '../../config'

export { CHAT_MODES, type ChatMode, type ResolvedMode }

export const MODE_LABELS: Record<ChatMode, string> = {
  auto: 'Auto',
  single: 'Single model',
  roles: 'Planner + supervisor',
  roles_analyst: 'Analyst + planner + supervisor',
}

export const isChatMode = (value: unknown): value is ChatMode =>
  typeof value === 'string' && (CHAT_MODES as readonly string[]).includes(value)

export const CHAT_MODE_KEY = 'rb_chat_mode'

/* The remembered pick; 'auto' when there is none or storage cannot be read. */
export function loadChatMode(): ChatMode {
  try {
    const value = localStorage.getItem(CHAT_MODE_KEY)
    return isChatMode(value) ? value : 'auto'
  } catch {
    return 'auto'
  }
}

export function saveChatMode(mode: ChatMode): void {
  try {
    if (mode === 'auto') localStorage.removeItem(CHAT_MODE_KEY)
    else localStorage.setItem(CHAT_MODE_KEY, mode)
  } catch { /* storage refused: the pick lasts for this page only */ }
}

/* The modes this browser may pick: shown only with two or more. */
export function pickableModes(features: Pick<RuntimeFeatures, 'chatModes'>): ChatMode[] {
  return features.chatModes.length >= 2 ? features.chatModes : []
}

/* The `mode` to send: the pick when the host offers it, else auto; undefined (nothing sent)
   when there is nothing to pick. */
export function requestMode(features: Pick<RuntimeFeatures, 'chatModes'>, picked: ChatMode): ChatMode | undefined {
  const modes = pickableModes(features)
  if (modes.length === 0) return undefined
  return modes.includes(picked) ? picked : 'auto'
}

/* An option's label; Auto names what it runs on this host. */
export function modeLabel(mode: ChatMode, resolved: ResolvedMode | null): string {
  if (mode === 'auto') return resolved ? `${uiText(MODE_LABELS.auto)} (${uiText(MODE_LABELS[resolved])})` : uiText(MODE_LABELS.auto)
  return uiText(MODE_LABELS[mode])
}

interface ChatModeState {
  mode: ChatMode
  setMode: (mode: ChatMode) => void
}

export const useChatMode = create<ChatModeState>(set => ({
  mode: loadChatMode(),
  setMode: (mode) => { saveChatMode(mode); set({ mode }) },
}))

/* What the task page sends now (read when the request goes out). */
export function currentRequestMode(): ChatMode | undefined {
  return requestMode(getConfig().features, useChatMode.getState().mode)
}
