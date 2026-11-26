/* Panel 4: the spec the model proposed, line by line with its source and evidence,
   the interpretations to tick, the conflicts, and Confirm - lit only when reconcile says ready. */

import { uiText } from '../../utils/strings'
import { useEffect, useRef } from 'react'
import type { ExecutionResult, SpecError, TaskSpec } from '../../types/api'
import { confirmLabel, interpretationsOf, type Confirmation, type Proposal, type Tamper } from './flow'
import { planStatus } from './plan'
import { browserLanguage } from '../../utils/language'
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
  onRetryReport?: () => void
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
  const zh = browserLanguage() === 'zh'
  return (
    <table className="spec-table">
      <thead>
        <tr><th>{uiText("Parameter")}</th><th>{uiText("Value")}</th><th>{uiText("Source")}</th><th>{uiText("Evidence")}</th></tr>
      </thead>
      <tbody>
        {spec.parameters.length === 0 && defaults.length === 0 && <tr><td colSpan={4} className="muted">{uiText("no parameters")}</td></tr>}
        {spec.parameters.map(p => (
          <tr key={p.name} className={conflicts.some(c => c.param === p.name) ? 'has-conflict' : undefined}>
            <td className="mono">{p.name}</td>
            <td className="mono">{showValue(p.value)}{p.unit ? ` ${p.unit}` : ''}</td>
            <td><span className={`source-pill source-${p.source}`}>{uiText(p.source)}</span></td>
            <td className="evidence">{p.evidence}</td>
          </tr>
        ))}
        {defaults.map(d => (
          <tr key={`default-${d.name}`}>
            <td className="mono">{d.name}</td>
            <td className="mono">{uiText("default")} {JSON.stringify(d.value)}{d.unit ? ` ${d.unit}` : ''}{zh ? '' : uiText(" will be used")}</td>
            <td><span className="source-pill source-default">{uiText("default")}</span></td>
            <td className="evidence">{uiText("not in the spec: the tool fills it")}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function SpecCard({ proposal, canConfirm, confirming, confirmErrors, onToggleInterpretation, onConfirm,
  execution, confirmation, running, archived, onTamper, onRetryReport }: Props) {
  const zh = browserLanguage() === 'zh'
  const card = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!archived) card.current?.scrollIntoView?.({ block: 'nearest' })
  }, [archived, proposal.spec])
  const { spec, draft, reconcile, reconcileError, errors } = proposal
  const interpretations = interpretationsOf(proposal)
  const conflicts = reconcile?.conflicts ?? []
  const openQuestions = reconcile?.questions.length ?? 0
  const ready = reconcile?.ready === true
  const selection = proposal.assistantSelection

  return (
    <section ref={card} className="card section spec-card" data-ready={ready} data-active-card={!archived}>
      <div className="panel-head">
        <h3 className="section-title">{draft.task}</h3>
        <span className={`readiness ${ready ? 'is-ready' : 'is-open'}`}>
          {proposal.reconciling ? uiText("reconciling...") : ready ? uiText("ready")
            : reconcile ? uiText("not ready") : uiText("not reconciled")}
        </span>
      </div>

      <p className="section-copy small" style={{ margin: 0 }}>
        {spec.action.kind === 'run_tool'
          ? <>{uiText("Pack")} <code>{spec.action.tool}</code> (run_tool)</>
          : <>{spec.action.kind}{uiText(": not offered here - this host runs saved tools only")}</>}
      </p>

      <SpecTable spec={draft} conflicts={conflicts} />

      {interpretations.length > 0 && (
        <div className="spec-block">
          <span className="label-text">{uiText("Interpretations to confirm")}</span>
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
                <span className="muted"> {uiText("| available: ")}{c.available.map(showValue).join(', ')}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {openQuestions > 0 && (
        <p className="spec-open">{uiText("{0} question{1} still open: answer in the chat (the options are there).", openQuestions, openQuestions === 1 ? '' : 's')}</p>
      )}

      {reconcileError && (
        <p className="spec-conflict">{uiText("Not reconciled: ")}{reconcileError.message ?? reconcileError.error} ({reconcileError.error})</p>
      )}

      {errors.length > 0 && (
        <ul className="spec-errors">
          {errors.map((e, i) => <li key={i}><code>{e.code}</code>{e.param ? <>{uiText(" on ")}<code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}

      <details className="spec-details">
        <summary>{uiText("Details")}</summary>
        {spec.snapshot_fingerprint && <p>{uiText("Snapshot")} <code>{spec.snapshot_fingerprint}</code></p>}
        {confirmation && <p>{uiText("Token")} <code>{tokenPrefix(confirmation.token)}</code> · {confirmation.expires_at}</p>}
        <pre className="spec-text">{proposal.card}</pre>
        {confirmation?.used && onTamper && <TamperDemo key={confirmation.token} confirmation={confirmation}
          running={!!running} onTamper={onTamper} />}
      </details>

      {confirmErrors.length > 0 && (
        <ul className="spec-errors" data-testid="confirm-errors">
          {confirmErrors.map((e, i) => <li key={i}>{uiText("Confirm refused: ")}<code>{e.code}</code>{e.param ? <>{uiText(" on ")}<code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
      {selection && <p role="status">
        {uiText("Selection was set by the assistant: ")}
        {selection.count === null ? uiText("count unknown") : uiText("{0} element{1}", selection.count, selection.count === 1 ? '' : 's')}
        {' · '}<code>{selection.tool}</code>
        {selection.ids.length > 0 && <> · {selection.ids.join(', ')}
          {selection.count !== null && selection.ids.length < selection.count && '…'}</>}
      </p>}
      {proposal.runReason && <p role="status">{uiText("Manual confirmation: ") + (
        ({ excluded_tool: uiText("tool requires a separate confirmation"),
           previous_failure: uiText("a previous step failed"),
           workflow_changed: uiText("the workflow was revised"),
           step_limit: uiText("step limit reached"),
           unverified_evidence: uiText("parameter evidence could not be verified"),
           unconfirmed_interpretation: uiText("assumptions need confirmation"),
           not_ready: uiText("specification is not ready") } as Record<string, string>)[proposal.runReason]
        ?? proposal.runReason)}</p>}
      {execution && <ExecutionView execution={execution} />}
      {proposal.run === 'auto' ? <p role="status">{execution
        ? (execution.success ? uiText("Executed") : uiText("Execution failed"))
        : proposal.awaitingReceipt ? uiText("Result pending. Check the execution record.")
        : uiText("Running automatically")}</p> : onRetryReport ? <button type="button" onClick={onRetryReport}>{uiText("Resend execution result")}</button>
        : proposal.ended ? <p role="status">{proposal.planStep && proposal.planStep.status !== 'proposed' ? planStatus(proposal.planStep.status, zh).label : uiText("Task ended")}</p> : execution?.success ? <p role="status">{uiText("Executed")}</p> : archived ? <p>{uiText("Replaced by a new card")}</p> :
        <button className="btn-primary" onClick={onConfirm} disabled={!canConfirm}>
          {confirming ? uiText("Confirming…") : running ? uiText("Running…") : confirmLabel(execution, confirmErrors)}
        </button>}
    </section>
  )
}
