/* The task page's state and moves, independent of React and of the transport.

   Events come from an injected generator (chatEvents() from api/chat.ts on
   the page), the v1 calls from an injected FlowApi, so the whole
   sequence - brief, spec card, confirm, run, tamper, report - runs under
   vitest with a scripted stream and no fetch. The confirmation token lives in
   this store only: never in localStorage or sessionStorage. Only saved tools
   run here (run_tool): the model in this host proposes no code. */

import { browserLanguage } from '../../utils/language'
import { createStore } from 'zustand/vanilla'
import type {
  AssistantSelection, ChatMessage, ConfirmResponse, DesignerAnswers, ExecutionResult, HostEvent, HostRequest, Interpretation, Question, ReconcileResult,
  PlanStatus, SpecError, TaskPlan, TaskSpec,
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
  run?: () => HostRequest['run']
}

export interface Transcript {
  messages: ChatMessage[]
  sessionId: string | null
  streaming: boolean
}

/* The model's latest propose_spec, and the designer's copy of it (draft) with the interpretations ticked. */
export interface Proposal {
  run?: 'auto' | 'confirm'
  runReason?: string
  assistantSelection?: AssistantSelection
  awaitingReceipt?: boolean
  ended?: boolean
  planStep?: { planId: string; stepId: string; status: PlanStatus }
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

export interface TaskState {
  attachment: string | null
  cardMessage: number
  highlight: number
  archived: { message: number; proposal: Proposal; execution: ExecutionResult | null; confirmation: Confirmation | null }[]
  bridge: Transcript
  baseline: Transcript          // the same brief with bridge=false
  compare: boolean
  proposal: Proposal | null
  confirmation: Confirmation | null
  confirming: boolean
  confirmErrors: SpecError[]
  execution: ExecutionResult | null
  running: boolean
  reported: boolean             // the execution went back to the model
}

const emptyTranscript = (): Transcript => ({ messages: [], sessionId: null, streaming: false })

export const initialState = (): TaskState => ({
  attachment: null,
  cardMessage: -1, highlight: 0, archived: [],
  bridge: emptyTranscript(),
  baseline: emptyTranscript(),
  compare: false,
  proposal: null,
  confirmation: null,
  confirming: false,
  confirmErrors: [],
  execution: null,
  running: false,
  reported: false,
})

export const SPEC_ONLY_REPLY = '(the model proposed a spec: see the card below)'

/* A spec whose action is not a saved tool: not confirmed, nothing runs (the server refuses
   such a proposal too; this is the page's own guard). */
export const NOT_OFFERED: SpecError = {
  code: 'code_not_offered', param: null,
  message: 'This host runs saved tools only: a spec that is not run_tool is not confirmed or run here.',
}
const GATE_REFUSALS: readonly string[] = ['confirmation_required', 'confirmation_invalid']

/* A gate refusal: this request did not reach Revit. A pack that refused its
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
  const replies = t.messages.filter(m => m.role === 'assistant')
  const calls = (m: ChatMessage) => (m.trace ?? []).filter((e): e is UsageEvent => e.type === 'usage')
  const first = replies.findLastIndex(m => !m.continuesTurn)
  return { turn: replies.slice(Math.max(first, 0)).flatMap(calls), session: replies.flatMap(calls) }
}

const isTraceEvent = (e: HostEvent): e is TraceEvent => e.type === 'stage' || e.type === 'tool' || e.type === 'usage'

export function canConfirm(s: TaskState): boolean {
  return !!s.proposal && s.proposal.run !== 'auto' && !s.proposal.ended && s.proposal.reconcile?.ready === true && !s.proposal.reconcileError
    && !s.proposal.reconciling && !s.confirming && !s.running && !s.bridge.streaming && !s.execution?.success
}

export function confirmLabel(execution?: ExecutionResult | null, errors: SpecError[] = []): string {
  const retry = Boolean(execution || errors.length)
  return browserLanguage() === 'zh' ? (retry ? '重试执行' : '确认并执行') : (retry ? 'Retry execution' : 'Confirm and run · 确认并执行')
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
function updatePlan(t: Transcript, plan: TaskPlan, execution?: ExecutionResult): Transcript {
  const previous = t.messages.findIndex(m => m.plan?.plan_id === plan.plan_id)
  const index = previous < 0 ? t.messages.length - 1 : previous
  const message = t.messages[index]
  const resultStep = execution && plan.steps.find(step =>
    (step.status === 'done' || step.status === 'failed')
    && message.plan?.steps.some(old => old.id === step.id && old.status === 'proposed')
    && (!step.evidence_id || step.evidence_id === execution.evidence_id))
  const messages = t.messages.slice()
  messages[index] = { ...message, plan, ...(resultStep
    ? { stepResults: { ...message.stepResults, [resultStep.id]: execution } } : {}) }
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
    const run = deps.run?.()
    return { ...request, ...(mode ? { mode } : {}), ...(run === 'auto' ? { run } : {}) }
  }

  const push = (side: Side, message: ChatMessage) =>
    patch(side, t => ({ ...t, messages: [...t.messages, message] }))

  /* One stream into one transcript; a spec event is handed to onSpec. The stage, tool and
     usage events stay with the assistant message of their turn (its trace), in order. */
  async function consume(side: Side, request: HostRequest, onSpec?: (e: SpecEvent) => void,
    onAccepted?: () => void) {
    // An accepted execution report retires the previous question even if the supervisor ends the task.
    const previousForm = request.execution && getState()[side].messages.findLast(m => m.form && !m.answered)?.form
    if (previousForm) {
      onAccepted = () => patch(side, t => ({ ...t, messages: t.messages.map(m =>
        m.form?.ask_id === previousForm.ask_id ? { ...m, answered: 'execution' } : m) }))
    }
    patch(side, t => ({ ...t, streaming: true, messages: [...t.messages, { role: 'assistant', content: '' }] }))
    let reply = ''
    let sawSpec = false
    let sawForm = false
    let sawImage = false
    let sawPlan = false
    let startNextReply = false
    let supervisor = false
    let receipt = request.execution
    const update = (fn: (m: ChatMessage) => ChatMessage) => patch(side, t => {
      const messages = t.messages.slice()
      messages[messages.length - 1] = fn(messages[messages.length - 1])
      return { ...t, messages }
    })
    const show = (text: string) => update(m => ({ ...m, content: text }))
    try {
      for await (const evt of deps.events(request, controller?.signal)) {
        if (onAccepted) { onAccepted(); onAccepted = undefined }
        if (evt.type === 'session') { patch(side, t => ({ ...t, sessionId: evt.id })); continue }
        if (evt.type === 'token') { reply += evt.text; show(reply); continue }
        if (evt.type === 'plan') {
          sawPlan = true
          const plan = { plan_id: evt.plan_id, steps: evt.steps, ...(evt.workflow ? { workflow: evt.workflow } : {}) }
          const current = getState().proposal?.planStep
          if (side === 'bridge' && current?.planId === plan.plan_id) {
            const step = plan.steps.find(s => s.id === current.stepId)
            if (receipt && step?.status === 'done') startNextReply = true
            if (!step || step.status !== 'proposed') {
              setState(s => s.proposal ? { proposal: { ...s.proposal, ended: true,
                planStep: { ...current, status: step?.status ?? 'skipped' } } } : {})
            }
          }
          patch(side, t => updatePlan(t, plan, receipt))
          continue
        }
        if (startNextReply && ((evt.type === 'stage' && evt.role === 'planner' && evt.status === 'start')
          || (evt.type === 'spec' && getState()[side].messages.findLast(m => m.plan)?.plan?.workflow))) {
            push(side, { role: 'assistant', content: '', continuesTurn: true })
            reply = ''
            sawSpec = sawForm = sawPlan = sawImage = startNextReply = false
            supervisor = false
        }
        if (isTraceEvent(evt)) {
          if (evt.type === 'stage') supervisor = evt.role === 'supervisor'
          update(m => ({ ...m, trace: [...(m.trace ?? []), evt] }))
          continue
        }
        if (evt.type === 'spec') { sawSpec = true; onSpec?.(evt); continue }
        if (evt.type === 'view_image') {
          sawImage = true
          update(m => ({ ...m, images: [...(m.images ?? []), evt] }))
          continue
        }
        if (evt.type === 'image_notice') {
          update(m => ({ ...m, imageNotice: evt.reason }))
          continue
        }
        if (evt.type === 'questions' || evt.type === 'ask_selection') {
          sawForm = true
          patch(side, t => {
            const messages = t.messages.map(m => m.form && (!m.answered || m.form.ask_id === previousForm?.ask_id)
              ? { ...m, answered: 'replaced' as const } : m)
            const form = evt.type === 'questions' ? { ask_id: evt.ask_id, questions: evt.questions }
              : { ask_id: evt.ask_id, questions: [], selection: evt.selection }
            const last = messages.length - 1
            if (messages[last].form) messages.push({ role: 'assistant', content: '', form, supervisorForm: supervisor })
            else messages[last] = { ...messages[last], form, supervisorForm: supervisor }
            return { ...t, messages }
          })
          continue
        }
        if (evt.type === 'execution') {
          receipt = evt.execution
          if (side === 'bridge' && getState().proposal?.run === 'auto') setState({ execution: receipt, reported: true })
          continue
        }
        if (evt.type === 'mentions') { patch(side, t => reportPicks(t, evt)); continue }
        if (evt.type === 'error') {
          reply += `${reply ? '\n\n' : ''}**Model error:** ${evt.detail}`
          show(reply)
          break
        }
        break                                             // done
      }
    } catch (e: unknown) {
      const askId = request.answers?.ask_id ?? request.selection_answer
      if (askId && e instanceof BridgeApiError && e.status === 422 && e.code === 'invalid_answers') {
        patch(side, t => ({ ...t, messages: t.messages.map(m =>
          m.form?.ask_id === askId ? { ...m, answered: 'invalid' } : m) }))
      }
      if (!isAbortError(e)) {
        reply += `${reply ? '\n\n' : ''}**Model error:** ${getErrorMessage(e)}`
        show(reply)
      }
    }
    if (side === 'bridge' && getState().proposal?.run === 'auto' && !getState().execution) {
      setState(s => s.proposal ? { proposal: { ...s.proposal, awaitingReceipt: true } } : {})
    }
    if (!reply && !sawForm && !sawPlan && !sawImage) show(sawSpec ? SPEC_ONLY_REPLY : NO_RESPONSE)
    // However the stream ended (done, error, abort, broken off): a tool or stage still open
    // will never be closed by the server now - mark it interrupted.
    patch(side, t => {
      const last = t.messages.at(-1)
      const trace = last?.role === 'assistant' && last.trace ? interruptOpen(last.trace) : undefined
      if (!last || !trace || trace === last.trace) return { ...t, streaming: false }
      return { ...t, streaming: false, messages: [...t.messages.slice(0, -1), { ...last, trace }] }
    })
  }

