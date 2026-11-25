/* Panel 4: the spec the model proposed, line by line with its source and evidence,
   the interpretations to tick, the conflicts, and Confirm - lit only when reconcile says ready. */

import { useEffect, useRef } from 'react'
import type { ExecutionResult, SpecError, TaskSpec } from '../../types/api'
import { confirmLabel, interpretationsOf, type Confirmation, type Proposal, type Tamper } from './flow'
import { showValue, tokenPrefix } from './format'
import { ExecutionView, TamperDemo } from './ExecutionPanel'

interface Props {
  execution?: ExecutionResult | null
  confirmation?: Confirmation | null
  running?: boolean
  archived?: boolean
  onTamper?: (value: Tamper) => Promise<ExecutionResult | null>
  proposal: Proposal
  canConfirm: boolean
  confirming: boolean
  confirmErrors: SpecError[]
  onToggleInterpretation: (text: string, confirmed: boolean) => void
  onConfirm: () => void
}

/* Every parameter with its value, source and evidence; a conflicting one is marked. defaults:
   parameters the spec leaves out whose default the tool fills when it runs (the workbench's
   cleared optional values), as rows of their own; they are not part of the spec. */
export function SpecTable({ spec, conflicts = [], defaults = [] }: {
  spec: TaskSpec
  conflicts?: Array<Record<string, unknown>>
  defaults?: Array<{ name: string; value: unknown; unit?: string | null }>
}) {
  return (
    <table className="spec-table">
      <thead>
        <tr><th>Parameter</th><th>Value</th><th>Source</th><th>Evidence</th></tr>
      </thead>
      <tbody>
        {spec.parameters.length === 0 && defaults.length === 0 && <tr><td colSpan={4} className="muted">no parameters</td></tr>}
        {spec.parameters.map(p => (
          <tr key={p.name} className={conflicts.some(c => c.param === p.name) ? 'has-conflict' : undefined}>
            <td className="mono">{p.name}</td>
            <td className="mono">{showValue(p.value)}{p.unit ? ` ${p.unit}` : ''}</td>
            <td><span className={`source-pill source-${p.source}`}>{p.source}</span></td>
            <td className="evidence">{p.evidence}</td>
          </tr>
        ))}
        {defaults.map(d => (
          <tr key={`default-${d.name}`}>
            <td className="mono">{d.name}</td>
            <td className="mono">default {JSON.stringify(d.value)}{d.unit ? ` ${d.unit}` : ''} will be used</td>
            <td><span className="source-pill source-default">default</span></td>
            <td className="evidence">not in the spec: the tool fills it</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function SpecCard({ proposal, canConfirm, confirming, confirmErrors, onToggleInterpretation, onConfirm,
  execution, confirmation, running, archived, onTamper }: Props) {
  const card = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!archived) card.current?.scrollIntoView?.({ block: 'nearest' })
  }, [archived, proposal.spec])
  const { spec, draft, reconcile, reconcileError, errors } = proposal
  const interpretations = interpretationsOf(proposal)
  const conflicts = reconcile?.conflicts ?? []
  const openQuestions = reconcile?.questions.length ?? 0
  const ready = reconcile?.ready === true

  return (
    <section ref={card} className="card section spec-card" data-ready={ready} data-active-card={!archived}>
      <div className="panel-head">
        <h3 className="section-title">{draft.task}</h3>
        <span className={`readiness ${ready ? 'is-ready' : 'is-open'}`}>
          {proposal.reconciling ? 'reconciling...' : ready ? 'ready' : reconcile ? 'not ready' : 'not reconciled'}
        </span>
      </div>

      <p className="section-copy small" style={{ margin: 0 }}>
        {spec.action.kind === 'run_tool'
          ? <>Pack <code>{spec.action.tool}</code> (run_tool)</>
          : <>{spec.action.kind}: not offered here - this host runs saved tools only</>}
      </p>

      <SpecTable spec={draft} conflicts={conflicts} />

      {interpretations.length > 0 && (
        <div className="spec-block">
          <span className="label-text">Interpretations to confirm</span>
          {interpretations.map(it => (
            <label key={it.text} className="interpretation">
              <input type="checkbox" checked={it.confirmed} disabled={archived || proposal.ended || !!execution?.success || confirming || running || proposal.reconciling}
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

      <details className="spec-details">
        <summary>详情</summary>
        {spec.snapshot_fingerprint && <p>Snapshot <code>{spec.snapshot_fingerprint}</code></p>}
        {confirmation && <p>Token <code>{tokenPrefix(confirmation.token)}</code> · {confirmation.expires_at}</p>}
        <pre className="spec-text">{proposal.card}</pre>
        {confirmation?.used && onTamper && <TamperDemo key={confirmation.token} confirmation={confirmation}
          running={!!running} onTamper={onTamper} />}
      </details>

      {confirmErrors.length > 0 && (
        <ul className="spec-errors" data-testid="confirm-errors">
          {confirmErrors.map((e, i) => <li key={i}>Confirm refused: <code>{e.code}</code>{e.param ? <> on <code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
      {execution && <ExecutionView execution={execution} />}
      {proposal.ended ? <p role="status">任务已结束</p> : execution?.success ? <p role="status">已执行</p> : archived ? <p>已被新卡替换</p> :
        <button className="btn-primary" onClick={onConfirm} disabled={!canConfirm}>
          {confirming ? '正在确认…' : running ? '正在执行…' : confirmLabel(execution, confirmErrors)}
        </button>}
    </section>
  )
}
