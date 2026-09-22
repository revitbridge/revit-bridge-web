/* POST /api/chat - the host loop's SSE stream from the designer's own model.

   chatEvents() yields every event of one turn (HostEvent, spec 10.10): the session
   id, reply tokens, the spec card the model proposed, the execution the page
   reported, a model error, done. chatStream() is the older text-only view of the
   same stream (session / token / error) and stays as it was. */

import { apiStream } from './client'
import { llmHeaders } from '../store'
import type { ExecutionResult, HostEvent, HostRequest, ReconcileResult, SpecError, TaskSpec } from '../types/api'

export type ChatEvent =
  | { type: 'session'; id: string }
  | { type: 'token'; text: string }
  | { type: 'error'; detail: string }

/* One SSE block ("event: x\ndata: y" or just "data: y") to an event; null for a blank,
   comment or malformed block. Pure, so it is unit-tested without fetch. */
export function parseSseFrame(frame: string): HostEvent | null {
  let event = ''
  const dataLines: string[] = []
  for (const rawLine of frame.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.startsWith('data: ') ? line.slice(6) : line.slice(5))
  }
  if (dataLines.length === 0) return null
  const data = dataLines.join('\n')
  if (data.trim() === '[DONE]' || event === 'done') return { type: 'done' }
  let parsed: unknown
  try { parsed = JSON.parse(data) } catch { parsed = undefined }
  if (event === 'error') return { type: 'error', detail: errorDetail(parsed, data) }
  if (event === 'spec') {
    if (!isRecord(parsed) || !isRecord(parsed.spec)) return null
    const spec: Extract<HostEvent, { type: 'spec' }> = {
      type: 'spec',
      spec: parsed.spec as unknown as TaskSpec,
      card: String(parsed.card ?? ''),
      errors: (Array.isArray(parsed.errors) ? parsed.errors : []) as SpecError[],
      reconcile: isRecord(parsed.reconcile) ? (parsed.reconcile as unknown as ReconcileResult) : null,
    }
    if (isRecord(parsed.reconcile_error) && typeof parsed.reconcile_error.error === 'string') {
      spec.reconcile_error = parsed.reconcile_error as { error: string; message?: string }
    }
    return spec
  }
  if (event === 'execution') {
    if (!isRecord(parsed)) return null
    return { type: 'execution', execution: parsed as unknown as ExecutionResult }
  }
  if (event === '' && typeof parsed === 'string') return { type: 'token', text: parsed }
  return null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/* error.detail: message, then error, then the raw text (the bridge's {error, message?}
   bodies and the chat's {detail} frames both land here). */
function errorDetail(parsed: unknown, raw: string): string {
  if (isRecord(parsed)) {
    if (typeof parsed.message === 'string' && parsed.message) return parsed.message
    if (typeof parsed.error === 'string' && parsed.error) return parsed.error
    if (typeof parsed.detail === 'string' && parsed.detail) return parsed.detail
  }
  if (typeof parsed === 'string' && parsed) return parsed
  return raw
}

async function* sseFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<string> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let cut = buffer.indexOf('\n\n')
    while (cut >= 0) {
      yield buffer.slice(0, cut)
      buffer = buffer.slice(cut + 2)
      cut = buffer.indexOf('\n\n')
    }
  }
  if (buffer.trim()) yield buffer
}

export async function* chatEvents(request: HostRequest, signal?: AbortSignal): AsyncGenerator<HostEvent> {
  const body: HostRequest = { session_id: request.session_id || null }
  if (request.message !== undefined) body.message = request.message
  if (request.execution !== undefined) body.execution = request.execution
  if (request.bridge !== undefined) body.bridge = request.bridge
  const resp = await apiStream('/api/chat', body, llmHeaders(), signal)
  const sid = resp.headers.get('X-Session-Id')
  if (sid) yield { type: 'session', id: sid }
  for await (const frame of sseFrames(resp.body!)) {
    const event = parseSseFrame(frame)
    if (!event) continue
    yield event
    if (event.type === 'done' || event.type === 'error') return
  }
}

/* The text-only view: what the 0.1 Task page consumes. */
export async function* chatStream(
  message: string,
  sessionId: string,
  signal?: AbortSignal,
): AsyncGenerator<ChatEvent> {
  for await (const event of chatEvents({ message, session_id: sessionId || null }, signal)) {
    if (event.type === 'session' || event.type === 'token' || event.type === 'error') yield event
  }
}
