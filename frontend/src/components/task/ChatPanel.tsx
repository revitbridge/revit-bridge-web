/* Panels 2 and 3: the brief and the model's questions; the same transcript
   without the bridge when the comparison is on. A question's kind is a badge
   and what it holds up its tooltip: shown only, the spec card gates on
   reconcile.ready. With showTrace each reply has its tool activity above it
   (the trace of its turn: one row per tool call, expandable).

   The input is the mention input: `@` offers @link, @type, @level and @element, and a picked
   model item is one tag (components/hostv2/mention). A sent message shows its picks
   with what the host found for each (the turn's `mentions` event); a pick the host
   found gone or not valid is marked when it is in the input again. */

import { uiText } from '../../utils/strings'
import { mentionLabel } from '../hostv2/mention/model'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import type { DesignerAnswers, Question, SentMention, SelectionState, MissingTool } from '../../types/api'
import type { FetchCandidates, Mention } from '../../types/hostv2'
import { MentionInput } from '../hostv2/mention'
import { TraceTimeline } from '../hostv2/trace'
import Markdown from '../shared/Markdown'
import PlanChecklist from './PlanChecklist'
import ToolDrawer from './ToolDrawer'
import QuestionForm from './QuestionForm'
import Capture from './Capture'
import { useSessionStore } from '../../store'
import { useSelectionState, type SelectionSupport } from './useSelectionState'
import SelectionWatch from './SelectionWatch'
import type { Transcript } from './flow'
import { questionBadge } from './format'
import { distinctMentions, fetchCandidates as fetchFromHost, mentionNote, tooManyPicks, unavailableInInput } from './mentions'

interface Props {
  onSelectionAnswer?: (askId: string, items: Mention[]) => void
  support?: SelectionSupport
  hiddenCaptures?: string[]
  onHide?: (id: string) => void
  onDestination?: (destination: 'authoring' | 'remote_mcp', request: string) => void
  onDismiss?: (index: number) => void
  renderCards?: (message: number) => ReactNode
  onSubmitForm?: (text: string, answers: DesignerAnswers) => void
  highlight?: number
  title: string
  transcript: Transcript
  questionMessage?: number
  questions?: Question[]
  onSend?: (text: string, mentions: Mention[], selection?: SelectionState) => void   // absent: a read-only mirror (the no-bridge side)
  onAnswer?: (question: Question, option: { label: string; value: unknown }) => void
  onReset?: () => void
  disabled?: boolean
  placeholder?: string
  hint?: string
  showTrace?: boolean                    // the tool activity above each reply (the bridge side)
  fetchCandidates?: FetchCandidates      // the picker's candidates (default: GET /api/v1/bridge/mentions)
  controls?: ReactNode                   // drawn next to the input, above Send (the chat mode)
}

function ToolQuestion({ form, choose }: { form: MissingTool; choose: (action: keyof MissingTool['choices']) => void }) {
  return <fieldset className="inline-form"><legend>{form.question}</legend>
    {(['authoring', 'remote_mcp', 'dismiss'] as const).map(key => <button key={key} type="button"
      onClick={() => choose(key)}>{form.choices[key]}</button>)}</fieldset>
}

function omissionReason(reason: string) {
  if (reason === 'disabled') return uiText("unavailable for selection")
  if (reason === 'not_found') return uiText("no longer in the model")
  return uiText("could not be verified ({0})", reason)
}

function SentMentions({ mentions }: { mentions: SentMention[] }) {
  return (
    <div className="chat-mentions">
      {mentions.map((m, i) => (
        <span key={i} className={`chat-mention is-${m.status}`} title={mentionNote(m)}>
          {mentionLabel(m)}<span className="chat-mention-note">{mentionNote(m)}</span>
        </span>
      ))}
    </div>
  )
}

