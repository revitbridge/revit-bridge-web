/* POST /api/chat - SSE token stream from the designer's own model. */

import { apiStream } from './client'
import { llmHeaders } from '../store'

export type ChatEvent =
  | { type: 'session'; id: string }
  | { type: 'token'; text: string }
  | { type: 'error'; detail: string }

export async function* chatStream(
  message: string,
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const resp = await apiStream('/api/chat', { message, session_id: sessionId || null }, llmHeaders(), signal)
  const sid = resp.headers.get('X-Session-Id')
  if (sid) yield { type: 'session', id: sid }

  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()!
    for (const line of lines) {
      if (line.startsWith('event: ')) { currentEvent = line.slice(7).trim(); continue }
      if (!line.startsWith('data: ')) continue
      const data = line.slice(6)
      const event = currentEvent
      currentEvent = ''
      if (data.trim() === '[DONE]') return
      if (event === 'error') {
        try { yield { type: 'error', detail: String(JSON.parse(data).detail || data) } }
        catch { yield { type: 'error', detail: data } }
        return
      }
      try {
        const token = JSON.parse(data)
        if (typeof token === 'string') yield { type: 'token', text: token }
      } catch { /* skip malformed frame */ }
    }
  }
}
