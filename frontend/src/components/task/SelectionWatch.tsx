import { uiText } from '../../utils/strings'
import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { apiFetch, BridgeApiError } from '../../api/client'
import { deviceHeaders, useSessionStore } from '../../store'
import type { SelectionQuestion } from '../../types/api'
import type { Mention, MentionCandidates } from '../../types/hostv2'
import { elementId } from '../hostv2/mention/controller'
import { hasLineBreak } from '../hostv2/mention/model'
import { tooManyPicks } from './mentions'

const started = new WeakSet<SelectionQuestion>()
const path = '/api/v1/bridge/watch-selection'
type Reply = Partial<MentionCandidates> & {
  error?: string; timed_out?: boolean; document_changed?: boolean; cancelled?: boolean; filtered_out?: number
}

interface Props {
  askId?: string
  onFinish?: () => void
  sessionId: string | null; question?: SelectionQuestion; legacy?: boolean; disabled?: boolean; onPick: (items: Mention[]) => void
}

function newId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
}

export default function SelectionWatch(props: Props) {
  const device = useSessionStore(s => s.deviceId)
  return <WatchControl key={JSON.stringify([props.sessionId, device])} {...props} />
}

function WatchControl({ sessionId, question, disabled, onPick, legacy, onFinish, askId }: Props) {
  const [draftSession] = useState(newId)
  const mode = question?.mode ?? 'multiple'
  const category = question?.category ?? ''
  const [watching, setWatching] = useState(false)
  const [unsupported, setFallback] = useState(false)
  const fallback = legacy || unsupported
  const [message, setMessage] = useState('')
  const active = useRef<AbortController | null>(null)
  useEffect(() => {
    const stop = () => active.current?.abort()
    window.addEventListener('pagehide', stop)
    return () => { stop(); window.removeEventListener('pagehide', stop) }
  }, [])

  useEffect(() => { if (disabled) active.current?.abort() }, [disabled])

  async function select(current = false) {
    if (active.current || disabled) return
    const controller = new AbortController()
    active.current = controller
    const session = sessionId ?? draftSession
    const watch = newId()
    const headers = deviceHeaders()
    const cancelled = uiText("Selection cancelled.")
    const cancel = () => {
      setMessage(cancelled)
      if (!current) void apiFetch(path + '/' + encodeURIComponent(session) + '/' + watch,
        { method: 'DELETE', headers, keepalive: true }).catch(() => {})
    }
    controller.signal.addEventListener('abort', cancel, { once: true })
    setWatching(true)
    setMessage('')
    try {
      const reply = current ? await apiFetch<Reply>('/api/v1/bridge/mentions?kind=element&source=selection',
        { signal: controller.signal, headers }) : await apiFetch<Reply>(path, {
        method: 'POST', signal: controller.signal, headers,
        body: JSON.stringify({ session_id: session, watch_id: watch, mode, category: category.trim() || null, prompt: question?.prompt, ask_id: askId }),
      })
      if (controller.signal.aborted) return
      if (reply.cancelled) {
        setMessage(cancelled)
        return
      }
      if (reply.error === 'unsupported_addin') {
        setFallback(true)
        return
      }
      if (reply.timed_out) setMessage(uiText("Selection timed out."))
      else if (reply.document_changed) setMessage(uiText("The document changed. Select again."))
      else if (reply.error) setMessage(reply.error)
      else {
        const items: Mention[] = []
        for (const row of reply.items ?? []) {
          const id = elementId(row)
          if (row.kind === 'element' && id && row.document_id && !row.disabled && !hasLineBreak(row.name ?? '')
              && (!category.trim() || row.category === category.trim()))
            items.push({ kind: 'element', id, name: row.name ?? '', document_id: row.document_id })
        }
        if (reply.truncated || (question && (tooManyPicks(items) || (mode === 'single' && items.length > 1))))
          setMessage(uiText("Narrow the selection and try again."))
        else if (!items.length) setMessage(uiText("No matching elements selected."))
        else {
          onPick(items)
          if (reply.filtered_out) setMessage(uiText("{0} elements filtered out.", reply.filtered_out))
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof BridgeApiError && error.code === 'revit_unreachable')
        setMessage(uiText("Revit is unreachable."))
      else if (error instanceof BridgeApiError && error.code === 'watch_in_progress')
        setMessage(uiText("A selection watch is already running on this device."))
      else setMessage(String(error))
    } finally {
      controller.signal.removeEventListener('abort', cancel)
      active.current = null
      setWatching(false)
      onFinish?.()
    }
  }

  const startWatch = useEffectEvent(() => { void select() })
  useEffect(() => {
    if (!question || disabled || fallback || started.has(question)) return
    const start = () => {
      if (!document.hidden && !started.has(question)) { started.add(question); startWatch() }
      if (document.hidden) active.current?.abort()
    }
    const timer = setTimeout(start, 0)
    document.addEventListener('visibilitychange', start)
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', start) }
  }, [question, disabled, fallback])

  return <div className="selection-watch">
    {question && <p>{question.prompt}{question.category && <> · {question.category}</>}</p>}
    {fallback && <p role="status">{uiText("Upgrade the Revit add-in to watch selection. You can select elements and take the current selection.")}</p>}
    {watching ? <><p role="status">{mode === 'single'
      ? uiText("Click one new element in Revit.")
      : uiText("Ctrl-click or drag a box in Revit, then pause.")}</p>
      <button type="button" onClick={() => active.current?.abort()}>{uiText("Cancel")}</button></>
      : fallback && <button type="button" disabled={disabled} onClick={() => void select(true)}>
        {uiText("Take current selection")}</button>}
    {message && <p role="status">{message}</p>}
  </div>
}
