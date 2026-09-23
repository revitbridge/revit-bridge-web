/* Display helpers of the Connect page. */

/* "2 min ago" / "just now" / "never"; the server sends an ISO timestamp. */
export function lastSeen(value: string | null | undefined, now: number): string {
  if (!value) return 'never'
  const at = Date.parse(value)
  if (Number.isNaN(at)) return value
  const seconds = Math.max(0, Math.floor((now - at) / 1000))
  if (seconds < 45) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`
  return `${Math.floor(seconds / 86400)} d ago`
}

/* "9:58" until a pairing code expires, or "expired". */
export function timeLeft(expiresAt: string, now: number): string {
  const left = Date.parse(expiresAt) - now
  if (Number.isNaN(left)) return expiresAt
  if (left <= 0) return 'expired'
  const total = Math.floor(left / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}
