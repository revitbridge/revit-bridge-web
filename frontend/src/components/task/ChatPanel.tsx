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
import Icon from '../shared/Icon'
import ProjectContext from './ProjectContext'
import { useContextLayout } from '../shared/usePanelLayout'
import ToolDrawer from './ToolDrawer'
import QuestionForm from './QuestionForm'
import Capture from './Capture'
import { useSessionStore } from '../../store'
import { useSelectionState, type SelectionSupport } from './useSelectionState'
import SelectionWatch from './SelectionWatch'
import SketchPad from './SketchPad'
import type { SketchDraft } from '../../types/api'
import type { Transcript } from './flow'
import { questionBadge } from './format'
import { distinctMentions, fetchCandidates as fetchFromHost, fetchPreview as previewFromHost, mentionNote, tooManyPicks, unavailableInInput } from './mentions'

interface Props {
  initialInput?: string
  context?: ReactNode
  compare?: boolean
  footer?: ReactNode
  stop?: ReactNode
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
  onSend?: (text: string, mentions: Mention[], selection?: SelectionState, sketch?: SketchDraft) => void   // absent: a read-only mirror (the no-bridge side)
  sketching?: boolean                    // offer "Sketch on view" (a line drawn on a capture of the plan view goes with the message)
  onAnswer?: (question: Question, option: { label: string; value: unknown }) => void
  onReset?: () => void
  disabled?: boolean
  placeholder?: string
  hint?: string
  showTrace?: boolean                    // the tool activity above each reply (the bridge side)
  fetchCandidates?: FetchCandidates      // the picker's candidates (default: GET /api/v1/bridge/mentions)
  controls?: ReactNode
}

