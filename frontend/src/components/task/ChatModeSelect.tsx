/* The chat mode next to the chat input: a compact menu of the modes this host lets the
   browser pick (features.chatModes), Auto naming what it runs here. Nothing is drawn with fewer
   than two modes to pick. */

import type { ChatMode, RuntimeFeatures } from '../../config'
import { isChatMode, modeLabel, pickableModes } from './chatMode'

interface Props {
  features: Pick<RuntimeFeatures, 'chatModes' | 'chatModeDefault'>
  mode: ChatMode
  onChange: (mode: ChatMode) => void
  disabled?: boolean
}

export default function ChatModeSelect({ features, mode, onChange, disabled }: Props) {
  const modes = pickableModes(features)
  if (modes.length === 0) return null
  const value = modes.includes(mode) ? mode : 'auto'
  return (
    <label className="chat-mode" title="How the chat runs your requests: one model, or roles with their own models">
      <span className="chat-mode-label">Mode</span>
      <select className="chat-mode-select" value={value} disabled={disabled}
        onChange={e => { if (isChatMode(e.target.value)) onChange(e.target.value) }}>
        {modes.map(m => <option key={m} value={m}>{modeLabel(m, features.chatModeDefault)}</option>)}
      </select>
    </label>
  )
}
