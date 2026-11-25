/* Panel 4: the spec the model proposed, line by line with its source and evidence,
   the interpretations to tick, the conflicts, and Confirm - lit only when reconcile says ready. */

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
        <tr><th>{zh ? '参数' : 'Parameter'}</th><th>{zh ? '值' : 'Value'}</th><th>{zh ? '来源' : 'Source'}</th><th>{zh ? '依据' : 'Evidence'}</th></tr>
      </thead>
      <tbody>
        {spec.parameters.length === 0 && defaults.length === 0 && <tr><td colSpan={4} className="muted">{zh ? '无参数' : 'no parameters'}</td></tr>}
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
            <td className="mono">{zh ? '将使用默认值' : 'default'} {JSON.stringify(d.value)}{d.unit ? ` ${d.unit}` : ''}{zh ? '' : ' will be used'}</td>
            <td><span className="source-pill source-default">{zh ? '默认' : 'default'}</span></td>
            <td className="evidence">{zh ? '由工具填入' : 'not in the spec: the tool fills it'}</td>
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
          {proposal.reconciling ? (zh ? '正在核对…' : 'reconciling...') : ready ? (zh ? '就绪' : 'ready')
            : reconcile ? (zh ? '尚未就绪' : 'not ready') : (zh ? '尚未核对' : 'not reconciled')}
        </span>
      </div>

      <p className="section-copy small" style={{ margin: 0 }}>
        {spec.action.kind === 'run_tool'
          ? <>{zh ? '工具包' : 'Pack'} <code>{spec.action.tool}</code> (run_tool)</>
          : <>{spec.action.kind}{zh ? '：此宿主仅执行已保存的工具' : ': not offered here - this host runs saved tools only'}</>}
      </p>

      <SpecTable spec={draft} conflicts={conflicts} />

      {interpretations.length > 0 && (
        <div className="spec-block">
          <span className="label-text">{zh ? '请确认以下理解' : 'Interpretations to confirm'}</span>
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
                <span className="muted"> {zh ? '可选：' : '| available: '}{c.available.map(showValue).join(', ')}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {openQuestions > 0 && (
        <p className="spec-open">{zh ? `还有 ${openQuestions} 个问题，请在聊天中选择答案。`
          : `${openQuestions} question${openQuestions === 1 ? '' : 's'} still open: answer in the chat (the options are there).`}</p>
      )}

      {reconcileError && (
        <p className="spec-conflict">{zh ? '核对失败：' : 'Not reconciled: '}{reconcileError.message ?? reconcileError.error} ({reconcileError.error})</p>
      )}

      {errors.length > 0 && (
        <ul className="spec-errors">
          {errors.map((e, i) => <li key={i}><code>{e.code}</code>{e.param ? <>{zh ? '，参数 ' : ' on '}<code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}

      <details className="spec-details">
        <summary>{zh ? '详情' : 'Details'}</summary>
        {spec.snapshot_fingerprint && <p>{zh ? '快照' : 'Snapshot'} <code>{spec.snapshot_fingerprint}</code></p>}
        {confirmation && <p>{zh ? '令牌' : 'Token'} <code>{tokenPrefix(confirmation.token)}</code> · {confirmation.expires_at}</p>}
        <pre className="spec-text">{proposal.card}</pre>
        {confirmation?.used && onTamper && <TamperDemo key={confirmation.token} confirmation={confirmation}
          running={!!running} onTamper={onTamper} />}
      </details>

      {confirmErrors.length > 0 && (
        <ul className="spec-errors" data-testid="confirm-errors">
          {confirmErrors.map((e, i) => <li key={i}>{zh ? '确认被拒绝：' : 'Confirm refused: '}<code>{e.code}</code>{e.param ? <>{zh ? '，参数 ' : ' on '}<code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
      {selection && <p role="status">
        {zh ? '助手设置了当前选择：' : 'Selection was set by the assistant: '}
        {selection.count === null ? (zh ? '数量未知' : 'count unknown') : zh ? `${selection.count} 个构件`
          : `${selection.count} element${selection.count === 1 ? '' : 's'}`}
        {' · '}<code>{selection.tool}</code>
        {selection.ids.length > 0 && <> · {selection.ids.join(', ')}
          {selection.count !== null && selection.ids.length < selection.count && '…'}</>}
      </p>}
      {proposal.runReason && <p role="status">{(zh ? '未自动执行：' : 'Manual confirmation: ') + (
        ({ excluded_tool: zh ? '此工具需要单独确认' : 'tool requires a separate confirmation',
           previous_failure: zh ? '此前步骤未成功' : 'a previous step failed',
           step_limit: zh ? '已达到本条消息的步数上限' : 'step limit reached',
           unverified_evidence: zh ? '无法核实参数依据' : 'parameter evidence could not be verified',
           unconfirmed_interpretation: zh ? '请确认假设' : 'assumptions need confirmation',
           not_ready: zh ? '规格尚未就绪' : 'specification is not ready' } as Record<string, string>)[proposal.runReason]
        ?? proposal.runReason)}</p>}
      {execution && <ExecutionView execution={execution} />}
      {proposal.run === 'auto' ? <p role="status">{execution
        ? (execution.success ? (zh ? '已执行' : 'Executed') : (zh ? '执行失败' : 'Execution failed'))
        : proposal.awaitingReceipt ? (zh ? '结果待核对，请查看执行记录。' : 'Result pending. Check the execution record.')
        : (zh ? '自动执行中' : 'Running automatically')}</p> : onRetryReport ? <button type="button" onClick={onRetryReport}>{zh ? '重新提交执行结果' : 'Resend execution result'}</button>
        : proposal.ended ? <p role="status">{proposal.planStep && proposal.planStep.status !== 'proposed' ? planStatus(proposal.planStep.status, zh).label : (zh ? '任务已结束' : 'Task ended')}</p> : execution?.success ? <p role="status">{zh ? '已执行' : 'Executed'}</p> : archived ? <p>{zh ? '已被新卡替换' : 'Replaced by a new card'}</p> :
        <button className="btn-primary" onClick={onConfirm} disabled={!canConfirm}>
          {confirming ? (zh ? '正在确认…' : 'Confirming…') : running ? (zh ? '正在执行…' : 'Running…') : confirmLabel(execution, confirmErrors)}
        </button>}
    </section>
  )
}