export default function ChatPanel({ title, transcript, questionMessage, questions = [], onSend, onAnswer, onReset, disabled, placeholder, hint,
  showTrace, fetchCandidates = fetchFromHost, controls, renderCards, onSubmitForm, highlight,
  onSelectionAnswer, hiddenCaptures = [], onHide, support = 'offline', onDestination, onDismiss }: Props) {
  const [input, setInput] = useState('')
  const [mentions, setMentions] = useState<Mention[]>([])
  const [notice, setNotice] = useState('')
  const [composerSession, setComposerSession] = useState(0)
  const device = useSessionStore(s => s.deviceId)
  const asking = transcript.messages.findLast(m => m.form?.selection && !m.answered)?.form
  const [finishedWatch, setFinishedWatch] = useState<string>()
  const reading = useSelectionState(device, support,
    !onSend || !!disabled || transcript.streaming || (!!asking && finishedWatch !== asking.ask_id), transcript.selectionRevision)
  const selected = reading?.value
  const [removed, setRemoved] = useState('')
  const identity = JSON.stringify([device, selected?.document_id, selected?.stamp])
  const attachSelection = selected && selected.count > 0 && !selected.assistant_set && removed !== identity
  const current = attachSelection ? { count: selected.count, stamp: selected.stamp, document_id: selected.document_id } : undefined
  const retained = transcript.messages.flatMap(m => m.images ?? []).slice(-32)
  const recent = retained.slice(-2)
  const scroll = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scroll.current?.scrollTo({ top: scroll.current.scrollHeight })
  }, [transcript.messages])

  useEffect(() => {
    if (!highlight) return
    const card = scroll.current?.querySelector<HTMLElement>('[data-active-card="true"]')
    card?.scrollIntoView?.({ block: 'nearest' })
    card?.animate?.([{ outline: '3px solid currentColor' }, { outline: '3px solid transparent' }], 2200)
  }, [highlight])

  /* A message with more picks than the host takes is not sent: the box keeps it and says why. */
  const send = (text: string, picked: Mention[]) => {
    const message = text.trim()
    if (!message || !onSend || disabled) return
    const problem = tooManyPicks(picked)
    if (problem) {
      setNotice(problem)
      return
    }
    setNotice('')
    setInput('')
    setMentions([])
    if (current) onSend(message, distinctMentions(picked), current)
    else onSend(message, distinctMentions(picked))
    setRemoved('')
  }

  return (
    <div className="card chat-card">
      <div className="chat-head">
        <span className="label-text" style={{ margin: 0 }}>{title}</span>
        <span className="muted small-mono">
          {transcript.sessionId ? `${uiText('session')} ${transcript.sessionId.slice(0, 8)}` : uiText("no session yet")}
          {transcript.streaming ? uiText(" | streaming") : ''}
        </span>
      </div>
      <div className="chat-scroll" ref={scroll}>
        {transcript.messages.length === 0 && hint && <p className="section-copy small chat-hint">{hint}</p>}
        {transcript.messages.map((m, i) => (
          <Fragment key={i}>
            {showTrace && m.role === 'assistant' && m.trace && m.trace.length > 0 && <TraceTimeline events={m.trace} />}
            <div className={`chat-bubble ${m.role}`}>
              {m.role === 'assistant'
                ? <div className="markdown-body"><Markdown>{m.content || (m.form || m.plan || m.missingTool || m.images?.length ? '' : '...')}</Markdown></div>
                : m.content}
              {m.role === 'user' && m.mentions && m.mentions.length > 0 && <SentMentions mentions={m.mentions} />}
              {m.trace?.map(evt => evt.type === 'tool' && evt.read && <details key={evt.id}>
                <summary>{evt.read.result.ui_only ? (evt.read.result.success === false
                  ? uiText("Revit action failed: ") : uiText("Done in Revit: ")) : evt.read.result.success === false
                  ? uiText("Read failed: ")
                  : uiText("Read: ")}{evt.read.name}</summary>
                <pre>{JSON.stringify(evt.read.result, null, 2)}</pre>
              </details>)}
              {m.images?.map(img => <Capture key={img.capture_id} image={img} hidden={hiddenCaptures.includes(img.capture_id)}
                expired={!retained.includes(img)} older={!recent.includes(img)} toggle={() => onHide?.(img.capture_id)} />)}
              {m.missingTool && <ToolQuestion form={m.missingTool} choose={action => {
                if (action !== 'dismiss') onDestination?.(action, m.missingTool!.request)
                onDismiss?.(i)
              }} />}
              {m.currentSelection !== undefined && <span className="chat-mention">{uiText('Current selection')} · {m.currentSelection}</span>}
              {Object.entries(m.selectionOmissions ?? {}).map(([reason, count]) =>
                <p key={reason} role="status">{uiText("{0} selected elements left out: {1}", count, omissionReason(reason))}</p>)}
              {m.imageNotice && <p role="status">{m.imageNotice === 'vision_unavailable'
                ? uiText("Image input is disabled for this planner. Your text was sent.")
                : uiText('Capture context is disabled')}</p>}
              {m.plan && <PlanChecklist plan={m.plan} results={m.stepResults} />}
              {m.role === 'assistant' && renderCards?.(i)}
              {i === questionMessage && !m.form && questions.length > 0 && onAnswer && (
                <div className="question-list">
                  {questions.map(q => {
                    const badge = questionBadge(q.kind, q.blocks)
                    return (
                      <div key={q.id} className="question">
                        <div className="question-text">
                          {badge && <span className={`source-pill kind-pill kind-${q.kind}`} title={badge.title}>{badge.label}</span>}
                          {q.text}
                        </div>
                        {q.why && <div className="muted small-mono">{q.why}</div>}
                        {q.options.length > 0
                          ? <div className="option-row">
                              {q.options.map((o, i) => (
                                <button key={i} className="option-button" disabled={disabled} onClick={() => onAnswer(q, o)} title={o.source ? uiText("from {0}", o.source) : undefined}>
                                  {o.label}
                                </button>
                              ))}
                            </div>
                          : <div className="muted small-mono">{uiText("no options from the model: answer in the box below")}</div>}
                      </div>
                    )
                  })}
                </div>
              )}
              {m.form?.selection ? (!m.answered && <SelectionWatch key={m.form.ask_id} sessionId={transcript.sessionId}
                askId={m.form.ask_id} question={m.form.selection} onStart={() => setFinishedWatch(undefined)} onFinish={() => setFinishedWatch(m.form!.ask_id)} disabled={disabled} legacy={support === 'legacy'}
                onPick={items => onSelectionAnswer?.(m.form!.ask_id, items)} />)
                : m.form && onSubmitForm && <QuestionForm key={m.form.ask_id} form={m.form} answered={m.answered}
                  disabled={disabled} onAnswer={onSubmitForm} />}
            </div>
          </Fragment>
        ))}

      </div>
      {onSend && (
        <div className="chat-input-row">
          {selected && <div className="current-selection" role="status">
            {uiText("{0} selected in Revit", selected.count)}
            {attachSelection && <button type="button" aria-label={uiText('Remove current selection')} onClick={() => setRemoved(identity)}>
              {uiText('Current selection')} ×</button>}
          </div>}
          <MentionInput value={input} mentions={mentions}
            onChange={(text, picked) => { setInput(text); setMentions(picked); setNotice('') }}
            onSubmit={send} fetchCandidates={fetchCandidates} disabled={disabled}
            placeholder={placeholder} invalid={unavailableInInput(transcript.messages, mentions)} />
          <div className="chat-buttons">
            {controls}
            {!asking && (support === 'legacy' || reading?.legacy) && <SelectionWatch legacy key={composerSession} sessionId={transcript.sessionId} disabled={disabled} onPick={items => {
              const problem = tooManyPicks([...mentions, ...items])
              if (problem) { setNotice(problem); return }
              setInput(text => [text.trimEnd(), ...items.map(mentionLabel)].filter(Boolean).join(' ') + ' ')
              setMentions(picked => [...picked, ...items])
              setNotice('')
            }} />}
            {showTrace && <ToolDrawer onExample={text => { setInput(text); setMentions([]); setNotice('') }} />}
            <button className="btn-primary" onClick={() => send(input, mentions)} disabled={disabled || !input.trim()}>{transcript.streaming ? uiText("Working...") : uiText("Send")}</button>
            {onReset && <button className="btn-ghost" onClick={() => {
              setComposerSession(previous => previous + 1)
              onReset()
            }} disabled={disabled || transcript.streaming}>{uiText("New session")}</button>}
          </div>
        </div>
      )}
      {onSend && notice && <p className="chat-notice" role="alert">{notice}</p>}
    </div>
  )
}
