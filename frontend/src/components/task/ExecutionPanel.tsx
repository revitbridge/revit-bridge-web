/* Panel 5: the token, the run, the validator's checks - and the tamper demo:
   one value changed after confirmation is confirmation_invalid. */

import { useEffect, useState } from 'react'
import type { ExecutionResult } from '../../types/api'
import ExecResult from '../shared/ExecResult'
import { isGateRefusal, paramsOf, type Confirmation, type Tamper } from './flow'
import { expiresIn, showValue, tokenPrefix } from './format'

interface Props {
  confirmation: Confirmation | null
  execution: ExecutionResult | null
  executionKind: 'genuine' | 'tampered' | null
  running: boolean
  reported: boolean
  canReport: boolean
  onRun: () => void
  onTamper: (tamper: Tamper) => void
  onReport: () => void
}

export default function ExecutionPanel({ confirmation, execution, executionKind, running, reported, canReport, onRun, onTamper, onReport }: Props) {
  // the clock for "expires in ...": ticks once the panel is on screen, then every 15 s
  const [now, setNow] = useState<number | null>(null)
  useEffect(() => {
    if (!confirmation) return
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, 15000)
    return () => { clearTimeout(first); clearInterval(timer) }
  }, [confirmation])

  return (
    <section className="card section">
      <div className="panel-head">
        <h3 className="heading-display section-title">5. Confirm and run</h3>
        {confirmation && (
          <span className="token-chip" title={`spec ${confirmation.spec_hash}`}>
            token <code>{tokenPrefix(confirmation.token)}</code> | expires {now === null ? confirmation.expires_at : expiresIn(confirmation.expires_at, now)} | {confirmation.used ? 'used' : 'unused'}
          </span>
        )}
      </div>

      {!confirmation && <p className="section-copy small" style={{ margin: 0 }}>Confirm the spec card to get a one-time token; it stays in this page's memory only.</p>}

      {confirmation && (
        <>
          <div className="flex gap-2 flex-wrap items-center">
            <button className="btn-primary" onClick={onRun} disabled={running || confirmation.used}>
              {running ? 'Running...' : confirmation.spec.action.kind === 'run_tool' ? `Run ${confirmation.spec.action.tool}` : 'Execute the code'}
            </button>
            {confirmation.used && <span className="muted small-mono">token spent: confirm the card again for another run</span>}
          </div>
          <TamperDemo key={confirmation.token} confirmation={confirmation} running={running} onTamper={onTamper} />
        </>
      )}

      {execution && (
        <ExecutionView execution={execution} tampered={executionKind === 'tampered'} />
      )}

      {execution && !isGateRefusal(execution) && (
        <div className="flex gap-2 items-center" style={{ marginTop: 10 }}>
          <button className="btn-secondary" onClick={onReport} disabled={reported || !canReport}>
            {reported ? 'Reported to the model' : 'Report to the model'}
          </button>
          {!canReport && !reported && <span className="muted small-mono">needs a chat session</span>}
        </div>
      )}
    </section>
  )
}

function TamperDemo({ confirmation, running, onTamper }: { confirmation: Confirmation; running: boolean; onTamper: (t: Tamper) => void }) {
  const spec = confirmation.spec
  const isTool = spec.action.kind === 'run_tool'
  const params = paramsOf(spec)
  const names = Object.keys(params)
  const [param, setParam] = useState(names[0] ?? '')
  const [value, setValue] = useState(names[0] ? showValue(params[names[0]]) : '')
  const [code, setCode] = useState(spec.action.code ?? '')   // keyed by the token: a new confirmation resets the demo

  const pick = (name: string) => { setParam(name); setValue(showValue(params[name])) }
  const unchanged = isTool ? showValue(params[param]) === value : code === (spec.action.code ?? '')

  return (
    <details className="spec-details tamper">
      <summary className="label-text">Tamper demo: change one thing, run under the same token</summary>
      {isTool ? (
        names.length === 0
          ? <p className="section-copy small">This pack has no parameters to edit.</p>
          : <div className="tamper-row">
              <select className="input-field" value={param} onChange={e => pick(e.target.value)} style={{ maxWidth: 200 }}>
                {names.map(n => <option key={n} value={n}>{n}</option>)}
              </select>
              <input className="input-field" value={value} onChange={e => setValue(e.target.value)} style={{ maxWidth: 240 }} aria-label="Edited value" />
              <button className="btn-secondary" onClick={() => onTamper({ param, value })} disabled={running || unchanged}>Run edited</button>
            </div>
      ) : (
        <div>
          <textarea className="code-editor small" value={code} onChange={e => setCode(e.target.value)} spellCheck={false} aria-label="Edited code" />
          <button className="btn-secondary" onClick={() => onTamper({ code })} disabled={running || unchanged} style={{ marginTop: 8 }}>Run edited code</button>
        </div>
      )}
      <p className="section-copy small">The token is bound to the confirmed values: anything else is refused as <code>confirmation_invalid</code> and nothing reaches Revit.</p>
    </details>
  )
}

export function ExecutionView({ execution, tampered }: { execution: ExecutionResult; tampered?: boolean }) {
  const validation = execution.validation ?? null
  const validationFailed = !execution.success && validation !== null && !validation.passed
  const refusal = isGateRefusal(execution)
  const preconditions = execution.preconditions_failed ?? []
  const tone = execution.success ? 'ok' : refusal ? 'refused' : 'failed'

  return (
    <div className={`execution-view tone-${tone}`} data-testid="execution-view">
      {refusal && (
        <div className="execution-banner refused">
          Refused by the gate: <code>{execution.error}</code>{execution.reason ? ` (${execution.reason})` : ''}
          {execution.message ? ` - ${execution.message}` : ''}
          <div className="muted small-mono">{tampered ? 'The edited value does not match the confirmed spec. ' : ''}Nothing reached Revit; the token is not consumed.</div>
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
      {!execution.success && !refusal && !validationFailed && (
        <div className="execution-banner failed">
          Failed: <code>{execution.error ?? 'error'}</code>{execution.message ? ` - ${execution.message}` : ''}
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
