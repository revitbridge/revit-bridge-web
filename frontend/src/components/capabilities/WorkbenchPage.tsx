/* The workbench page (Capabilities > Workbench, also /capabilities/workbench): the workbench
   component (components/hostv2/workbench) on the real client (api/workbench.ts) - paste or write
   a C# snippet, check it, dry-run it against the open model, save it as a tool; your own tools
   listed with their directory, opened, exported, deleted, imported; the built-in ones read-only
   (open to read or copy, export). Run confirms and runs the saved tool (workbenchRun.ts). On a
   cloud host saving, deleting and importing need the admin password (shared/packAdmin). */

import { useCallback, useState } from 'react'
import { workbenchApi } from '../../api/workbench'
import type { TaskSpec } from '../../types/api'
import type { ToolDraft, WorkbenchApi } from '../../types/hostv2'
import { Workbench } from '../hostv2/workbench'
import { usePackGate } from '../shared/packAdmin'
import { ExecutionView } from '../task/ExecutionPanel'
import { tokenPrefix } from '../task/format'
import { SpecTable } from '../task/SpecCard'
import { confirmRun, workbenchRunApi, workbenchSpec, type RunOutcome, type WorkbenchRunApi } from './workbenchRun'

type RunState = { spec: TaskSpec; running: boolean; outcome: RunOutcome | null }

export function RunPanel({ run, onConfirm, onClose }: { run: RunState; onConfirm: () => void; onClose: () => void }) {
  const { spec, running, outcome } = run
  return (
    <section className="hostv2-workbench-result" aria-label="Run the saved tool">
      <h3>Run {spec.action.tool}</h3>
      <SpecTable spec={spec} />
      {!outcome && <>
        <p className="section-copy small">
          Confirm issues a one-time token for exactly these values and runs the saved tool under it; the
          validator's checks and the evidence id come back here, as on the Packs page.
        </p>
        <div className="hostv2-workbench-actions">
          <button type="button" className="btn-primary" disabled={running} onClick={onConfirm}>{running ? 'Running...' : 'Confirm and run in Revit'}</button>
          <button type="button" className="btn-ghost" disabled={running} onClick={onClose}>Cancel</button>
        </div>
      </>}
      {outcome?.kind === 'rejected' && (
        <ul className="spec-errors">
          {outcome.errors.map((e, i) => <li key={i}>Confirm refused: <code>{e.code}</code>{e.param ? <> on <code>{e.param}</code></> : null}: {e.message}</li>)}
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
          <span className="token-chip">token <code>{tokenPrefix(outcome.token)}</code> | expires {outcome.expires_at}</span>
          <ExecutionView execution={outcome.result} />
        </div>
      )}
      {outcome && <button type="button" className="btn-ghost" onClick={onClose}>Close</button>}
    </section>
  )
}

export default function WorkbenchPage({ api = workbenchApi, runApi = workbenchRunApi }: { api?: WorkbenchApi; runApi?: WorkbenchRunApi }) {
  const gate = usePackGate()
  const [run, setRun] = useState<RunState | null>(null)

  // The workbench's Run: refused (the reason shows on the workbench) unless the draft is the
  // saved tool; otherwise its spec card appears under the workbench's buttons.
  const onRun = useCallback(async (draft: ToolDraft, values: Record<string, unknown>) => {
    setRun(null)
    const spec = await workbenchSpec(runApi, draft, values)
    setRun({ spec, running: false, outcome: null })
  }, [runApi])

  const confirm = async () => {
    if (!run || run.running || run.outcome) return
    const spec = run.spec
    setRun({ spec, running: true, outcome: null })
    const outcome = await confirmRun(runApi, spec)
    setRun(current => current && current.spec === spec ? { spec, running: false, outcome } : current)
  }

  return (
    <div className="space-y-4">
      <div className="card section">
        <h3 className="heading-display section-title">Workbench</h3>
        <p className="section-copy small" style={{ marginTop: 0 }}>
          Paste or write a C# snippet, check it, dry-run it against the open model (rolled back, nothing
          kept) and save it as a tool. Run confirms and runs the saved tool, as the Packs page does. The
          model in the chat writes no code; it runs saved tools only and sends you here when none fits.
        </p>
        <p className="section-copy small" style={{ margin: 0 }}>
          Your tools are shared with every host that uses the same tool directory - on this machine the
          revit-bridge MCP server of Claude Code and the other coding frameworks.
        </p>
      </div>
      <Workbench api={api} onRun={onRun} canEdit={gate.canEdit} editNote={gate.note}
        runPanel={run && <RunPanel run={run} onConfirm={() => { void confirm() }} onClose={() => setRun(null)} />} />
    </div>
  )
}
