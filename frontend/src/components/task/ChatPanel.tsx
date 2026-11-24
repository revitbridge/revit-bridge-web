/* Panels 2 and 3: the brief and the model's questions; the same transcript
   without the bridge when the comparison is on. A question's kind is a badge
   and what it holds up its tooltip: shown only, the spec card gates on
   reconcile.ready. With showTrace each reply has its tool activity above it
   (the trace of its turn: one row per tool call, expandable).

   The input is the mention input: `@` offers @link, @type and @level, and a picked
   model item is one tag (components/hostv2/mention). A sent message shows its picks
   with what the host found for each (the turn's `mentions` event); a pick the host
   found gone or not valid is marked when it is in the input again. */

import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import type { Question, SentMention } from '../../types/api'
import type { FetchCandidates, Mention } from '../../types/hostv2'
import { MentionInput } from '../hostv2/mention'
import { TraceTimeline } from '../hostv2/trace'
import Markdown from '../shared/Markdown'
import type { Transcript } from './flow'
import { questionBadge } from './format'
import { distinctMentions, fetchCandidates as fetchFromHost, mentionNote, tooManyPicks, unavailableInInput } from './mentions'

interface Props {
  title: string
  transcript: Transcript
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

function SentMentions({ mentions }: { mentions: SentMention[] }) {
  return (
    <div className="chat-mentions">
      {mentions.map((m, i) => (
        <span key={i} className={`chat-mention is-${m.status}`} title={mentionNote(m)}>
          @{m.kind}:{m.name}<span className="chat-mention-note">{mentionNote(m)}</span>
        </span>
      ))}
    </div>
  )
}

export default function ChatPanel({ title, transcript, questions = [], onSend, onAnswer, onReset, disabled, placeholder, hint,
  showTrace, fetchCandidates = fetchFromHost, controls }: Props) {
  const [input, setInput] = useState('')
  const [mentions, setMentions] = useState<Mention[]>([])
  const [notice, setNotice] = useState('')
  const scroll = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scroll.current?.scrollTo({ top: scroll.current.scrollHeight })
  }, [transcript.messages])

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
                ? <div className="markdown-body"><Markdown>{m.content || '...'}</Markdown></div>
                : m.content}
              {m.role === 'user' && m.mentions && m.mentions.length > 0 && <SentMentions mentions={m.mentions} />}
            </div>
          </Fragment>
        ))}
        {questions.length > 0 && onAnswer && (
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
      </div>
      {onSend && (
        <div className="chat-input-row">
          <MentionInput value={input} mentions={mentions}
            onChange={(text, picked) => { setInput(text); setMentions(picked); setNotice('') }}
            onSubmit={send} fetchCandidates={fetchCandidates} disabled={disabled}
            placeholder={placeholder} invalid={unavailableInInput(transcript.messages, mentions)} />
          <div className="chat-buttons">
            {controls}
            <button className="btn-primary" onClick={() => send(input, mentions)} disabled={disabled || !input.trim()}>{transcript.streaming ? 'Working...' : 'Send'}</button>
            {onReset && <button className="btn-ghost" onClick={onReset} disabled={transcript.streaming}>New session</button>}
          </div>
        </div>
      )}
      {onSend && notice && <p className="chat-notice" role="alert">{notice}</p>}
    </div>
  )
}
