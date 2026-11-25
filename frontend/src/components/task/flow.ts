/* The task page's state and moves, independent of React and of the transport.

   Events come from an injected generator (chatEvents() from api/chat.ts on
   the page), the v1 calls from an injected FlowApi, so the whole
   sequence - brief, spec card, confirm, run, tamper, report - runs under
   vitest with a scripted stream and no fetch. The confirmation token lives in
   this store only: never in localStorage or sessionStorage. Only saved tools
   run here (run_tool): the model in this host proposes no code. */

import { createStore } from 'zustand/vanilla'
import type {
  ChatMessage, ConfirmResponse, ExecutionResult, HostEvent, HostRequest, Interpretation, Question, ReconcileResult,
  SpecError, TaskSpec,
} from '../../types/api'
import type { Mention, TraceEvent, UsageEvent } from '../../types/hostv2'
import { interruptOpen } from '../hostv2/trace/events'
import { mentionRefs, pendingMentions, withReport } from './mentions'
import { NO_RESPONSE } from '../../utils/chatRun'
import { getErrorMessage, isAbortError } from '../../utils/errors'
import { BridgeApiError } from '../shared/http'

export type EventSource = (request: HostRequest, signal?: AbortSignal) => AsyncIterable<HostEvent>

/* reconcile and confirm carry the chat session the spec comes from: a parameter bound to
   one of the designer's picks is accepted only for a pick checked in that session. */
export interface FlowApi {
  reconcile(spec: TaskSpec, sessionId: string | null): Promise<ReconcileResult>
  confirm(spec: TaskSpec, sessionId: string | null): Promise<ConfirmResponse>
  runTool(name: string, params: Record<string, unknown>, token: string): Promise<ExecutionResult>
}

export interface FlowDeps {
  events: EventSource
  api: FlowApi
  /* The chat mode each bridge request carries, read when it goes out (the page's mode
     selector); undefined or absent: no mode is sent, the host runs its default. */
  mode?: () => HostRequest['mode'] | undefined
}

export interface Transcript {
  messages: ChatMessage[]
  sessionId: string | null
  streaming: boolean
}

/* The model's latest propose_spec, and the designer's copy of it (draft) with the interpretations ticked. */
export interface Proposal {
  spec: TaskSpec
  card: string
  errors: SpecError[]
  reconcile: ReconcileResult | null
  reconcileError: { error: string; message?: string } | null
  draft: TaskSpec
  reconciling: boolean
}

export interface Confirmation extends ConfirmResponse {
  spec: TaskSpec       // exactly what the token is bound to
  used: boolean        // an execution reached Revit under it
}

/* The tamper demo: one parameter value changed after confirmation. */
export type Tamper = { param: string; value: string }

/* A run whose report came back with a new card (the supervisor sent the task back): what the
   page itself received from that run, kept above the new card - the model's report is not the
   only record of what happened. */
export interface PreviousAttempt {
  execution: ExecutionResult
  executionKind: 'genuine' | 'tampered' | null
}

export interface TaskState {
  bridge: Transcript
  baseline: Transcript          // the same brief with bridge=false
  compare: boolean
  proposal: Proposal | null
  confirmation: Confirmation | null
  confirming: boolean
  confirmErrors: SpecError[]
  execution: ExecutionResult | null
  executionKind: 'genuine' | 'tampered' | null
  running: boolean
  reported: boolean             // the execution went back to the model
  previous: PreviousAttempt | null
}

const emptyTranscript = (): Transcript => ({ messages: [], sessionId: null, streaming: false })

export const initialState = (): TaskState => ({
  bridge: emptyTranscript(),
  baseline: emptyTranscript(),
  compare: false,
  proposal: null,
  confirmation: null,
  confirming: false,
  confirmErrors: [],
  execution: null,
  executionKind: null,
  running: false,
  reported: false,
  previous: null,
})

export const SPEC_ONLY_REPLY = '(the model proposed a spec: see the card below)'

/* A spec whose action is not a saved tool: not confirmed, nothing runs (the server refuses
   such a proposal too; this is the page's own guard). */
export const NOT_OFFERED: SpecError = {
  code: 'code_not_offered', param: null,
  message: 'This host runs saved tools only: a spec that is not run_tool is not confirmed or run here.',
}
const GATE_REFUSALS: readonly string[] = ['confirmation_required', 'confirmation_invalid']

