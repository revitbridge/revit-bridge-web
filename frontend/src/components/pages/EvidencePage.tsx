/* Evidence: the ledger of executions (GET /evidence); expand one record for every
   field, Validate re-runs its assertion against the model now. */

import { uiText } from '../../utils/strings'
import { useEffect, useState } from 'react'
import { useStore } from 'zustand'
import { taskApi } from '../task/api'
import { usePanelDialog, useWidePanel } from '../shared/usePanelLayout'
import EvidenceDetails from '../evidence/EvidenceDetails'
import { createEvidenceFlow, type EvidenceApi, type EvidenceFlow } from '../evidence/evidenceFlow'
import { outcomeOf } from '../evidence/fields'

let sharedFlow: EvidenceFlow | null = null
function flowFor(api?: EvidenceApi): EvidenceFlow {
  if (api) return createEvidenceFlow(api)
  sharedFlow ??= createEvidenceFlow(taskApi)
  return sharedFlow
}

export default function EvidencePage({ api }: { api?: EvidenceApi }) {
  const [flow] = useState(() => flowFor(api))
  const state = useStore(flow.store)

  useEffect(() => { flow.load() }, [flow])

  const record = state.records.find(r => r.id === state.expanded)
  const docked = useWidePanel()
  const inspector = usePanelDialog(docked, Boolean(record))
  return (
    <div className="page evidence-page">
      <section className="card section">
        <div className="panel-head">
          <h3 className="heading-display section-title">{uiText("Evidence")}</h3>
          <div className="flex gap-2 items-center flex-wrap">
            <input className="input-field" value={state.tool} onChange={e => flow.setFilter(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && flow.load()} placeholder={uiText("filter by pack name")} style={{ maxWidth: 220 }} />
            <button className="btn-secondary" onClick={() => flow.load()} disabled={state.loading}>{state.loading ? uiText("Loading...") : uiText("Refresh")}</button>
          </div>
        </div>
        <p className="section-copy small" style={{ marginTop: 0 }}>{uiText("Every execution through this host or the MCP server: who confirmed it, what ran, what the validator found. Newest first,")} {state.limit} {uiText("at most.")}</p>
        {state.notice && <div className="status-line">{state.notice}</div>}
        {!state.notice && state.records.length === 0 && !state.loading && (
          <p className="section-copy small">{uiText("No executions recorded yet.")} {' '}
            <a href="/">{uiText("Describe a task in Chat and run it to see its record here.")}</a></p>
        )}
        {state.records.length > 0 && (
          <div className="evidence-layout"><div className="table-scroll">
            <table className="evidence-table">
              <thead>
                <tr>
                  <th>{uiText("When")}</th><th>{uiText("Id")}</th><th>{uiText("Host")}</th><th>{uiText("Action")}</th><th>{uiText("Pack")}</th><th>{uiText("Outcome")}</th><th>{uiText("Validation")}</th><th>{uiText("By")}</th><th>ms</th>
                </tr>
              </thead>
              <tbody>
                {state.records.map(r => {
                  const open = state.expanded === r.id
                  const outcome = outcomeOf(r)
                  return (
                    <tr key={r.id} className={`evidence-row${open ? ' is-open' : ''}`} onClick={() => flow.expand(r.id)}>
                      <td>{r.ts}</td>
                      <td><button className="record-id btn-ghost" aria-expanded={open}>{r.id}</button></td>
                      <td>{r.host}</td>
                      <td>{r.action}</td>
                      <td className="mono">{r.tool ?? '-'}</td>
                      <td><span className={`outcome ${outcome.tone}`}>{outcome.text}</span></td>
                      <td>{r.validation ? <span className={`outcome ${r.validation.passed ? 'ok' : 'failed'}`}>{r.validation.validator} {r.validation.passed ? uiText("passed") : uiText("FAILED")}</span> : <span className="muted">-</span>}</td>
                      <td>{r.confirmed_by ?? '-'}</td>
                      <td>{r.duration_ms ?? '-'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {record && <dialog ref={inspector} className={'evidence-inspector ' + (docked ? 'docked' : 'drawer-shell')}
            aria-label={record.id} onCancel={e => { e.preventDefault(); flow.expand(record.id) }}><div className="panel-head"><h3>{record.id}</h3>
            <button className="btn-ghost" onClick={() => flow.expand(record.id)}>{uiText("Close")}</button></div>
            <EvidenceDetails record={record} revalidation={state.revalidations[record.id]} onValidate={id => flow.validate(id)} />
          </dialog>}</div>
        )}
      </section>
    </div>
  )
}
