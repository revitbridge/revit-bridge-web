/* Panels 2 and 3: the brief and the model's questions; the same transcript
   without the bridge when the comparison is on. A question's kind is a badge
   and what it holds up its tooltip: shown only, the spec card gates on
   reconcile.ready. With showTrace each reply has its tool activity above it
   (the trace of its turn: one row per tool call, expandable).

   The input is the mention input: `@` offers @link, @type, @level and @element, and a picked
   model item is one tag (components/hostv2/mention). A sent message shows its picks
   with what the host found for each (the turn's `mentions` event); a pick the host
   found gone or not valid is marked when it is in the input again. */

import { mentionLabel } from '../hostv2/mention/model'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import type { DesignerAnswers, Question, SentMention, ViewImage } from '../../types/api'
import type { FetchCandidates, Mention } from '../../types/hostv2'
import { MentionInput } from '../hostv2/mention'
import { TraceTimeline } from '../hostv2/trace'
import Markdown from '../shared/Markdown'
import PlanChecklist from './PlanChecklist'
import ToolDrawer from './ToolDrawer'
import { browserLanguage } from '../../utils/language'
import QuestionForm from './QuestionForm'
import SelectionWatch from './SelectionWatch'
import type { Transcript } from './flow'
import { questionBadge } from './format'
import { distinctMentions, fetchCandidates as fetchFromHost, mentionNote, tooManyPicks, unavailableInInput } from './mentions'

interface Props {
  onSelectionAnswer?: (askId: string, items: Mention[]) => void
  attachment?: string | null
  onAttach?: (id: string | null) => void
  renderCards?: (message: number) => ReactNode
  onSubmitForm?: (text: string, answers: DesignerAnswers) => void
  highlight?: number
  title: string
  transcript: Transcript
  questionMessage?: number
  questions?: Question[]
  onSend?: (text: string, mentions: Mention[]) => void   // absent: a read-only mirror (the no-bridge side)
  onAnswer?: (question: Question, option: { label: string; value: unknown }) => void
  onReset?: () => void
  disabled?: boolean
  placeholder?: string
  hint?: string
  showTrace?: boolean                    // the tool activity above each reply (the bridge side)
  fetchCandidates?: FetchCandidates      // the picker's candidates (default: GET /api/v1/bridge/mentions)
  controls?: ReactNode                   // drawn next to the input, above Send (the chat mode)
}