  const onSpec = (evt: SpecEvent) => setState(s => {
    const plan = s.bridge.messages.findLast(m => m.plan)?.plan
    const step = plan?.steps.find(item => item.status === 'proposed')
    return {
      cardMessage: s.bridge.messages.length - 1,
      archived: s.proposal ? [...s.archived, { message: s.cardMessage, proposal: s.proposal,
        execution: s.execution, confirmation: s.confirmation }] : s.archived,
      proposal: {
        ...(plan && step ? { planStep: { planId: plan.plan_id, stepId: step.id, status: step.status } } : {}),
        run: evt.run, runReason: evt.run_reason, assistantSelection: evt.assistant_selection,
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
      reported: false,
    }
  })

  /* The designer's message, with the model items picked in it (bridge side only: the
     comparison has no model to check them against, and gets the text as typed). */
  async function send(text: string, mentions: Mention[] = [], answers?: DesignerAnswers, selectionAnswer?: string) {
    const s = getState()
    const message = text.trim()
    if (!message || s.bridge.streaming || s.baseline.streaming || s.confirming || s.running) return
    const open = s.bridge.messages.findLastIndex(m => !!m.form && !m.answered)
    const askId = answers?.ask_id ?? selectionAnswer
    if (askId && (open < 0 || s.bridge.messages[open].form?.ask_id !== askId)) return
    const closeForm = open < 0 ? undefined : () => {
      patch('bridge', t => ({ ...t, messages: t.messages.map((m, i) =>
        i === open ? { ...m, answered: askId ? 'form' : 'text' } : m) }))
      if (s.bridge.messages[open].supervisorForm && answers?.items[0]?.value === 'stop' && s.proposal) {
        setState({ proposal: { ...s.proposal, ended: true } })
      }
    }
    if (!s.attachment && open < 0 && canConfirm(s) && !mentions.length && /^(同意|确认|yes|ok)[.!。！]?$/iu.test(message)) {
      push('bridge', { role: 'user', content: message })
      push('bridge', { role: 'assistant',
        content: browserLanguage() === 'zh' ? `卡片在我上一条提交它的回复里。请点击卡片上的“${confirmLabel(s.execution, s.confirmErrors)}”。`
          : `The card is in the reply where I proposed it. Click “${confirmLabel(s.execution, s.confirmErrors)}” on that card.` })
      setState({ highlight: s.highlight + 1 })
      return
    }
    controller = new AbortController()
    const picked = mentions.length > 0
    push('bridge', { role: 'user', content: message, ...(picked ? { mentions: pendingMentions(mentions) } : {}) })
    const request: HostRequest = { message, session_id: s.bridge.sessionId, bridge: true }
    if (answers) request.answers = answers
    if (selectionAnswer) request.selection_answer = selectionAnswer
    if (s.attachment) {
      const image = s.bridge.messages.flatMap(m => m.images ?? []).find(img => img.capture_id === s.attachment)
      if (image) request.capture = { capture_id: image.capture_id, png_base64: image.png_base64 }
      setState({ attachment: null })
    }
    if (picked) request.mentions = mentionRefs(mentions)
    const jobs = [consume('bridge', withMode(request), onSpec, closeForm)]
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
    if (!p || p.ended || getState().confirming || getState().running || getState().execution?.success) return
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
    setState({ confirming: true, confirmation: null, confirmErrors: [] })
    try {
      const issued = await deps.api.confirm(spec, s.bridge.sessionId)
      if (getState().proposal !== s.proposal) return
      setState({
        confirmation: { ...issued, spec, used: false },
        confirming: false, execution: null, reported: false,
      })
      return issued
    } catch (e: unknown) {
      const errors = e instanceof BridgeApiError && e.body?.errors?.length
        ? e.body.errors
        : [{ code: e instanceof BridgeApiError ? e.code ?? 'confirm_failed' : 'confirm_failed', param: null, message: getErrorMessage(e) }]
      setState({ confirming: false, confirmErrors: errors })
    }
  }

  async function runConfirmed(): Promise<ExecutionResult | null> {
    const c = getState().confirmation
    if (!c || getState().running || c.spec.action.kind !== 'run_tool' || c.used) return null
    setState({ running: true })
    try {
      const params = paramsOf(c.spec)
      const result = await deps.api.runTool(c.spec.action.tool ?? '', params, c.token)
      setState(s => ({
        execution: result, reported: false, running: false,
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
          ...(typeof body?.reason === 'string' ? { reason: body.reason } : {}),
          ...(typeof body?.hint === 'string' ? { hint: body.hint } : {}),
        },
        reported: false, running: false,
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
    await consume('bridge', withMode({ execution: s.execution, session_id: s.bridge.sessionId }), onSpec)
    const current = getState()
    if (s.proposal?.planStep && current.proposal === s.proposal && !current.proposal.ended
        && current.execution === s.execution) {
      // No plan transition acknowledged this result. Retry the report without running the tool again.
      setState({ reported: false })
    }
  }

  async function run() {
    const result = await runConfirmed()
    if (result && !isGateRefusal(result)) await report()
  }

  async function demoTamper(tamper: Tamper): Promise<ExecutionResult | null> {
    const c = getState().confirmation
    if (!c?.used || !c.spec.action.tool) return null
    const params = paramsOf(c.spec)
    const value = coerceLike(tamper.value, params[tamper.param])
    if (value === params[tamper.param]) return { success: false, error: 'Choose a different value.' }
    try {
      return await deps.api.runTool(c.spec.action.tool, { ...params, [tamper.param]: value }, c.token)
    } catch (e: unknown) {
      const body = e instanceof BridgeApiError ? e.body : null
      return { success: false, error: body?.error ?? 'request_failed', message: body?.message ?? getErrorMessage(e),
        ...(typeof body?.reason === 'string' ? { reason: body.reason } : {}) }
    }
  }

  async function confirmAndRun() {
    const issued = await confirm()
    if (issued && getState().confirmation?.token === issued.token) await run()
  }

  const setCompare = (compare: boolean) => setState({ compare })

  function abort() {
    controller?.abort()
    controller = null
  }

  function reset() {
    if (getState().confirming || getState().running) return
    abort()
    setState({ ...initialState(), compare: getState().compare })
  }

  return { store, send, answer, toggleInterpretation, confirm, run, confirmAndRun, demoTamper, report, setCompare, abort, reset }
}

export type TaskFlow = ReturnType<typeof createTaskFlow>
