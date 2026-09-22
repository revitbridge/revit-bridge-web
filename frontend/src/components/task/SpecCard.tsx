/* Panel 4: the spec the model proposed, line by line with its source and evidence,
   the interpretations to tick, the conflicts, and Confirm - lit only when reconcile says ready. */

import { useState } from 'react'
import type { SpecError, TaskSpec } from '../../types/api'
import { interpretationsOf, type Proposal } from './flow'
import { showValue } from './format'

interface Props {
  proposal: Proposal
  canConfirm: boolean
  confirming: boolean
  confirmErrors: SpecError[]
  onToggleInterpretation: (text: string, confirmed: boolean) => void
  onConfirm: () => void
}

/* Every parameter with its value, source and evidence; a conflicting one is marked. */
export function SpecTable({ spec, conflicts = [] }: { spec: TaskSpec; conflicts?: Array<Record<string, unknown>> }) {
  return (
    <table className="spec-table">
      <thead>
        <tr><th>Parameter</th><th>Value</th><th>Source</th><th>Evidence</th></tr>
      </thead>
      <tbody>
        {spec.parameters.length === 0 && <tr><td colSpan={4} className="muted">no parameters</td></tr>}
        {spec.parameters.map(p => (
          <tr key={p.name} className={conflicts.some(c => c.param === p.name) ? 'has-conflict' : undefined}>
            <td className="mono">{p.name}</td>
            <td className="mono">{showValue(p.value)}{p.unit ? ` ${p.unit}` : ''}</td>
            <td><span className={`source-pill source-${p.source}`}>{p.source}</span></td>
            <td className="evidence">{p.evidence}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function SpecCard({ proposal, canConfirm, confirming, confirmErrors, onToggleInterpretation, onConfirm }: Props) {
  const [showText, setShowText] = useState(false)
  const { spec, draft, reconcile, reconcileError, errors } = proposal
  const interpretations = interpretationsOf(proposal)
  const conflicts = reconcile?.conflicts ?? []
  const openQuestions = reconcile?.questions.length ?? 0
  const ready = reconcile?.ready === true

  return (
    <section className="card section spec-card" data-ready={ready}>
      <div className="panel-head">
        <h3 className="heading-display section-title">4. Spec card</h3>
        <span className={`readiness ${ready ? 'is-ready' : 'is-open'}`}>
          {proposal.reconciling ? 'reconciling...' : ready ? 'ready' : reconcile ? 'not ready' : 'not reconciled'}
        </span>
      </div>

      <p className="spec-task">{draft.task}</p>
      <p className="section-copy small" style={{ margin: 0 }}>
        {spec.action.kind === 'run_tool'
          ? <>Pack <code>{spec.action.tool}</code> (run_tool)</>
          : <>execute_code ({(spec.action.code ?? '').split('\n').length} lines)</>}
        {spec.snapshot_fingerprint && <> | snapshot <code>{spec.snapshot_fingerprint}</code></>}
      </p>

      <SpecTable spec={draft} conflicts={conflicts} />

      {spec.action.kind === 'execute_code' && (
        <details className="spec-details">
          <summary className="label-text">Code</summary>
          <pre className="command-block">{spec.action.code}</pre>
        </details>
      )}

      {interpretations.length > 0 && (
        <div className="spec-block">
          <span className="label-text">Interpretations to confirm</span>
          {interpretations.map(it => (
            <label key={it.text} className="interpretation">
              <input type="checkbox" checked={it.confirmed} disabled={proposal.reconciling}
                onChange={e => onToggleInterpretation(it.text, e.target.checked)} />
              <span>{it.text}{it.param ? <span className="muted"> ({it.param})</span> : null}</span>
            </label>
          ))}
        </div>
      )}

      {conflicts.length > 0 && (
        <ul className="spec-conflicts">
          {conflicts.map((c, i) => (
            <li key={i} className="spec-conflict">
              <strong>{String(c.param ?? '')}</strong> {String(c.kind ?? '')}: {String(c.message ?? '')}
              {Array.isArray(c.available) && c.available.length > 0 && (
                <span className="muted"> | available: {c.available.map(showValue).join(', ')}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {openQuestions > 0 && (
        <p className="spec-open">{openQuestions} question{openQuestions === 1 ? '' : 's'} still open: answer in the chat (the options are there).</p>
      )}

      {reconcileError && (
        <p className="spec-conflict">Not reconciled: {reconcileError.message ?? reconcileError.error} ({reconcileError.error})</p>
      )}

      {errors.length > 0 && (
        <ul className="spec-errors">
          {errors.map((e, i) => <li key={i}><code>{e.code}</code>{e.param ? <> on <code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}

      <div className="spec-actions">
        <button className="btn-primary" onClick={onConfirm} disabled={!canConfirm}>
          {confirming ? 'Confirming...' : 'Confirm'}
        </button>
        <button className="btn-ghost" onClick={() => setShowText(!showText)}>{showText ? 'Hide card text' : 'Card as text'}</button>
        {!ready && !proposal.reconciling && <span className="muted small-mono">Confirm lights up when reconcile says ready.</span>}
      </div>
      {showText && <pre className="spec-text">{proposal.card}</pre>}

      {confirmErrors.length > 0 && (
        <ul className="spec-errors" data-testid="confirm-errors">
          {confirmErrors.map((e, i) => <li key={i}>Confirm refused: <code>{e.code}</code>{e.param ? <> on <code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
    </section>
  )
}
