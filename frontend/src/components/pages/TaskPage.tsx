/* Task: talk to the model, review the C# it proposes, run it in Revit, keep what worked.
   Rebuilt as the spec-driven task page in a later phase; this is the thin host loop. */

import { useCallback, useEffect, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { bridgeApi } from '../../api/bridge'
import { chatStream } from '../../api/chat'
import { useSessionStore } from '../../store'
import type { ChatMessage, ToolParam } from '../../types/api'
import { extractCSharp } from '../../utils/code'
import { getErrorMessage, isAbortError } from '../../utils/errors'
import Accordion from '../shared/Accordion'
import ExecResult, { type ExecOutcome } from '../shared/ExecResult'
import StepIndicator from '../shared/StepIndicator'

const STEPS = ['Ask', 'Review code', 'Execute', 'Save as pack']

export default function TaskPage() {
  const { chatSessionId, setChatSessionId } = useSessionStore()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [picking, setPicking] = useState(false)
  const [code, setCode] = useState('')
  const [step, setStep] = useState(1)
  const [executing, setExecuting] = useState(false)
  const [result, setResult] = useState<ExecOutcome | null>(null)
  const [packName, setPackName] = useState('')
  const [packDesc, setPackDesc] = useState('')
  const [packParams, setPackParams] = useState('[]')
  const [saveStatus, setSaveStatus] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const lastQueryRef = useRef('')

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])
  useEffect(() => () => { abortRef.current?.abort() }, [])

  const send = useCallback(async () => {
    const text = input.trim()
    if (!text || streaming) return
    setInput('')
    lastQueryRef.current = text
    setMessages(prev => [...prev, { role: 'user', content: text }])
    setStreaming(true)
    setResult(null)
    setSaveStatus('')
    const abort = new AbortController()
    abortRef.current = abort
    let reply = ''
    try {
      for await (const evt of chatStream(text, chatSessionId, abort.signal)) {
        if (evt.type === 'session') { setChatSessionId(evt.id); continue }
        if (evt.type === 'error') { reply += `\n\n**Model error:** ${evt.detail}`; break }
        reply += evt.text
        const snapshot = reply
        setMessages(prev => {
          const copy = [...prev]
          if (copy.length && copy[copy.length - 1].role === 'assistant') copy[copy.length - 1] = { role: 'assistant', content: snapshot }
          else copy.push({ role: 'assistant', content: snapshot })
          return copy
        })
      }
      if (!reply) setMessages(prev => [...prev, { role: 'assistant', content: '(no response)' }])
      const proposed = extractCSharp(reply)
      if (proposed) { setCode(proposed); setStep(2) }
    } catch (e: unknown) {
      if (!isAbortError(e)) setMessages(prev => [...prev, { role: 'assistant', content: `Error: ${getErrorMessage(e)}` }])
    } finally {
      setStreaming(false)
    }
  }, [input, streaming, chatSessionId, setChatSessionId])

  const pickInRevit = async () => {
    setPicking(true)
    try {
      const res = await bridgeApi.triggerSelection()
      const lines = (res.elements || []).map(el => {
        const id = String(el.Id ?? el.id ?? '')
        const name = String(el.Name ?? el.name ?? '')
        const cat = String(el.Category ?? el.category ?? '')
        return `${cat ? cat + ': ' : ''}${name} (ID ${id})`
      })
      if (lines.length) setInput(prev => `${prev}${prev ? '\n' : ''}Selected in Revit: ${lines.join('; ')}`)
      else setInput(prev => `${prev}${prev ? '\n' : ''}(nothing selected in Revit)`)
    } catch (e: unknown) {
      setInput(prev => `${prev}${prev ? '\n' : ''}(pick failed: ${getErrorMessage(e)})`)
    } finally {
      setPicking(false)
    }
  }

  const execute = async () => {
    if (!code.trim() || executing) return
    setExecuting(true)
    setStep(3)
    try {
      const res = await bridgeApi.execute(code)
      let data: unknown = res.result
      if (typeof data === 'string') { try { data = JSON.parse(data) } catch { /* plain text result */ } }
      setResult(res.success ? { ok: true, data } : { ok: false, data: null, error: res.error })
    } catch (e: unknown) {
      setResult({ ok: false, data: null, error: getErrorMessage(e) })
    } finally {
      setExecuting(false)
    }
  }

  const save = async () => {
    if (!packName.trim() || !code.trim()) { setSaveStatus('Enter a pack name first.'); return }
    let parameters: ToolParam[] = []
    try {
      const parsed = JSON.parse(packParams || '[]')
      if (!Array.isArray(parsed)) throw new Error('parameters must be a JSON array')
      parameters = parsed
    } catch (e: unknown) {
      setSaveStatus(`Parameters JSON: ${getErrorMessage(e)}`)
      return
    }
    try {
      const res = await bridgeApi.solidify({
        name: packName.trim(), code, description: packDesc, parameters, tags: [], source_query: lastQueryRef.current,
      })
      setSaveStatus(`Saved as '${res.name}'${res.revit_synced ? ' and registered in Revit' : ''}. Find it on the Capabilities page.`)
      setStep(4)
    } catch (e: unknown) {
      setSaveStatus(`Save failed: ${getErrorMessage(e)}`)
    }
  }

  return (
    <div className="page">
      <StepIndicator steps={STEPS} current={step} />

      <section className="card chat-card">
        <div className="chat-scroll">
          {messages.length === 0 && (
            <p className="section-copy" style={{ padding: 16 }}>
              Describe what you want in the model. The model answers, asks for what it cannot know, and proposes
              C# in a code block. You review that code, run it, and can keep it as a capability pack.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div className={`chat-bubble ${m.role}`}>
                {m.role === 'assistant'
                  ? <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown></div>
                  : m.content}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>
        <div className="chat-input-row">
          <textarea
            className="input-field"
            rows={2}
            placeholder="e.g. Create a 200 mm wall from (0,0) to (6000,0) on the level I selected"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            disabled={streaming}
          />
          <div className="chat-buttons">
            <button onClick={send} disabled={streaming || !input.trim()} className="btn-primary">{streaming ? 'Thinking...' : 'Send'}</button>
            <button onClick={pickInRevit} disabled={picking || streaming} className="btn-secondary" title="Select an element in Revit and paste it into the message">
              {picking ? 'Pick in Revit...' : 'Pick in Revit'}
            </button>
            {streaming
              ? <button onClick={() => abortRef.current?.abort()} className="btn-ghost">Stop</button>
              : <button onClick={() => { setMessages([]); setChatSessionId(''); setCode(''); setResult(null); setStep(1) }} className="btn-ghost">New conversation</button>}
          </div>
        </div>
      </section>

      <Accordion title="Step 2: Review the proposed code" defaultOpen>
        <textarea
          className="code-editor"
          value={code}
          onChange={e => setCode(e.target.value)}
          placeholder="C# from the model appears here. You can also paste your own."
          spellCheck={false}
        />
        <p className="section-copy small">
          Runs as the body of <code>Execute(Document document, object[] parameters)</code> with a transaction open.
          The server reviews it against the sandbox rules before anything reaches Revit.
        </p>
      </Accordion>

      <Accordion title="Step 3: Execute in Revit" defaultOpen>
        <button onClick={execute} disabled={executing || !code.trim()} className="btn-primary">
          {executing ? 'Executing...' : 'Execute in Revit'}
        </button>
        {result && <ExecResult result={result} />}
      </Accordion>

      <Accordion title="Step 4: Save as a capability pack">
        <div className="form-grid">
          <label>
            <span className="label-text">Pack name</span>
            <input className="input-field" placeholder="e.g. create_wall_by_points" value={packName} onChange={e => setPackName(e.target.value)} />
          </label>
          <label>
            <span className="label-text">Description</span>
            <input className="input-field" placeholder="What it does" value={packDesc} onChange={e => setPackDesc(e.target.value)} />
          </label>
          <label className="form-wide">
            <span className="label-text">Parameters (JSON array; use {'{name}'} placeholders in the code)</span>
            <textarea className="code-editor small" value={packParams} onChange={e => setPackParams(e.target.value)} spellCheck={false}
              placeholder='[{"name": "level_name", "type": "string", "source": "query:levels", "choices_from": "levels"}]' />
          </label>
        </div>
        <button onClick={save} className="btn-primary" disabled={!code.trim()}>Save pack</button>
        {saveStatus && <p className="status-line">{saveStatus}</p>}
      </Accordion>
    </div>
  )
}