/* A gate refusal: nothing reached Revit and the token is still whole. A pack that refused its
   input in Revit (refused: true) is never one, whatever its text says: it reached Revit and
   spent the token. */
export function isGateRefusal(result: ExecutionResult | null): boolean {
  return !!result && result.refused !== true && GATE_REFUSALS.includes(result.error ?? '')
}

/* revit-bridge 0.9.1: the pack refused its input in Revit; the add-in kept nothing. Read from the
   package's field, never from the text (which is the pack's own words, after its marker). */
export function isToolRefusal(result: ExecutionResult | null): boolean {
  return !!result && result.success !== true && result.refused === true
}

/* The values the token is bound to, exactly as the spec carries them (3000, not "3000"). */
export function paramsOf(spec: TaskSpec): Record<string, unknown> {
  return Object.fromEntries(spec.parameters.map(p => [p.name, p.value]))
}

/* An edited value keeps the type of the confirmed one where the text allows it. */
export function coerceLike(text: string, original: unknown): unknown {
  if (typeof original === 'number') {
    const n = Number(text)
    return text.trim() !== '' && Number.isFinite(n) ? n : text
  }
  if (typeof original === 'boolean') return text.trim().toLowerCase() === 'true'
  return text
}

/* The interpretations the card shows: the draft's, then the reconcile's still-open ones. */
export function interpretationsOf(p: Proposal): Interpretation[] {
  const shown = [...(p.draft.interpretations ?? [])]
  for (const it of p.reconcile?.interpretations_required ?? []) {
    if (!shown.some(s => s.text === it.text)) shown.push({ ...it, confirmed: false })
  }
  return shown
}

/* The token usage of a transcript: the model calls of its latest turn (the last assistant
   message) and of the whole session, as the usage events reported them. */
export function usageOf(t: Transcript): { turn: UsageEvent[]; session: UsageEvent[] } {
  const perReply = t.messages
    .filter(m => m.role === 'assistant')
    .map(m => (m.trace ?? []).filter((e): e is UsageEvent => e.type === 'usage'))
  return { turn: perReply.at(-1) ?? [], session: perReply.flat() }
}

const isTraceEvent = (e: HostEvent): e is TraceEvent => e.type === 'stage' || e.type === 'tool' || e.type === 'usage'

export function canConfirm(s: TaskState): boolean {
  return !!s.proposal && s.proposal.reconcile?.ready === true && !s.proposal.reconciling && !s.confirming && !s.bridge.streaming
}

type SpecEvent = Extract<HostEvent, { type: 'spec' }>
type MentionsEvent = Extract<HostEvent, { type: 'mentions' }>

/* The turn's mentions event onto the picks of the message it answers (the last user message). */
function reportPicks(t: Transcript, report: MentionsEvent): Transcript {
  const at = t.messages.findLastIndex(m => m.role === 'user')
  const message = t.messages[at]
  if (!message?.mentions) return t
  const messages = t.messages.slice()
  messages[at] = { ...message, mentions: withReport(message.mentions, report) }
  return { ...t, messages }
}
type Side = 'bridge' | 'baseline'

