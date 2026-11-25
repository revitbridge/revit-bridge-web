/* Task: the demo sequence 1-6 - snapshot, brief, (no-bridge comparison), spec card,
   confirm and run a saved tool under a token, the model's report. The model here writes
   no code: a tool it lacks is made on the workbench (Capabilities). The state and the
   moves live in components/task/flow.ts; this page only lays them out. */

import { useState } from 'react'
import { ExecutionMode } from '../task/ExecutionMode'
import { useExecutionMode } from '../task/executionPreference'
import useHostFeatures from '../shared/useHostFeatures'
import { useStore } from 'zustand'
import { chatEvents } from '../../api/chat'
import { browserLanguage } from '../../utils/language'

import { flowApi } from '../task/api'
import { currentRequestMode, useChatMode } from '../task/chatMode'
import ChatModeSelect from '../task/ChatModeSelect'
import ChatPanel from '../task/ChatPanel'
import { canConfirm, createTaskFlow, usageOf, type EventSource, type TaskFlow, type TaskState } from '../task/flow'
import { TokenPanel } from '../hostv2/trace'
import SnapshotPanel from '../task/SnapshotPanel'
import SpecCard from '../task/SpecCard'
import StepIndicator from '../shared/StepIndicator'

const STEPS = ['Snapshot', 'Conversation', 'Report']

/* One flow for the tab's lifetime, so switching pages keeps the conversation. */
let sharedFlow: TaskFlow | null = null
function flowFor(events?: EventSource): TaskFlow {
  const run = () => useExecutionMode.getState().auto ? 'auto' as const : 'confirm' as const
  if (events) return createTaskFlow({ events, api: flowApi, mode: currentRequestMode, run })
  sharedFlow ??= createTaskFlow({ events: chatEvents, api: flowApi, mode: currentRequestMode, run })
  return sharedFlow
}

function stepOf(s: TaskState): number {
  if (s.reported) return 3
  if (s.bridge.messages.length > 0) return 2
  return 1
}

export default function TaskPage({ events }: { events?: EventSource }) {
  const zh = browserLanguage() === 'zh'
  const [flow] = useState(() => flowFor(events))
  const state = useStore(flow.store)
  const features = useHostFeatures()
  const chatMode = useChatMode(s => s.mode)
  const setChatMode = useChatMode(s => s.setMode)
  const busy = state.bridge.streaming || state.baseline.streaming || state.confirming || state.running
  const questions = state.proposal?.reconcile?.questions ?? []

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
          <ChatPanel title="With the bridge" transcript={state.bridge} questions={questions} questionMessage={state.cardMessage}
            highlight={state.highlight} onSubmitForm={(text, answers) => flow.send(text, [], answers)}
            renderCards={index => <>
              {state.archived.filter(c => c.message === index).map((c, i) => <SpecCard key={i} archived
                proposal={c.proposal} confirmation={c.confirmation} execution={c.execution}
                confirming={false} canConfirm={false} confirmErrors={[]}
                onConfirm={() => {}} onToggleInterpretation={() => {}} />)}
              {state.cardMessage === index && state.proposal && <SpecCard proposal={state.proposal}
                canConfirm={canConfirm(state)} confirming={state.confirming} running={state.running}
                execution={state.execution} confirmation={state.confirmation} confirmErrors={state.confirmErrors}
                onRetryReport={state.proposal.planStep && !state.proposal.ended && state.confirmation?.used
                  && state.execution && !state.reported && !state.bridge.streaming ? flow.report : undefined}
                onConfirm={flow.confirmAndRun} onToggleInterpretation={flow.toggleInterpretation}
                onTamper={flow.demoTamper} />}
            </>}
            onSend={(t, picked) => flow.send(t, picked)} onAnswer={(q, o) => flow.answer(q, o)} onReset={() => flow.reset()} disabled={busy}
            placeholder={zh ? '例如：在坐标 (3000, 3000) 放一根结构柱。' : 'For example, place a structural column at (3000, 3000).'}
            hint={zh ? '模型会读取快照和工具包，询问缺少的信息，再在回复中给出任务卡。输入 @ 可选择标高、类型、链接或 Revit 中选中的构件。宿主会逐项与当前模型核对。'
              : 'The model sees the snapshot and the packs. It asks what it cannot know, then adds a spec card to its reply. Type @ to pick a level, type, link or selected Revit element. The host verifies each pick against the open model.'}
            showTrace
            controls={<><ChatModeSelect features={features} mode={chatMode} onChange={setChatMode} disabled={busy} />
              {features.runModes?.includes('auto') && <ExecutionMode disabled={busy} />}
              {state.bridge.streaming && <button type="button" onClick={flow.abort}>{zh ? '停止' : 'Stop'}</button>}
            </>} />
          {state.compare && (
            <ChatPanel title="Without the bridge (baseline)" transcript={state.baseline}
              hint="The same brief goes here with bridge=false. Nothing it says can be executed." />
          )}
        </div>
        {(state.bridge.messages.length > 0 || (state.compare && state.baseline.messages.length > 0)) && (
          <div className={`task-columns task-tokens${state.compare ? ' two' : ''}`}>
            <TokenPanel title="Tokens" {...usageOf(state.bridge)} />
            {state.compare && <TokenPanel title="Tokens without the bridge" {...usageOf(state.baseline)} />}
          </div>
        )}
      </section>

    </div>
  )
}
