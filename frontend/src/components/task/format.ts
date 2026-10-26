/* Display helpers shared by the task, capabilities and evidence components. */

const TOKEN_PREFIX = 6

export function showValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return '-'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

const QUESTION_KINDS = new Map<string, { label: string; hint: string }>([
  ['user_intent', { label: 'designer', hint: 'only the designer can answer this' }],
  ['model_fact', { label: 'model fact', hint: 'not found in the model: check the model before answering' }],
  ['technical', { label: 'technical', hint: 'a technical question' }],
])

/* A question's kind as its badge: a short label and the tooltip, which also lists what
   the question holds up. An unknown kind shows as it is; no kind (a 0.3 server), no badge. */
export function questionBadge(kind: string | undefined, blocks?: readonly string[]): { label: string; title: string } | null {
  if (!kind) return null
  const known = QUESTION_KINDS.get(kind)
  const parts = [known?.hint ?? kind]
  if (blocks?.length) parts.push(`holds up: ${blocks.join(', ')}`)
  return { label: known?.label ?? kind.replace(/_/g, ' '), title: parts.join('; ') }
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
