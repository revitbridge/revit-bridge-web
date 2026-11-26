/* Display helpers of the Connect page. */

import { uiText } from '../../utils/strings'
/* "2 min ago" / "just now" / "never"; the server sends an ISO timestamp. */
export function lastSeen(value: string | null | undefined, now: number): string {
  if (!value) return uiText("never")
  const at = Date.parse(value)
  if (Number.isNaN(at)) return value
  const seconds = Math.max(0, Math.floor((now - at) / 1000))
  if (seconds < 45) return uiText("just now")
  if (seconds < 3600) return uiText("{0} min ago", Math.floor(seconds / 60))
  if (seconds < 86400) return uiText("{0} h ago", Math.floor(seconds / 3600))
  return uiText("{0} d ago", Math.floor(seconds / 86400))
}

/* "9:58" until a pairing code expires, or "expired". */
export function timeLeft(expiresAt: string, now: number): string {
  const left = Date.parse(expiresAt) - now
  if (Number.isNaN(left)) return expiresAt
  if (left <= 0) return uiText("expired")
  const total = Math.floor(left / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}
