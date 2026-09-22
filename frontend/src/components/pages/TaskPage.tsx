/* Task: the demo sequence 1-7 - snapshot, brief, (no-bridge comparison), spec card,
   confirm and run under a token, the model's report, solidify. The state and the
   moves live in components/task/flow.ts; this page only lays them out. */

import { useState } from 'react'
import { useStore } from 'zustand'
import { chatEvents } from '../../api/chat'
import { taskApi } from '../task/api'
import ChatPanel from '../task/ChatPanel'
import ExecutionPanel from '../task/ExecutionPanel'
import { canConfirm, createTaskFlow, type EventSource, type TaskFlow, type TaskState } from '../task/flow'
import SnapshotPanel from '../task/SnapshotPanel'
import SolidifyPanel from '../task/SolidifyPanel'
import SpecCard from '../task/SpecCard'
import StepIndicator from '../shared/StepIndicator'

const STEPS = ['Snapshot', 'Brief', 'Spec card', 'Confirm and run', 'Report', 'Solidify']

/* One flow for the tab's lifetime, so switching pages keeps the conversation. */
let sharedFlow: TaskFlow | null = null
function flowFor(events?: EventSource): TaskFlow {
  if (events) return createTaskFlow({ events, api: taskApi })
  sharedFlow ??= createTaskFlow({ events: chatEvents, api: taskApi })
  return sharedFlow
}

function stepOf(s: TaskState): number {
  if (s.execution?.success && s.confirmation?.spec.action.kind === 'execute_code') return 6
  if (s.reported) return 5
  if (s.confirmation) return 4
  if (s.proposal) return 3
  if (s.bridge.messages.length > 0) return 2
  return 1
}

export default function TaskPage({ events }: { events?: EventSource }) {
  const [flow] = useState(() => flowFor(events))
  const state = useStore(flow.store)
  const busy = state.bridge.streaming || state.baseline.streaming
  const questions = state.proposal?.reconcile?.questions ?? []
  const solidifiable = state.execution?.success && state.confirmation?.spec.action.kind === 'execute_code' ? state.confirmation.spec : null

  return (
    <div className="page">
      <StepIndicator steps={STEPS} current={stepOf(state)} />

      <SnapshotPanel />

      <section className="task-brief">
        <div className="panel-head">
          <h3 className="heading-display section-title">2. Brief</h3>
          <label className="compare-toggle">
            <input type="checkbox" checked={state.compare} onChange={e => flow.setCompare(e.target.checked)} disabled={busy} />
            <span>3. Compare with no bridge</span>
          </label>
        </div>
        <p className="section-copy small" style={{ marginTop: 0 }}>
          One sentence, as a designer would say it. With the bridge the model reads the snapshot and must ask before it proposes;
          the questions come with the real options. The no-bridge session (<code>bridge=false</code>) has no tools and no skills
          and cannot execute anything: it shows how the same brief would be filled in blindly.
        </p>
        <div className={`task-columns${state.compare ? ' two' : ''}`}>
          <ChatPanel title="With the bridge" transcript={state.bridge} questions={questions}
            onSend={t => flow.send(t)} onAnswer={(q, o) => flow.answer(q, o)} onReset={() => flow.reset()} disabled={busy}
            placeholder="e.g. 在坐标 (3000, 3000) 放一根结构柱。"
            hint="The model sees the snapshot and the packs. It asks what it cannot know, then proposes a spec card below." />
          {state.compare && (
            <ChatPanel title="Without the bridge (baseline)" transcript={state.baseline}
              hint="The same brief goes here with bridge=false. Nothing it says can be executed." />
          )}
        </div>
      </section>

      {state.proposal && (
        <SpecCard proposal={state.proposal} canConfirm={canConfirm(state)} confirming={state.confirming}
          confirmErrors={state.confirmErrors}
          onToggleInterpretation={(text, confirmed) => flow.toggleInterpretation(text, confirmed)}
          onConfirm={() => flow.confirm()} />
      )}

      {(state.confirmation || state.execution) && (
        <ExecutionPanel confirmation={state.confirmation} execution={state.execution} executionKind={state.executionKind}
          running={state.running} reported={state.reported} canReport={!!state.bridge.sessionId && !state.bridge.streaming}
          onRun={() => flow.run()} onTamper={t => flow.runTampered(t)} onReport={() => flow.report()} />
      )}

      {solidifiable && (
        <SolidifyPanel spec={solidifiable} evidenceId={state.execution?.evidence_id} solidify={taskApi.solidify} />
      )}
    </div>
  )
}
