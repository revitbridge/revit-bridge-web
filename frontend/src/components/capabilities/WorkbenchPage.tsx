/* The workbench page (Capabilities > Workbench, also /capabilities/workbench): the workbench
   component (components/hostv2/workbench) on the real client (api/workbench.ts) - paste or write
   a C# snippet, check it, dry-run it against the open model, save it as a tool; your own tools
   listed with their directory, opened, exported, deleted, imported; the built-in ones read-only
   (open to read or copy, export). Run confirms and runs the saved tool (workbenchRun.ts: the
   card goes with any change to the draft, Confirm checks the saved tool again, and Run is off
   while a run is in progress). On a cloud host saving, deleting and importing need the admin
   password (shared/packAdmin). */

import { uiText } from '../../utils/strings'
import { useState, useSyncExternalStore } from 'react'
import { workbenchApi } from '../../api/workbench'
import type { WorkbenchApi } from '../../types/hostv2'
import { Workbench } from '../hostv2/workbench'
import { usePackGate } from '../shared/packAdmin'
import { ExecutionView } from '../task/ExecutionPanel'
import { tokenPrefix } from '../task/format'
import { SpecTable } from '../task/SpecCard'
import { WorkbenchRun, workbenchRunApi, type RunState, type WorkbenchRunApi } from './workbenchRun'

export function RunPanel({ state, onConfirm, onClose }: { state: RunState; onConfirm: () => void; onClose: () => void }) {
  const { card, notice } = state
  if (!card) return notice ? <p className="tool-warning-status" role="status">{notice}</p> : null
  const { spec, defaults, running, outcome } = card
  return (
    <section className="hostv2-workbench-result" aria-label={uiText("Run the saved tool")}>
      <h3>{uiText("Run")} {spec.action.tool}</h3>
      <SpecTable spec={spec} defaults={defaults} />
      {!outcome && <>
        <p className="section-copy small">{uiText("Confirm issues a one-time token for exactly these values and runs the saved tool under it; the validator's checks and the evidence id come back here, as on the Packs page.")}</p>
        <div className="hostv2-workbench-actions">
          <button type="button" className="btn-primary" disabled={running} onClick={onConfirm}>{running ? uiText("Running...") : uiText("Confirm and run in Revit")}</button>
          <button type="button" className="btn-ghost" disabled={running} onClick={onClose}>{uiText("Cancel")}</button>
        </div>
      </>}
      {outcome?.kind === 'rejected' && (
        <ul className="spec-errors">
          {outcome.errors.map((e, i) => <li key={i}>{uiText("Confirm refused:")} <code>{e.code}</code>{e.param ? <> {uiText("on")} <code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
      {outcome?.kind === 'failed' && (
        <div className="tool-warning-status" role="alert">
          <p style={{ margin: 0 }}>{outcome.message}</p>
          {outcome.details.length > 0 && <ul>{outcome.details.map((line, i) => <li key={i}>{line}</li>)}</ul>}
        </div>
      )}
      {outcome?.kind === 'executed' && (
        <div>
          <span className="token-chip">{uiText("token")} <code>{tokenPrefix(outcome.token)}</code> {uiText("| expires")} {outcome.expires_at}</span>
          <ExecutionView execution={outcome.result} />
        </div>
      )}
      {outcome && <button type="button" className="btn-ghost" onClick={onClose}>{uiText("Close")}</button>}
    </section>
  )
}

/* runner: the run state to use (a test passes one); by default the page makes its own. */
export default function WorkbenchPage({ api = workbenchApi, runApi = workbenchRunApi, runner }: { api?: WorkbenchApi; runApi?: WorkbenchRunApi; runner?: WorkbenchRun }) {
  const gate = usePackGate()
  const [run] = useState(() => runner ?? new WorkbenchRun(runApi))
  const state = useSyncExternalStore(run.subscribe, run.getSnapshot, run.getSnapshot)

  return (
    <div className="space-y-4">
      <div className="card section">
        <h3 className="heading-display section-title">{uiText("Workbench")}</h3>
        <p className="section-copy small" style={{ marginTop: 0 }}>{uiText("Paste or write a C# snippet, check it, dry-run it against the open model (rolled back, nothing kept) and save it as a tool. Run confirms and runs the saved tool, as the Packs page does. The model in the chat writes no code; it runs saved tools only and sends you here when none fits.")}</p>
        <p className="section-copy small" style={{ margin: 0 }}>{uiText("Your tools are shared with every host that uses the same tool directory - on this machine the revit-bridge MCP server of Claude Code and the other coding frameworks.")}</p>
      </div>
      <Workbench api={api} onRun={run.onRun} canEdit={gate.canEdit} editNote={gate.note}
        onDraftChange={run.draftChanged} runLocked={state.card?.running ?? false}
        runPanel={<RunPanel state={state} onConfirm={() => { void run.confirm() }} onClose={run.close} />} />
    </div>
  )
}
