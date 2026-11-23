/* The Token page's arithmetic: the runs of /api/usage/runs as rows, per role, and two runs
   side by side. A count the provider did not report is null and shows as '-'; a sum or a
   difference with an unknown in it is unknown, never 0. */

import type { DeployMode } from '../../config'
import type { UsagePart, UsageRun } from '../../types/api'

export const METRICS = [
  { key: 'calls', label: 'Calls' },
  { key: 'input', label: 'Input' },
  { key: 'cache_read', label: 'Cache read' },
  { key: 'cache_write', label: 'Cache write' },
  { key: 'output', label: 'Output' },
  { key: 'ms', label: 'Time (ms)' },
] as const

export type Metric = typeof METRICS[number]['key']
export type Counts = Record<Metric, number | null>

export const isCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0

/* '-' for anything that is not a count. */
export function formatCount(value: number | null | undefined): string {
  return isCount(value) ? value.toLocaleString('en-US') : '-'
}

/* The counts of a run or of one role's part of it; whatever is not a count is unknown. */
export function countsOf(row: UsageRun | UsagePart): Counts {
  const counts = {} as Counts
  for (const { key } of METRICS) {
    const value: unknown = row[key]
    counts[key] = isCount(value) ? value : null
  }
  return counts
}

/* The sum, or null as soon as one value is unknown. */
export function sumCounts(values: Array<number | null | undefined>): number | null {
  let total = 0
  for (const value of values) {
    if (!isCount(value)) return null
    total += value
  }
  return total
}

export interface RoleTotal {
  role: string
  models: string[]       // the models the role ran on in this run, in order
  counts: Counts
}

/* One line per role (its models summed), in the order the run first used them. */
export function roleTotals(run: UsageRun): RoleTotal[] {
  const parts = new Map<string, UsagePart[]>()
  for (const part of run.by_role ?? []) {
    const list = parts.get(part.role) ?? []
    list.push(part)
    parts.set(part.role, list)
  }
  return [...parts].map(([role, list]) => {
    const counts = {} as Counts
    for (const { key } of METRICS) counts[key] = sumCounts(list.map(part => countsOf(part)[key]))
    return { role, models: list.map(part => part.model ?? '-'), counts }
  })
}

/* b - a, unknown when either is. */
export function difference(a: number | null, b: number | null): number | null {
  return isCount(a) && isCount(b) ? b - a : null
}

/* '+1,234 (+12.5%)', '-20 (-3.0%)', '0'; the percentage only against a known, non-zero a. */
export function formatDifference(a: number | null, b: number | null): string {
  const diff = difference(a, b)
  if (diff === null) return '-'
  if (diff === 0) return '0'
  const sign = diff > 0 ? '+' : '-'
  const text = `${sign}${Math.abs(diff).toLocaleString('en-US')}`
  return a ? `${text} (${sign}${(Math.abs(diff) / a * 100).toFixed(1)}%)` : text
}

export interface CompareRow {
  key: Metric
  label: string
  a: string
  b: string
  diff: string
  direction: 'up' | 'down' | 'same' | 'unknown'
}

export function compareRows(a: Counts, b: Counts): CompareRow[] {
  return METRICS.map(({ key, label }) => {
    const diff = difference(a[key], b[key])
    return {
      key, label, a: formatCount(a[key]), b: formatCount(b[key]), diff: formatDifference(a[key], b[key]),
      direction: diff === null ? 'unknown' : diff > 0 ? 'up' : diff < 0 ? 'down' : 'same',
    }
  })
}

export interface RoleComparison {
  role: string
  modelsA: string
  modelsB: string
  rows: CompareRow[]
}

const UNKNOWN: Counts = { calls: null, input: null, cache_read: null, cache_write: null, output: null, ms: null }

/* Each role either run has, A's order first. A role one run does not have shows '-' there. */
export function compareRoles(a: UsageRun, b: UsageRun): RoleComparison[] {
  const inA = roleTotals(a)
  const inB = roleTotals(b)
  const roles = [...new Set([...inA, ...inB].map(total => total.role))]
  return roles.map(role => {
    const left = inA.find(total => total.role === role)
    const right = inB.find(total => total.role === role)
    return {
      role,
      modelsA: left ? left.models.join(', ') : '-',
      modelsB: right ? right.models.join(', ') : '-',
      rows: compareRows(left?.counts ?? UNKNOWN, right?.counts ?? UNKNOWN),
    }
  })
}

/* Two runs at most: picking a third lets go of the first picked; picking one again unpicks it. */
export function toggleSelection(selected: readonly string[], label: string): string[] {
  if (selected.includes(label)) return selected.filter(item => item !== label)
  return [...selected, label].slice(-2)
}

/* 'first' or 'first - last' (UTC, as the host stores it). */
export function runTime(run: UsageRun): string {
  return run.first_at === run.last_at ? `${run.first_at} UTC` : `${run.first_at} - ${run.last_at} UTC`
}

export interface UsageGate {
  canRead: boolean
  askToken: boolean      // show the admin password field
  note: string
}

/* /api/usage is admin only on a cloud host (the logs' rule); a local host needs nothing. */
export function usageGate(mode: DeployMode, admin: boolean, adminToken: string): UsageGate {
  if (mode === 'local') return { canRead: true, askToken: false, note: '' }
  if (!admin) return { canRead: false, askToken: false, note: 'This host has no admin password configured, so its token usage cannot be read here.' }
  if (!adminToken) return { canRead: false, askToken: true, note: 'Token usage is admin only on this host: enter the admin password.' }
  return { canRead: true, askToken: true, note: '' }
}
