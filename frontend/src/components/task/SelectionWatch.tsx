import { useEffect, useRef, useState } from 'react'
import { apiFetch, BridgeApiError } from '../../api/client'
import { deviceHeaders, useSessionStore } from '../../store'
import type { SelectionQuestion } from '../../types/api'
import type { Mention, MentionCandidates } from '../../types/hostv2'
import { browserLanguage } from '../../utils/language'
import { elementId } from '../hostv2/mention/controller'
import { hasLineBreak } from '../hostv2/mention/model'
import { tooManyPicks } from './mentions'

const path = '/api/v1/bridge/watch-selection'
type Reply = Partial<MentionCandidates> & {
  error?: string; timed_out?: boolean; document_changed?: boolean; cancelled?: boolean; filtered_out?: number
}

interface Props {
  sessionId: string | null; question?: SelectionQuestion; disabled?: boolean; onPick: (items: Mention[]) => void
}

function newId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
}

export default function SelectionWatch(props: Props) {
  const device = useSessionStore(s => s.deviceId)
  return <WatchControl key={JSON.stringify([props.sessionId, device])} {...props} />
}

function WatchControl({ sessionId, question, disabled, onPick }: Props) {
  const zh = browserLanguage() === 'zh'
  const [draftSession] = useState(newId)
  const [mode, setMode] = useState<SelectionQuestion['mode']>(question?.mode ?? 'single')
  const [category, setCategory] = useState(question?.category ?? '')
  const [watching, setWatching] = useState(false)
  const [fallback, setFallback] = useState(false)
  const [message, setMessage] = useState('')
  const active = useRef<AbortController | null>(null)
  useEffect(() => {
    const stop = () => active.current?.abort()
    window.addEventListener('pagehide', stop)
    return () => { stop(); window.removeEventListener('pagehide', stop) }
  }, [disabled])

  async function select(current = false) {
    if (active.current || disabled) return
    const controller = new AbortController()
    active.current = controller
    const session = sessionId ?? draftSession
    const watch = newId()
    const headers = deviceHeaders()
    const cancel = () => {
      if (!current) void apiFetch(path + '/' + encodeURIComponent(session) + '/' + watch,
        { method: 'DELETE', headers, keepalive: true }).catch(() => {})
    }
    controller.signal.addEventListener('abort', cancel, { once: true })
    setWatching(true)
    setMessage('')
    try {
      const reply = current ? await apiFetch<Reply>('/api/v1/bridge/mentions?kind=element&source=selection',
        { signal: controller.signal }) : await apiFetch<Reply>(path, {
        method: 'POST', signal: controller.signal, headers,
        body: JSON.stringify({ session_id: session, watch_id: watch, mode, category: category.trim() || null }),
      })
      if (controller.signal.aborted || reply.cancelled) return
      if (reply.error === 'unsupported_addin') {
        setFallback(true)
        return
      }
      if (reply.timed_out) setMessage(zh ? '等待选择超时。' : 'Selection timed out.')
      else if (reply.document_changed) setMessage(zh ? '文档已切换，请重新选择。' : 'The document changed. Select again.')
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
          setMessage(zh ? '请缩小选择范围后重试。' : 'Narrow the selection and try again.')
        else if (!items.length) setMessage(zh ? '没有符合条件的构件。' : 'No matching elements selected.')
        else {
          onPick(items)
          if (reply.filtered_out) setMessage(zh ? `已过滤 ${reply.filtered_out} 个构件。` : `${reply.filtered_out} elements filtered out.`)
        }
      }
    } catch (error) {
      if (controller.signal.aborted) return
      if (error instanceof BridgeApiError && error.code === 'revit_unreachable')
        setMessage(zh ? '无法连接 Revit。' : 'Revit is unreachable.')
      else if (error instanceof BridgeApiError && error.code === 'watch_in_progress')
        setMessage(zh ? '此会话已在等待该设备的选择。' : 'A watch is already running for this session and device.')
      else setMessage(String(error))
    } finally {
      controller.signal.removeEventListener('abort', cancel)
      active.current = null
      setWatching(false)
    }
  }

  return <div className="selection-watch">
    {question ? <p>{question.prompt}{question.category && <> · {question.category}</>}</p> : <>
      <select aria-label={zh ? '选择方式' : 'Selection mode'} disabled={disabled || watching}
        value={mode} onChange={e => setMode(e.target.value as SelectionQuestion['mode'])}>
        <option value="single">{zh ? '单选' : 'Single'}</option>
        <option value="multiple">{zh ? '多选' : 'Multiple'}</option>
        <option value="box">{zh ? '框选' : 'Box'}</option>
      </select>
      <input aria-label={zh ? '可选类别' : 'Optional category'} placeholder={zh ? '类别（可选）' : 'Category (optional)'}
        title="OST_Walls" value={category} onChange={e => setCategory(e.target.value)} disabled={disabled || watching} />
    </>}
    {fallback && <p role="status">{zh ? '请升级 Revit 加载项以观察选择。也可选好后点击“取当前选择”。'
      : 'Upgrade the Revit add-in to watch selection. You can select elements and take the current selection.'}</p>}
    {watching ? <><p role="status">{mode === 'single'
      ? (zh ? '请在 Revit 中点选一个新构件。' : 'Click one new element in Revit.')
      : (zh ? '请在 Revit 中按 Ctrl 加选或拖框，选好后稍候。' : 'Ctrl-click or drag a box in Revit, then pause.')}</p>
      <button type="button" onClick={() => active.current?.abort()}>{zh ? '取消' : 'Cancel'}</button></>
      : <button type="button" disabled={disabled} onClick={() => void select(fallback)}>{fallback
        ? (zh ? '取当前选择' : 'Take current selection') : (zh ? '在 Revit 中选择' : 'Select in Revit')}</button>}
    {message && <p role="status">{message}</p>}
  </div>
}
