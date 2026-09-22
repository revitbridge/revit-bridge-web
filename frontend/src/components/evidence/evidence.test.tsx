/* The evidence page: list, expand one record to every field, validate it again. */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import type { EvidenceRecord } from '../../types/api'
import { TaskApiError } from '../task/api'
import EvidenceDetails from './EvidenceDetails'
import { createEvidenceFlow } from './evidenceFlow'
import { RECORD_FIELDS } from './fields'

const record: EvidenceRecord = {
  id: 'ev_20260922T090000_abc123', ts: '2026-09-22T09:00:00Z', host: 'web', action: 'run_tool',
  tool: 'create_structural_column', tool_version: '1.0.0', spec_hash: 'sh', projection_hash: 'ph', token_prefix: 'tok_ab',
  confirmed_by: 'designer', channel: 'host_ui', params: { x: 3000, y: 3000, level_name: 'L1', type_name: 'UC305x305x97' },
  code_sha256: null, code_head: null, document: { title: 'Project1', revit_version: '2026' },
  success: true, error: null, result_summary: { ids: [1234] },
  validation: { validator: 'created_ids', passed: true, checks: [{ detail: '1 new element', passed: true }] },
  duration_ms: 812, preconditions_failed: [], warnings: [],
}

describe('the evidence flow', () => {
  it('loads the list with the filter, expands one record, validates it', async () => {
    const api = {
      evidence: vi.fn().mockResolvedValue([record]),
      validateEvidence: vi.fn().mockResolvedValue({ evidence_id: record.id, tool: record.tool, validator: 'created_ids', passed: false, checks: [{ detail: 'element 1234 is gone', passed: false }] }),
    }
    const flow = createEvidenceFlow(api)
    flow.setFilter('create_structural_column')
    await flow.load()
    expect(api.evidence).toHaveBeenCalledWith(50, 'create_structural_column')
    expect(flow.store.getState().records).toEqual([record])

    flow.expand(record.id)
    expect(flow.store.getState().expanded).toBe(record.id)
    flow.expand(record.id)
    expect(flow.store.getState().expanded).toBeNull()

    await flow.validate(record.id)
    expect(api.validateEvidence).toHaveBeenCalledWith(record.id)
    expect(flow.store.getState().revalidations[record.id]).toMatchObject({ status: 'done', report: { passed: false } })
  })

  it('keeps the server\'s reason when a record cannot be validated, and the list error when the ledger is unreachable', async () => {
    const api = {
      evidence: vi.fn().mockRejectedValue(new Error('503: backend unreachable')),
      validateEvidence: vi.fn().mockRejectedValue(new TaskApiError(400, JSON.stringify({ error: 'no_validator', message: 'only run_tool executions carry a validator' }))),
    }
    const flow = createEvidenceFlow(api)
    await flow.load()
    expect(flow.store.getState().notice).toBe('Cannot list the evidence: 503: backend unreachable')
    await flow.validate('ev_x')
    expect(flow.store.getState().revalidations.ev_x).toEqual({ status: 'failed', error: 'no_validator', message: 'only run_tool executions carry a validator' })
  })
})

describe('EvidenceDetails', () => {
  it('shows all 22 ledger fields and the Validate button', () => {
    const html = renderToStaticMarkup(<EvidenceDetails record={record} onValidate={() => {}} />)
    expect(RECORD_FIELDS).toHaveLength(22)
    for (const field of RECORD_FIELDS) expect(html).toContain(`<dt>${field}</dt>`)
    expect(html).toContain('tok_ab')
    expect(html).toContain('&quot;x&quot;: 3000')
    expect(html).toContain('>Validate</button>')
  })

  it('renders the re-validation report', () => {
    const html = renderToStaticMarkup(
      <EvidenceDetails record={record} onValidate={() => {}}
        revalidation={{ status: 'done', report: { evidence_id: record.id, tool: 'create_structural_column', validator: 'created_ids', passed: false, checks: [{ detail: 'element 1234 is gone', passed: false }] } }} />,
    )
    expect(html).toContain('No longer holds')
    expect(html).toContain('element 1234 is gone')
    expect(html).toContain('check-failed')
  })
})
