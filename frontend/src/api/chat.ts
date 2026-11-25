/* POST /api/chat - the host loop's SSE stream from the designer's own model.

   chatEvents() yields every event of one turn (HostEvent, spec 10.10): the session
   id, what the host found for the message's picks (mentions), reply tokens, the spec
   card the model proposed, the execution the page reported, what the host did (stage,
   tool, usage), a model error, done.
   chatStream() is the older text-only view of the same stream (session / token /
   error) and stays as it was. */

import { apiStream } from './client'
import { adminHeaders, llmHeaders } from '../store'
import type {
  DesignerForm, ExecutionResult, HostEvent, HostRequest, MentionProblem, MentionReport, PlanStep, ReconcileResult, SpecError, TaskSpec,
} from '../types/api'
import type { Role, ToolEvent } from '../types/hostv2'

const ROLES: readonly string[] = ['analyst', 'planner', 'supervisor']
const TOOL_STATES: readonly string[] = ['running', 'ok', 'error']

const isRole = (value: unknown): value is Role => typeof value === 'string' && ROLES.includes(value)

/* A count as the server sent it (tokens, ms, characters): an integer from 0 to 2**53, the
   server's own limit. Anything else (null, absent, text, a fraction, larger) is unknown -
   null, never 0. */
const MAX_COUNT = 2 ** 53
const count = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 && value <= MAX_COUNT ? value : null

/* event: stage / tool / usage (plan 2.2); null when the frame is not one of them or is malformed. */
function traceEvent(event: string, parsed: unknown): HostEvent | null {
  if (!isRecord(parsed) || !isRole(parsed.role)) return null
  const role = parsed.role
  const model = typeof parsed.model === 'string' ? parsed.model : ''
  if (event === 'stage') {
    if (parsed.status !== 'start' && parsed.status !== 'end') return null
    return { type: 'stage', role, model, status: parsed.status }
  }
  if (event === 'tool') {
    const id = parsed.id
    if ((typeof id !== 'string' && typeof id !== 'number') || typeof parsed.status !== 'string'
      || !TOOL_STATES.includes(parsed.status)) return null
    const tool: ToolEvent = {
      type: 'tool', id: String(id), role, name: String(parsed.name ?? ''),
      args: isRecord(parsed.args) ? parsed.args : {}, status: parsed.status as ToolEvent['status'],
    }
    const ms = count(parsed.ms)
    const size = count(parsed.size)
    if (ms !== null) tool.ms = ms
    if (size !== null) tool.size = size
    if (typeof parsed.preview === 'string') tool.preview = parsed.preview
    if (isRecord(parsed.read) && typeof parsed.read.name === 'string' && isRecord(parsed.read.result)) {
      tool.read = { name: parsed.read.name, result: parsed.read.result }
    }
    if (typeof parsed.error === 'string') tool.error = parsed.error
    return tool
  }
  return {
    type: 'usage', role, model, round: count(parsed.round) ?? 0,
    input: count(parsed.input), output: count(parsed.output),
    cache_read: count(parsed.cache_read), cache_write: count(parsed.cache_write),
    ms: count(parsed.ms) ?? 0,
  }
}

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
    if (parsed.run === 'auto' || parsed.run === 'confirm') spec.run = parsed.run
    if (typeof parsed.run_reason === 'string') spec.run_reason = parsed.run_reason
    return spec
  }
  if (event === 'execution') {
    if (!isRecord(parsed)) return null
    return { type: 'execution', execution: parsed as unknown as ExecutionResult }
  }
  if (event === 'plan') {
    if (!isRecord(parsed) || typeof parsed.plan_id !== 'string' || !parsed.plan_id || !Array.isArray(parsed.steps)
      || !parsed.steps.length || !parsed.steps.every(isPlanStep)
      || new Set(parsed.steps.map(step => step.id)).size !== parsed.steps.length) return null
    return { type: 'plan', plan_id: parsed.plan_id, steps: parsed.steps }
  }
  if (event === 'mentions') return mentionsEvent(parsed)
  if (event === 'questions') {
    if (!isRecord(parsed) || typeof parsed.ask_id !== 'string' || !Array.isArray(parsed.questions)
      || !parsed.questions.length || parsed.questions.length > 4
      || !parsed.questions.every(q => isRecord(q) && typeof q.id === 'string' && typeof q.text === 'string'
        && Array.isArray(q.options) && q.options.length >= 2 && q.options.length <= 6
        && q.options.every(o => isRecord(o) && typeof o.label === 'string'
          && ['string', 'number', 'boolean'].includes(typeof o.value)))) return null
    return { type: 'questions', ask_id: parsed.ask_id, questions: parsed.questions as DesignerForm['questions'] }
  }
  if (event === 'stage' || event === 'tool' || event === 'usage') return traceEvent(event, parsed)
  if (event === '' && typeof parsed === 'string') return { type: 'token', text: parsed }
  return null
}

function isPlanStep(value: unknown): value is PlanStep {
  return isRecord(value) && typeof value.id === 'string' && !!value.id
    && typeof value.title === 'string' && typeof value.status === 'string'
    && ['pending', 'proposed', 'done', 'failed', 'skipped'].includes(value.status)
    && (value.evidence_id === undefined || typeof value.evidence_id === 'string')
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/* event: mentions {resolved, not_found, invalid, unresolved, warnings?}; a list that is
   missing or not a list is empty, an entry that is not an object is dropped. */
function mentionsEvent(parsed: unknown): HostEvent | null {
  if (!isRecord(parsed)) return null
  const list = (key: string) => (Array.isArray(parsed[key]) ? parsed[key] : []).filter(isRecord)
  const problems = (key: string): MentionProblem[] => list(key).map(entry => ({
    kind: typeof entry.kind === 'string' ? entry.kind : null,
    id: entry.id,
    reason: typeof entry.reason === 'string' ? entry.reason : key,
  }))
  const event: Extract<HostEvent, { type: 'mentions' }> = {
    type: 'mentions',
    resolved: list('resolved') as unknown as MentionReport['resolved'],
    not_found: problems('not_found'), invalid: problems('invalid'), unresolved: problems('unresolved'),
  }
  if (Array.isArray(parsed.warnings)) event.warnings = parsed.warnings.filter((w): w is string => typeof w === 'string')
  return event
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
  if (request.mentions?.length) body.mentions = request.mentions
  if (request.answers) body.answers = request.answers
  if (request.mode !== undefined) body.mode = request.mode
  if (request.run !== undefined) body.run = request.run
  const resp = await apiStream('/api/chat', body, { ...llmHeaders(), ...(request.run === 'auto' ? adminHeaders() : {}) }, signal)
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
