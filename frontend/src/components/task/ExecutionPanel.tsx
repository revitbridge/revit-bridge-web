/* Panel 5: the token, the run, the validator's checks - and the tamper demo:
   one value changed after confirmation is confirmation_invalid. */

import { useState } from 'react'
import type { ExecutionResult } from '../../types/api'
import ExecResult from '../shared/ExecResult'
import { isGateRefusal, isToolRefusal, paramsOf, type Confirmation, type Tamper } from './flow'
import { showValue } from './format'

export function TamperDemo({ confirmation, running, onTamper }: { confirmation: Confirmation; running: boolean; onTamper: (t: Tamper) => Promise<ExecutionResult | null> }) {
  const params = paramsOf(confirmation.spec)
  const names = Object.keys(params)
  const [param, setParam] = useState(names[0] ?? '')   // keyed by the token: a new confirmation resets the demo
  const [value, setValue] = useState(names[0] ? showValue(params[names[0]]) : '')

  const [result, setResult] = useState<ExecutionResult | null>(null)
  const [pending, setPending] = useState(false)
  const pick = (name: string) => { setParam(name); setValue(showValue(params[name])) }
  const unchanged = showValue(params[param]) === value

  return (
    <details className="spec-details tamper">
      <summary className="label-text">Tamper demo: change one thing, run under the same token</summary>
      {names.length === 0
        ? <p className="section-copy small">This pack has no parameters to edit.</p>
        : <div className="tamper-row">
            <select className="input-field" value={param} onChange={e => pick(e.target.value)} style={{ maxWidth: 200 }}>
              {names.map(n => <option key={n} value={n}>{n}</option>)}
            </select>
            <input className="input-field" value={value} onChange={e => setValue(e.target.value)} style={{ maxWidth: 240 }} aria-label="Edited value" />
            <button className="btn-secondary" onClick={async () => {
              setPending(true)
              try { setResult(await onTamper({ param, value })) }
              finally { setPending(false) }
            }} disabled={pending || running || unchanged}>Run edited</button>
          </div>}
      {result && <ExecutionView execution={result} />}
      <p className="section-copy small">This demo submits the edited value with the token from the completed run. The gate checks whether that token has already been used before comparing values.</p>
    </details>
  )
}

export function ExecutionView({ execution }: { execution: ExecutionResult }) {
  const validation = execution.validation ?? null
  const validationFailed = !execution.success && validation !== null && !validation.passed
  const refusal = isGateRefusal(execution)
  const toolRefused = isToolRefusal(execution)
  const preconditions = execution.preconditions_failed ?? []
  const tone = execution.success ? 'ok' : refusal || toolRefused ? 'refused' : 'failed'

  return (
    <div className={`execution-view tone-${tone}`} data-testid="execution-view">
      {refusal && (
        <div className="execution-banner refused">
          Refused by the gate: <code>{execution.error}</code>{execution.reason ? ` (${execution.reason})` : ''}
          {execution.message ? ` - ${execution.message}` : ''}
          <div className="muted small-mono">This request did not reach Revit.</div>
          {execution.hint && <div className="muted small-mono">{execution.hint}</div>}
        </div>
      )}
      {validationFailed && (
        <div className="execution-banner validation-failed">
          VALIDATION FAILED - Revit ran it, but the model did not change as claimed ({validation.validator}).
        </div>
      )}
      {execution.success && (
        <div className="execution-banner ok">
          Executed{validation ? ` and validated by ${validation.validator}` : ''}{execution.tool ? ` - ${execution.tool}` : ''}.
        </div>
      )}
      {toolRefused && (
        <div className="execution-banner refused">
          Refused by the tool in Revit: <code>{execution.error ?? 'no reason given'}</code>
          <div className="muted small-mono">Nothing was kept: the add-in rolled the run back. The token is spent.</div>
        </div>
      )}
      {!execution.success && !refusal && !toolRefused && !validationFailed && (
        <div className="execution-banner failed">
          Failed: <code>{execution.error ?? 'error'}</code>
          {execution.message ? ` - ${execution.message}` : ''}
        </div>
      )}

      {preconditions.length > 0 && (
        <ul className="spec-errors"><li>Preconditions failed:</li>{preconditions.map((p, i) => <li key={i}>{p}</li>)}</ul>
      )}

      {validation && (
        <ul className="validation-checks">
          {validation.checks.map((c, i) => (
            <li key={i} className={c.passed === false ? 'check-failed' : 'check-passed'}>
              <span className="check-mark">{c.passed === false ? '✗' : '✓'}</span> {c.detail}
            </li>
          ))}
        </ul>
      )}

      {(execution.evidence_id || (execution.warnings && execution.warnings.length > 0)) && (
        <p className="muted small-mono" style={{ margin: '6px 0 0' }}>
          {execution.evidence_id && <>evidence <code>{execution.evidence_id}</code></>}
          {execution.warnings && execution.warnings.length > 0 && <> | warnings: {execution.warnings.join('; ')}</>}
        </p>
      )}

      {execution.result !== undefined && execution.result !== null && (
        <details className="spec-details">
          <summary className="label-text">Result from Revit</summary>
          <ExecResult result={{ ok: true, data: execution.result }} />
        </details>
      )}
    </div>
  )
}
