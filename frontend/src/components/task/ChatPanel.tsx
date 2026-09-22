/* Panels 2 and 3: the brief and the model's questions; the same transcript
   without the bridge when the comparison is on. */

import { useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Question } from '../../types/api'
import type { Transcript } from './flow'

interface Props {
  title: string
  transcript: Transcript
  questions?: Question[]
  onSend?: (text: string) => void        // absent: a read-only mirror (the no-bridge side)
  onAnswer?: (question: Question, option: { label: string; value: unknown }) => void
  onReset?: () => void
  disabled?: boolean
  placeholder?: string
  hint?: string
}

export default function ChatPanel({ title, transcript, questions = [], onSend, onAnswer, onReset, disabled, placeholder, hint }: Props) {
  const [input, setInput] = useState('')
  const scroll = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scroll.current?.scrollTo({ top: scroll.current.scrollHeight })
  }, [transcript.messages])

  const send = () => {
    const text = input.trim()
    if (!text || !onSend || disabled) return
    setInput('')
    onSend(text)
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
          <div key={i} className={`chat-bubble ${m.role}`}>
            {m.role === 'assistant'
              ? <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content || '...'}</ReactMarkdown></div>
              : m.content}
          </div>
        ))}
        {questions.length > 0 && onAnswer && (
          <div className="question-list">
            {questions.map(q => (
              <div key={q.id} className="question">
                <div className="question-text">{q.text}</div>
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
            ))}
          </div>
        )}
      </div>
      {onSend && (
        <div className="chat-input-row">
          <textarea className="input-field" value={input} onChange={e => setInput(e.target.value)}
            placeholder={placeholder} disabled={disabled}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
          <div className="chat-buttons">
            <button className="btn-primary" onClick={send} disabled={disabled || !input.trim()}>{transcript.streaming ? 'Working...' : 'Send'}</button>
            {onReset && <button className="btn-ghost" onClick={onReset} disabled={transcript.streaming}>New session</button>}
          </div>
        </div>
      )}
    </div>
  )
}