function ToolQuestion({ form, choose }: { form: MissingTool; choose: (action: keyof MissingTool['choices']) => void }) {
  return <fieldset className="inline-form"><legend>{form.question}</legend>
    {(['authoring', 'remote_mcp', 'dismiss'] as const).map(key => <button key={key} type="button"
      className={key === 'authoring' ? 'btn-primary' : 'btn-ghost'} onClick={() => choose(key)}>{form.choices[key]}</button>)}</fieldset>
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

export default function ChatPanel({ initialInput = '', title, transcript, questionMessage, questions = [], onSend, onAnswer, onReset, disabled, placeholder, hint, sketching,
  context, compare = false, footer, stop, showTrace, fetchCandidates = fetchFromHost, controls, renderCards, onSubmitForm, highlight,
  onSelectionAnswer, hiddenCaptures = [], onHide, support = 'offline', onDestination, onDismiss }: Props) {
  const layout = useContextLayout(compare)
  const [input, setInput] = useState(initialInput)
  const [mentions, setMentions] = useState<Mention[]>([])
  const [sketch, setSketch] = useState<SketchDraft | null>(null)
  const [sketchOpen, setSketchOpen] = useState(false)
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
    if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) card?.animate?.([{ outline: '3px solid currentColor' }, { outline: '3px solid transparent' }], 180)
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
    const drawn = sketch ?? undefined
    setSketch(null)
    if (drawn) onSend(message, distinctMentions(picked), current || undefined, drawn)
    else if (current) onSend(message, distinctMentions(picked), current)
    else onSend(message, distinctMentions(picked))
    setRemoved('')
  }

  return (
    <>
    <section className="chat-card">
      <div className="chat-head">
        <div className="chat-heading"><h2>{title}</h2>
        <span className="session-id">
          {transcript.sessionId ? `${uiText('session')} ${transcript.sessionId.slice(0, 8)}` : uiText("no session yet")}
          {transcript.streaming ? uiText(" | streaming") : ''}
        </span></div>
        <div className="toolbar">
          {onReset && <button className="btn-ghost" onClick={() => {
            setComposerSession(previous => previous + 1)
            onReset()
          }} disabled={disabled || transcript.streaming}><Icon name="plus" />{uiText("New session")}</button>}
          {context && <button type="button" className="context-trigger btn-secondary" aria-controls="project-context"
            aria-expanded={layout.open} onClick={layout.toggle}><Icon name="panel" />{uiText('Project context')}</button>}
        </div>
      </div>
      <div className="chat-scroll" ref={scroll}>
        {transcript.messages.length === 0 && hint && <div className="chat-hint"><svg className="conversation-mark" aria-hidden="true" viewBox="0 0 160 110" fill="none" strokeLinejoin="round">
          <g stroke="var(--line)" strokeDasharray="3 4"><path d="M16 62v18m62-45v19m63-24v30M80 81v23" /></g>
          <g stroke="var(--accent)" strokeWidth="1.5">
            <path fill="var(--tint)" d="m16 70 63-34 63 28-63 36z" />
            <path fill="var(--panel)" d="m21 58 58-32 58 26-58 33z" />
            <path fill="var(--tint)" d="M21 58V35l58-31v22z" />
            <path fill="var(--bg2)" d="M79 4l58 26v22L79 26z" />
            <path d="m35 50 34 15 52-29M69 65V48l22-12m-22 12-12-5M91 36l16 7" />
            <path strokeWidth=".8" d="m87 14 42 18v10L87 24zM96 18v10m12-5v10m12-4v9" />
          </g></svg><h2>{uiText("Conversation")}</h2><p>{hint}</p></div>}
        {transcript.messages.map((m, i) => (
          <Fragment key={i}>
            {showTrace && m.role === 'assistant' && m.trace && m.trace.length > 0 && <TraceTimeline events={m.trace} />}
            {m.role === 'assistant' && <div className="message-role"><Icon name="mark" />
              {uiText(m.trace?.findLast(e => e.type === 'stage')?.role ?? 'planner')}</div>}
            <div className={`chat-bubble ${m.role}`}>
              {m.role === 'assistant'
                ? <div className="markdown-body"><Markdown>{m.content || (m.form || m.plan || m.missingTool || m.images?.length ? '' : '...')}</Markdown></div>
                : m.content}
              {m.role === 'user' && m.mentions && m.mentions.length > 0 && <SentMentions mentions={m.mentions} />}
              {m.role === 'user' && m.sketch && <div className="chat-sketch">
                {m.sketch.image && <img src={'data:image/png;base64,' + m.sketch.image} alt={uiText("Sketch on {0}", m.sketch.view_name)} />}
                <span>{uiText("Sketch on {0}: {1} line(s)", m.sketch.view_name, m.sketch.lines)}</span></div>}
              {m.trace?.map(evt => evt.type === 'tool' && evt.read && <details className="read-row" key={evt.id}>
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
          {sketchOpen && <SketchPad onClose={() => setSketchOpen(false)} onUse={draft => {
            setSketch(draft); setSketchOpen(false); setNotice('')
            if (!input.trim()) setInput(uiText("Draw a wall along the sketched line."))
          }} />}
          {(selected || mentions.length > 0 || sketch) && <div className="composer-chips">
            {sketch && <span className="chip composer-sketch">
              {sketch.image && <img src={'data:image/png;base64,' + sketch.image} alt="" />}
              {uiText("Sketch on {0}: {1} line(s)", sketch.view_name, sketch.lines.length)}
              <button type="button" aria-label={uiText("Remove the sketch")} onClick={() => setSketch(null)}>×</button></span>}
            {selected && <div className="current-selection" role="status"><Icon name="select" />
              {uiText("{0} selected in Revit", selected.count)}
              {attachSelection && <button type="button" aria-label={uiText('Remove current selection')} onClick={() => setRemoved(identity)}>
                {uiText('Current selection')} ×</button>}
            </div>}
            {distinctMentions(mentions).map(m => <span className="chip" key={m.kind + ':' + m.id}>{mentionLabel(m)}</span>)}
          </div>}
          <MentionInput value={input} mentions={mentions}
            onChange={(text, picked) => { setInput(text); setMentions(picked); setNotice('') }}
            onSubmit={send} fetchCandidates={fetchCandidates} disabled={disabled}
            fetchPreview={fetchCandidates === fetchFromHost ? previewFromHost : undefined}
            placeholder={placeholder} invalid={unavailableInInput(transcript.messages, mentions)} />
          <div className="chat-buttons">
            <div className="composer-modes">{controls}</div>
            {!asking && (support === 'legacy' || reading?.legacy) && <SelectionWatch legacy key={composerSession} sessionId={transcript.sessionId} disabled={disabled} onPick={items => {
              const problem = tooManyPicks([...mentions, ...items])
              if (problem) { setNotice(problem); return }
              setInput(text => [text.trimEnd(), ...items.map(mentionLabel)].filter(Boolean).join(' ') + ' ')
              setMentions(picked => [...picked, ...items])
              setNotice('')
            }} />}
            <div className="composer-actions">
              {sketching && !asking && <button type="button" className="sketch-open" disabled={disabled || transcript.streaming}
                onClick={() => setSketchOpen(true)}>{sketch ? uiText("Redo sketch") : uiText("Sketch on view")}</button>}
              {showTrace && <ToolDrawer onExample={text => { setInput(text); setMentions([]); setNotice('') }} />}
              <button className="btn-primary" onClick={() => send(input, mentions)} disabled={disabled || !input.trim()}><Icon name="send" />{transcript.streaming ? uiText("Working...") : uiText("Send")}</button>{stop}
            </div>
          </div>
        </div>
      )}
      {onSend && notice && <p className="chat-notice" role="alert">{notice}</p>}
      {footer}
    </section>
    {context && <ProjectContext layout={layout} selection={selected?.count}>{context}</ProjectContext>}
    </>
  )
}
