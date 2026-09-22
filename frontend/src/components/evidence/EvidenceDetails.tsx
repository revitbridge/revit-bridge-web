/* One ledger record, every field; the Validate button re-runs its assertion. */

import type { EvidenceRecord } from '../../types/api'
import type { Revalidation } from './evidenceFlow'
import { RECORD_FIELDS, showField } from './fields'

interface Props {
  record: EvidenceRecord
  revalidation?: Revalidation
  onValidate: (id: string) => void
}

export default function EvidenceDetails({ record, revalidation, onValidate }: Props) {
  const canValidate = record.action === 'run_tool' && record.validation !== null
  return (
    <div className="evidence-details">
      <dl className="evidence-fields">
        {RECORD_FIELDS.map(field => {
          const value = record[field]
          const block = value !== null && typeof value === 'object' && !(Array.isArray(value) && value.length === 0)
          return (
            <div key={field} className="evidence-field">
              <dt>{field}</dt>
              <dd>{block ? <pre>{showField(value)}</pre> : showField(value)}</dd>
            </div>
          )
        })}
      </dl>
      <div className="flex gap-2 items-center flex-wrap" style={{ marginTop: 10 }}>
        <button className="btn-secondary" onClick={() => onValidate(record.id)} disabled={revalidation?.status === 'running'}
          title={canValidate ? 'Re-run the recorded assertion against the model now' : 'Only run_tool executions with a validator can be re-checked'}>
          {revalidation?.status === 'running' ? 'Validating...' : 'Validate'}
        </button>
        {!canValidate && <span className="muted small-mono">no validator on this record: the server will say so</span>}
      </div>
      {revalidation?.status === 'done' && (
        <div className={`execution-banner ${revalidation.report.passed ? 'ok' : 'validation-failed'}`} style={{ marginTop: 8 }}>
          {revalidation.report.passed ? 'Still holds' : 'No longer holds'}: {revalidation.report.validator} on {revalidation.report.tool}
          <ul className="validation-checks">
            {revalidation.report.checks.map((c, i) => (
              <li key={i} className={c.passed === false ? 'check-failed' : 'check-passed'}>
                <span className="check-mark">{c.passed === false ? '✗' : '✓'}</span> {c.detail}
              </li>
            ))}
          </ul>
        </div>
      )}
      {revalidation?.status === 'failed' && (
        <p className="spec-conflict">Cannot validate: <code>{revalidation.error}</code> {revalidation.message}</p>
      )}
    </div>
  )
}