function Capture({ image, attached, toggle }: { image: ViewImage; attached: boolean; toggle: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const zh = browserLanguage() === 'zh'
  const src = 'data:image/png;base64,' + image.png_base64
  return <figure className="chat-capture">
    <button type="button" aria-label={zh ? '放大视图' : 'Enlarge view'} onClick={() => dialog.current?.showModal()}>
      <img src={src} alt={image.view_name} /></button>
    <figcaption>{image.view_name} · {image.width} × {image.height}
      <label><input type="checkbox" role="switch" checked={attached} onChange={toggle} />
        {zh ? '附给模型（下一条消息）' : 'Attach to model (next message)'}</label></figcaption>
    <dialog ref={dialog} aria-label={image.view_name}><button type="button" onClick={() => dialog.current?.close()}>
      {zh ? '关闭' : 'Close'}</button><img src={src} alt={image.view_name} /></dialog>
  </figure>
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
  onSelectionAnswer, attachment, onAttach }: Props) {
  const zh = browserLanguage() === 'zh'
  const [input, setInput] = useState('')
  const [mentions, setMentions] = useState<Mention[]>([])
  const [notice, setNotice] = useState('')
  const [composerSession, setComposerSession] = useState(0)
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
    onSend(message, distinctMentions(picked))
  }

  return (
    <div className="card chat-card">
      <div className="chat-head">
        <span className="label-text" style={{ margin: 0 }}>{title}</span>
        <span className="muted small-mono">
          {transcript.sessionId ? `session ${transcript.sessionId.slice(0, 8)}` : 'no session yet'}
          {transcript.streaming ? ' | streaming' : ''}
        </span>
      </div>
      <div className="chat-scroll" ref={scroll}>
        {transcript.messages.length === 0 && hint && <p className="section-copy small chat-hint">{hint}</p>}
        {transcript.messages.map((m, i) => (
          <Fragment key={i}>
            {showTrace && m.role === 'assistant' && m.trace && m.trace.length > 0 && <TraceTimeline events={m.trace} />}
            <div className={`chat-bubble ${m.role}`}>
              {m.role === 'assistant'
                ? <div className="markdown-body"><Markdown>{m.content || (m.form || m.plan || m.images?.length ? '' : '...')}</Markdown></div>
                : m.content}
              {m.role === 'user' && m.mentions && m.mentions.length > 0 && <SentMentions mentions={m.mentions} />}
              {m.trace?.map(evt => evt.type === 'tool' && evt.read && <details key={evt.id}>
                <summary>{evt.read.result.ui_only ? (evt.read.result.success === false
                  ? (zh ? 'Revit 操作失败：' : 'Revit action failed: ') : (zh ? '已在 Revit 中：' : 'Done in Revit: ')) : evt.read.result.success === false
                  ? (browserLanguage() === 'zh' ? '读取失败：' : 'Read failed: ')
                  : (browserLanguage() === 'zh' ? '已读取：' : 'Read: ')}{evt.read.name}</summary>
                <pre>{JSON.stringify(evt.read.result, null, 2)}</pre>
              </details>)}
              {m.images?.map(img => <Capture key={img.capture_id} image={img} attached={attachment === img.capture_id}
                toggle={() => onAttach?.(attachment === img.capture_id ? null : img.capture_id)} />)}
              {m.imageNotice && <p role="status">{m.imageNotice === 'vision_unavailable'
                ? (zh ? '当前规划模型未启用图像输入，已仅发送文字。' : 'Image input is disabled for this planner. Your text was sent.')
                : (zh ? '截图已不可用，请重新截图。' : 'This capture is unavailable. Capture the view again.')}</p>}
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
                                <button key={i} className="option-button" disabled={disabled} onClick={() => onAnswer(q, o)} title={o.source ? `from ${o.source}` : undefined}>
                                  {o.label}
                                </button>
                              ))}
                            </div>
                          : <div className="muted small-mono">no options from the model: answer in the box below</div>}
                      </div>
                    )
                  })}
                </div>
              )}
              {m.form?.selection ? <SelectionWatch key={m.form.ask_id} sessionId={transcript.sessionId}
                question={m.form.selection} disabled={disabled || !!m.answered}
                onPick={items => onSelectionAnswer?.(m.form!.ask_id, items)} />
                : m.form && onSubmitForm && <QuestionForm key={m.form.ask_id} form={m.form} answered={m.answered}
                  disabled={disabled} onAnswer={onSubmitForm} />}
            </div>
          </Fragment>
        ))}

      </div>
      {onSend && (
        <div className="chat-input-row">
          <MentionInput value={input} mentions={mentions}
            onChange={(text, picked) => { setInput(text); setMentions(picked); setNotice('') }}
            onSubmit={send} fetchCandidates={fetchCandidates} disabled={disabled}
            placeholder={placeholder} invalid={unavailableInInput(transcript.messages, mentions)} />
          <div className="chat-buttons">
            {controls}
            <SelectionWatch key={composerSession} sessionId={transcript.sessionId} disabled={disabled} onPick={items => {
              const problem = tooManyPicks([...mentions, ...items])
              if (problem) { setNotice(problem); return }
              setInput(text => [text.trimEnd(), ...items.map(mentionLabel)].filter(Boolean).join(' ') + ' ')
              setMentions(picked => [...picked, ...items])
              setNotice('')
            }} />
            {showTrace && <ToolDrawer onExample={text => { setInput(text); setMentions([]); setNotice('') }} />}
            <button className="btn-primary" onClick={() => send(input, mentions)} disabled={disabled || !input.trim()}>{transcript.streaming ? 'Working...' : 'Send'}</button>
            {onReset && <button className="btn-ghost" onClick={() => {
              setComposerSession(previous => previous + 1)
              onReset()
            }} disabled={disabled || transcript.streaming}>New session</button>}
          </div>
        </div>
      )}
      {onSend && notice && <p className="chat-notice" role="alert">{notice}</p>}
    </div>
  )
}
