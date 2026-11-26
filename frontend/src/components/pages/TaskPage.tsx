/* The conversation, project context and optional baseline share the existing task flow. */

import { uiText } from '../../utils/strings'
import { useState } from 'react'
import { ExecutionMode } from '../task/ExecutionMode'
import { useExecutionMode } from '../task/executionPreference'
import useHostFeatures from '../shared/useHostFeatures'
import { useStore } from 'zustand'
import { chatEvents } from '../../api/chat'

import { flowApi } from '../task/api'
import { currentRequestMode, useChatMode } from '../task/chatMode'
import ChatModeSelect from '../task/ChatModeSelect'
import ChatPanel from '../task/ChatPanel'
import type { SelectionSupport } from '../task/useSelectionState'
import { canConfirm, createTaskFlow, usageOf, type EventSource, type TaskFlow } from '../task/flow'
import { TokenPanel } from '../hostv2/trace'
import SpecCard from '../task/SpecCard'

/* One flow for the tab's lifetime, so switching pages keeps the conversation. */
let sharedFlow: TaskFlow | null = null
function flowFor(events?: EventSource): TaskFlow {
  const run = () => useExecutionMode.getState().auto ? 'auto' as const : 'confirm' as const
  if (events) return createTaskFlow({ events, api: flowApi, mode: currentRequestMode, run })
  sharedFlow ??= createTaskFlow({ events: chatEvents, api: flowApi, mode: currentRequestMode, run })
  return sharedFlow
}

export default function TaskPage({ events, support, onDestination, initialTool }: { initialTool?: string; events?: EventSource; support?: SelectionSupport; onDestination?: (destination: 'authoring' | 'remote_mcp', request: string) => void }) {
  const [flow] = useState(() => flowFor(events))
  const state = useStore(flow.store)
  const features = useHostFeatures()
  const chatMode = useChatMode(s => s.mode)
  const setChatMode = useChatMode(s => s.setMode)
  const busy = state.bridge.streaming || state.baseline.streaming || state.confirming || state.running
  const questions = state.proposal?.reconcile?.questions ?? []

  return (
    <div className={'chat-workspace' + (state.compare ? ' comparing' : '')}>
      <ChatPanel initialInput={initialTool ? uiText('Use the {0} tool.', initialTool) : ''} title={uiText("With the bridge")} transcript={state.bridge} questions={questions} questionMessage={state.cardMessage}
        highlight={state.highlight} onSubmitForm={(text, answers) => flow.send(text, [], answers)}
        compare={state.compare}
        context={<>
          <label className="compare-toggle"><input type="checkbox" checked={state.compare}
            onChange={e => flow.setCompare(e.target.checked)} disabled={busy} />
            <span>{uiText("Compare with no bridge")}</span></label>
          <p className="helper-text">{uiText("One sentence, as a designer would say it. With the bridge the model reads the snapshot and must ask before it proposes; the questions come with the real options. The no-bridge session (")}<code>{uiText("bridge=false")}</code>{uiText(") has no tools and no skills and cannot execute anything: it shows how the same brief would be filled in blindly.")}</p>
        </>}
        footer={state.bridge.messages.length > 0 && <TokenPanel title={uiText("Tokens")} {...usageOf(state.bridge)} />}
        stop={state.bridge.streaming && <button className="btn-danger" type="button" onClick={flow.abort}>{uiText("Stop")}</button>}
        support={support} onDestination={onDestination}
        onDismiss={index => flow.store.setState(s => ({ bridge: { ...s.bridge,
          messages: s.bridge.messages.map((m, i) => i === index ? { ...m, missingTool: undefined } : m) } }))}
        hiddenCaptures={state.hiddenCaptures} onHide={id => flow.store.setState(s => ({
          hiddenCaptures: s.hiddenCaptures.includes(id) ? s.hiddenCaptures.filter(x => x !== id) : [...s.hiddenCaptures, id]
        }))}
        onSelectionAnswer={(id, items) => flow.send(uiText('Selected elements in Revit.'), items, undefined, id)}
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
        onSend={(t, picked, selection) => flow.send(t, picked, undefined, undefined, selection)} onAnswer={flow.answer} onReset={() => flow.reset()} disabled={busy}
        placeholder={uiText("For example, place a structural column at (3000, 3000).")}
        hint={uiText("The model sees the snapshot and the packs. It asks what it cannot know, then adds a spec card to its reply. Type @ to pick a level, type, link or selected Revit element. The host verifies each pick against the open model.")}
        showTrace
        controls={<><ChatModeSelect features={features} mode={chatMode} onChange={setChatMode} disabled={busy} />
          {features.runModes?.includes('auto') && <ExecutionMode disabled={busy} />}
        </>} />
      {state.compare && (
        <ChatPanel title={uiText("Without the bridge (baseline)")} transcript={state.baseline}
          footer={(state.bridge.messages.length > 0 || state.baseline.messages.length > 0) && <TokenPanel title={uiText("Tokens without the bridge")} {...usageOf(state.baseline)} />}
          hint={uiText("The same brief goes here with bridge=false. Nothing it says can be executed.")} />
      )}
    </div>
  )
}
