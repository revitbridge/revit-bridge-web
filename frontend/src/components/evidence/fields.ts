/* The ledger record's fields and how one is shown. */

import type { EvidenceRecord } from '../../types/api'

/* In the ledger's order (revit_bridge.evidence.ledger.RECORD_FIELDS). */
export const RECORD_FIELDS: Array<keyof EvidenceRecord> = [
  'id', 'ts', 'host', 'action', 'tool', 'tool_version', 'spec_hash', 'projection_hash',
  'token_prefix', 'confirmed_by', 'channel', 'params', 'code_sha256', 'code_head', 'document',
  'success', 'error', 'result_summary', 'validation', 'duration_ms', 'preconditions_failed', 'warnings',
]

/* The fields of one record: the ledger's, then `refused` on the lines that carry it. */
export function fieldsOf(record: EvidenceRecord): Array<keyof EvidenceRecord> {
  return record.refused === true ? [...RECORD_FIELDS, 'refused'] : RECORD_FIELDS
}

/* The list's outcome: success; a pack's refusal in Revit (revit-bridge 0.9.1: refused true,
   nothing was kept; the error is the pack's own text); else the error. */
export function outcomeOf(record: Pick<EvidenceRecord, 'success' | 'error' | 'refused'>): { tone: 'ok' | 'refused' | 'failed'; text: string } {
  if (record.success) return { tone: 'ok', text: 'success' }
  if (record.refused === true) return { tone: 'refused', text: `refused by the tool: ${record.error ?? 'no reason given'}` }
  return { tone: 'failed', text: record.error ?? 'failed' }
}

export function showField(value: unknown): string {
  if (value === null || value === undefined) return '-'
  if (typeof value === 'string') return value || '-'
  if (typeof value === 'boolean' || typeof value === 'number') return String(value)
  if (Array.isArray(value) && value.length === 0) return '[]'
  return JSON.stringify(value, null, 2)
}