export function createTaskFlow(deps: FlowDeps) {
  const store = createStore<TaskState>(() => initialState())
  const { getState, setState } = store
  let controller: AbortController | null = null

  const patch = (side: Side, fn: (t: Transcript) => Transcript) =>
    setState(s => ({ [side]: fn(s[side]) }) as Partial<TaskState>)

  /* A bridge request with the chat mode of the moment, when there is one to send. */
  const withMode = (request: HostRequest): HostRequest => {
    const mode = deps.mode?.()
    return mode === undefined ? request : { ...request, mode }
  }

  const push = (side: Side, message: ChatMessage) =>
    patch(side, t => ({ ...t, messages: [...t.messages, message] }))

  /* One stream into one transcript; a spec event is handed to onSpec. The stage, tool and
     usage events stay with the assistant message of their turn (its trace), in order. */
  async function consume(side: Side, request: HostRequest, onSpec?: (e: SpecEvent) => void) {
    patch(side, t => ({ ...t, streaming: true, messages: [...t.messages, { role: 'assistant', content: '' }] }))
    let reply = ''
    let sawSpec = false
    const update = (fn: (m: ChatMessage) => ChatMessage) => patch(side, t => {
      const messages = t.messages.slice()
      messages[messages.length - 1] = fn(messages[messages.length - 1])
      return { ...t, messages }
    })
    const show = (text: string) => update(m => ({ ...m, content: text }))
    try {
      for await (const evt of deps.events(request, controller?.signal)) {
        if (evt.type === 'session') { patch(side, t => ({ ...t, sessionId: evt.id })); continue }
        if (evt.type === 'token') { reply += evt.text; show(reply); continue }
        if (isTraceEvent(evt)) { update(m => ({ ...m, trace: [...(m.trace ?? []), evt] })); continue }
        if (evt.type === 'spec') { sawSpec = true; onSpec?.(evt); continue }
        if (evt.type === 'execution') continue          // the echo of what the page sent
        if (evt.type === 'mentions') { patch(side, t => reportPicks(t, evt)); continue }
        if (evt.type === 'error') {
          reply += `${reply ? '\n\n' : ''}**Model error:** ${evt.detail}`
          show(reply)
          break
        }
        break                                             // done
      }
    } catch (e: unknown) {
      if (!isAbortError(e)) {
        reply += `${reply ? '\n\n' : ''}**Model error:** ${getErrorMessage(e)}`
        show(reply)
      }
    }
    if (!reply) show(sawSpec ? SPEC_ONLY_REPLY : NO_RESPONSE)
    // However the stream ended (done, error, abort, broken off): a tool or stage still open
    // will never be closed by the server now - mark it interrupted.
    patch(side, t => {
      const last = t.messages.at(-1)
      const trace = last?.role === 'assistant' && last.trace ? interruptOpen(last.trace) : undefined
      if (!last || !trace || trace === last.trace) return { ...t, streaming: false }
      return { ...t, streaming: false, messages: [...t.messages.slice(0, -1), { ...last, trace }] }
    })
  }

  const onSpec = (evt: SpecEvent) => setState({
    proposal: {
      spec: evt.spec,
      card: evt.card,
      errors: evt.errors ?? [],
      reconcile: evt.reconcile ?? null,
      reconcileError: evt.reconcile_error ?? null,
      draft: structuredClone(evt.spec),
      reconciling: false,
    },
    confirmation: null,     // a new proposal supersedes the confirmed one
    confirmErrors: [],
    execution: null,
    executionKind: null,
    reported: false,
    previous: null,         // a card that answers a message: no earlier attempt to show
  })

  /* The designer's message, with the model items picked in it (bridge side only: the
     comparison has no model to check them against, and gets the text as typed). */
  async function send(text: string, mentions: Mention[] = []) {
    const s = getState()
    const message = text.trim()
    if (!message || s.bridge.streaming || s.baseline.streaming) return
    controller = new AbortController()
    const picked = mentions.length > 0
    push('bridge', { role: 'user', content: message, ...(picked ? { mentions: pendingMentions(mentions) } : {}) })
    const request: HostRequest = { message, session_id: s.bridge.sessionId, bridge: true }
    if (picked) request.mentions = mentionRefs(mentions)
    const jobs = [consume('bridge', withMode(request), onSpec)]
    if (s.compare) {
      push('baseline', { role: 'user', content: message })
      jobs.push(consume('baseline', { message, session_id: s.baseline.sessionId, bridge: false }))
    }
    await Promise.all(jobs)
  }

  /* A clicked option answers the model's question in the chat. */
  const answer = (question: Question, option: { label: string; value: unknown }) =>
    send(`${question.param} = ${String(option.value)}`)

  /* Ticking an interpretation changes the draft, and the draft is reconciled again. */
  async function toggleInterpretation(text: string, confirmed: boolean) {
    const p = getState().proposal
    if (!p) return
    const list = [...(p.draft.interpretations ?? [])]
    const at = list.findIndex(it => it.text === text)
    if (at >= 0) list[at] = { ...list[at], confirmed }
    else {
      const required = p.reconcile?.interpretations_required.find(it => it.text === text)
      list.push({ param: required?.param ?? null, text, confirmed })
    }
    const draft: TaskSpec = { ...p.draft, interpretations: list }
    setState({ proposal: { ...p, draft, reconciling: true } })
    try {
      const reconcile = await deps.api.reconcile(draft, getState().bridge.sessionId)
      setState(s => s.proposal?.draft === draft
        ? { proposal: { ...s.proposal, reconcile, reconcileError: null, reconciling: false } }
        : {})
    } catch (e: unknown) {
      const code = e instanceof BridgeApiError ? e.code ?? 'reconcile_failed' : 'reconcile_failed'
      setState(s => s.proposal?.draft === draft
        ? { proposal: { ...s.proposal, reconcileError: { error: code, message: getErrorMessage(e) }, reconciling: false } }
        : {})
    }
  }

  async function confirm() {
    const s = getState()
    if (!canConfirm(s) || !s.proposal) return
    const spec = s.proposal.draft
    if (spec.action.kind !== 'run_tool') {
      setState({ confirmErrors: [NOT_OFFERED] })
      return
    }
    setState({ confirming: true, confirmErrors: [] })
    try {
      const issued = await deps.api.confirm(spec, s.bridge.sessionId)
      setState({
        confirmation: { ...issued, spec, used: false },
        confirming: false, execution: null, executionKind: null, reported: false,
      })
    } catch (e: unknown) {
      const errors = e instanceof BridgeApiError && e.body?.errors?.length
        ? e.body.errors
        : [{ code: e instanceof BridgeApiError ? e.code ?? 'confirm_failed' : 'confirm_failed', param: null, message: getErrorMessage(e) }]
      setState({ confirming: false, confirmErrors: errors })
    }
  }

  async function runProjection(kind: 'genuine' | 'tampered', tamper?: Tamper): Promise<ExecutionResult | null> {
    const c = getState().confirmation
    if (!c || getState().running || c.spec.action.kind !== 'run_tool') return null
    setState({ running: true })
    try {
      const params = paramsOf(c.spec)
      if (tamper) params[tamper.param] = coerceLike(tamper.value, params[tamper.param])
      const result = await deps.api.runTool(c.spec.action.tool ?? '', params, c.token)
      setState(s => ({
        execution: result, executionKind: kind, reported: false, running: false, previous: null,
        // a gate refusal consumed nothing; anything else reached Revit and the token is spent
        confirmation: s.confirmation && !isGateRefusal(result) ? { ...s.confirmation, used: true } : s.confirmation,
      }))
      return result
    } catch (e: unknown) {
      // 400 confirmation_required, 404 unknown pack, 503 Revit unreachable: shown in the result's shape
      const body = e instanceof BridgeApiError ? e.body : null
      setState({
        execution: {
          success: false,
          error: body?.error ?? 'request_failed',
          message: body?.message ?? getErrorMessage(e),
          ...(typeof body?.hint === 'string' ? { hint: body.hint } : {}),
        },
        executionKind: kind, reported: false, running: false, previous: null,
      })
      return null
    }
  }

  /* The execution result goes back to the model for its report. With the roles the supervisor
     reports; when it sends the task back, the planner proposes again in the same reply: the new
     card replaces this execution (onSpec), to be confirmed and run like the first, and what the
     page received from this run stays above it as the previous attempt. */
  async function report() {
    const s = getState()
    if (!s.execution || !s.bridge.sessionId || s.bridge.streaming || s.reported) return
    setState({ reported: true })
    const label = s.execution.evidence_id ? `evidence ${s.execution.evidence_id}` : s.execution.error ?? 'result'
    push('bridge', { role: 'host', content: `Execution result sent to the model (${label}).` })
    controller = new AbortController()
    const attempt: PreviousAttempt = { execution: s.execution, executionKind: s.executionKind }
    await consume('bridge', withMode({ execution: s.execution, session_id: s.bridge.sessionId }), evt => {
      onSpec(evt)
      setState({ previous: attempt })
    })
  }

  async function run() {
    const result = await runProjection('genuine')
    if (result && !isGateRefusal(result)) await report()
  }

  const runTampered = (tamper: Tamper) => runProjection('tampered', tamper)

  const setCompare = (compare: boolean) => setState({ compare })

  function abort() {
    controller?.abort()
    controller = null
  }

  function reset() {
    abort()
    setState({ ...initialState(), compare: getState().compare })
  }

  return { store, send, answer, toggleInterpretation, confirm, run, runTampered, report, setCompare, abort, reset }
}

export type TaskFlow = ReturnType<typeof createTaskFlow>
