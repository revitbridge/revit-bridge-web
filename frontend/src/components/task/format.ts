/* Display helpers shared by the task, capabilities and evidence components. */

const TOKEN_PREFIX = 6

export function showValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return '-'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

/* The first characters of the token: enough to match the ledger's token_prefix, never the whole secret. */
export function tokenPrefix(token: string): string {
  return `${token.slice(0, TOKEN_PREFIX)}...`
}

/* "in 9 min 30 s" / "expired"; the caller passes the clock. */
export function expiresIn(expiresAt: string, now: number): string {
  const left = Date.parse(expiresAt) - now
  if (Number.isNaN(left)) return expiresAt
  if (left <= 0) return 'expired'
  const min = Math.floor(left / 60000)
  const sec = Math.floor((left % 60000) / 1000)
  return min > 0 ? `in ${min} min ${sec} s` : `in ${sec} s`
}
