/* The ledger record's fields and how one is shown. */

import type { EvidenceRecord } from '../../types/api'

/* In the ledger's order (revit_bridge.evidence.ledger.RECORD_FIELDS). */
export const RECORD_FIELDS: Array<keyof EvidenceRecord> = [
  'id', 'ts', 'host', 'action', 'tool', 'tool_version', 'spec_hash', 'projection_hash',
  'token_prefix', 'confirmed_by', 'channel', 'params', 'code_sha256', 'code_head', 'document',
  'success', 'error', 'result_summary', 'validation', 'duration_ms', 'preconditions_failed', 'warnings',
]

export function showField(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'string') return value || '-'
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (Array.isArray(value) && value.length === 0) return '[]'
  return JSON.stringify(value, null, 2)
}
