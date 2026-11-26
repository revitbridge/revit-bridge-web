// These reply fields come from revit_bridge/checking.py and dry_run.py.
export interface Diagnostic {
  id: string
  severity: string
  line: number | null
  column: number | null
  template_column?: number
  message: string
  symbol: string | null
  in_wrapper: boolean
  file?: string
}

export interface CheckReply {
  schema_version: number
  checked: boolean
  compiled: boolean | null
  blocked: boolean
  capability: string | null
  reason: string | null
  diagnostics: Diagnostic[]
  truncated: number
  fingerprint: string | null
  hints: { id: string; symbol: string | null; text: string }[]
  guidance: unknown[]
  environment: Record<string, unknown> | null
  warnings: string[]
}

export interface ChangePart {
  count?: number | null
  ids?: unknown[]
  by_category?: Record<string, unknown>
  [key: string]: unknown
}

export interface DryRunReply {
  success: boolean
  rolled_back: boolean | null
  result: unknown
  error: string | null
  diagnostics: Diagnostic[]
  // note: what the add-in says about its counts, kept as it sent it.
  changes: { added?: ChangePart; modified?: ChangePart; deleted?: ChangePart; note?: string } | null
  failures: unknown[]
  fingerprint: string | null
  evidence_id: string | null
  hint: string | null
  warnings: string[]
}

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value)
const text = (value: unknown): string | null => typeof value === 'string' ? value : null
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
const number = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) ? value : null

function diagnostics(value: unknown): Diagnostic[] {
  if (!Array.isArray(value)) return []
  return value.filter(object).map(item => ({
    id: text(item.id) ?? '', severity: text(item.severity) ?? 'error',
    line: number(item.line), column: number(item.column), message: text(item.message) ?? '',
    ...(typeof item.template_column === 'number' ? { template_column: item.template_column } : {}),
    symbol: text(item.symbol), in_wrapper: item.in_wrapper === true,
    ...(typeof item.file === 'string' ? { file: item.file } : {}),
  }))
}

export function readCheck(value: unknown): CheckReply {
  if (!object(value) || typeof value.checked !== 'boolean' || !(value.compiled === null || typeof value.compiled === 'boolean')) {
    throw new Error('Invalid check response.')
  }
  return {
    schema_version: number(value.schema_version) ?? 1,
    checked: value.checked, compiled: value.compiled, blocked: value.blocked === true,
    capability: text(value.capability), reason: text(value.reason),
    diagnostics: diagnostics(value.diagnostics), truncated: number(value.truncated) ?? 0,
    fingerprint: text(value.fingerprint),
    hints: Array.isArray(value.hints) ? value.hints.filter(object).map(item => ({ id: text(item.id) ?? '', symbol: text(item.symbol), text: text(item.text) ?? '' })) : [],
    guidance: Array.isArray(value.guidance) ? value.guidance : [],
    environment: object(value.environment) ? value.environment : null,
    warnings: strings(value.warnings),
  }
}

export function readDryRun(value: unknown): DryRunReply {
  if (!object(value) || typeof value.success !== 'boolean') throw new Error('Invalid dry-run response; rollback is not confirmed.')
  const changes = object(value.changes) ? value.changes : null
  return {
    success: value.success, rolled_back: typeof value.rolled_back === 'boolean' ? value.rolled_back : null,
    result: value.result ?? null, error: text(value.error), diagnostics: diagnostics(value.diagnostics),
    changes: changes ? {
      ...Object.fromEntries(['added', 'modified', 'deleted'].filter(kind => object(changes[kind])).map(kind => [kind, changes[kind]])),
      ...(typeof changes.note === 'string' && changes.note ? { note: changes.note } : {}),
    } : null,
    failures: Array.isArray(value.failures) ? value.failures : [], fingerprint: text(value.fingerprint),
    evidence_id: text(value.evidence_id), hint: text(value.hint), warnings: strings(value.warnings),
  }
}

// Refusals revit_bridge/dry_run.py answers before any code is sent to Revit, so there is no
// rollback to confirm: the sandbox (blocked), an add-in without the dry run (no_dry_run,
// no_dry_run_include), the include checks (include_*), and an add-in whose capabilities could not
// be read ("<kind>: <reason>", e.g. "unreachable: ..."). They carry rolled_back null and no ledger
// line. A request that was sent and got no reply also has rolled_back null, but always a hint and,
// unless the ledger failed (a warning says so), an evidence id: that one stays "not confirmed".
const NOT_SENT = new Set(['blocked', 'no_dry_run', 'no_dry_run_include'])

export function dryRunNotSent(result: DryRunReply): boolean {
  if (result.success || result.rolled_back !== null || result.evidence_id !== null) return false
  if (result.warnings.some(warning => warning.startsWith('evidence not recorded'))) return false
  const error = result.error ?? ''
  if (NOT_SENT.has(error) || /^include_[a-z_]+$/.test(error)) return true
  return result.hint === null && /^[a-z_]+: /.test(error)
}

// Textarea values and selection offsets use LF, including when loaded code uses CRLF.
export const normalizeCode = (code: string): string => code.replace(/\r\n?/g, '\n')

export function diagnosticRange(code: string, diagnostic: Pick<Diagnostic, 'line' | 'column' | 'template_column' | 'in_wrapper' | 'file'>): [number, number] | null {
  if (diagnostic.in_wrapper || diagnostic.file || diagnostic.line === null || !Number.isInteger(diagnostic.line) || diagnostic.line < 1) return null
  const lines = normalizeCode(code).split('\n')
  if (diagnostic.line > lines.length) return null
  const start = lines.slice(0, diagnostic.line - 1).reduce((sum, line) => sum + line.length + 1, 0)
  const column = Math.max(0, Math.min(lines[diagnostic.line - 1].length, (diagnostic.template_column ?? diagnostic.column ?? 1) - 1))
  return [start + column, start + lines[diagnostic.line - 1].length]
}

export function indentSelection(code: string, start: number, end: number): { code: string; start: number; end: number } {
  code = normalizeCode(code)
  if (start === end) return { code: code.slice(0, start) + '  ' + code.slice(end), start: start + 2, end: end + 2 }
  const lineStart = code.lastIndexOf('\n', start - 1) + 1
  const selection = code.slice(lineStart, end)
  const indented = selection.replace(/^/gm, '  ')
  return { code: code.slice(0, lineStart) + indented + code.slice(end), start: start + 2, end: end + indented.length - selection.length }
}
