import { describe, expect, it, vi } from 'vitest'
import { BridgeApiError } from '../shared/http'
import { sumUsage } from '../hostv2/trace'
import { canConfirm, coerceLike, createTaskFlow, initialState, interpretationsOf, isGateRefusal, isToolRefusal, NOT_OFFERED, paramsOf, SPEC_ONLY_REPLY, usageOf, type FlowApi } from './flow'
import type { ExecutionResult, HostEvent, HostRequest, Question, ReconcileResult, TaskSpec } from '../../types/api'
import type { Mention } from '../../types/hostv2'

const spec: TaskSpec = {
  task: 'Place a structural column at (3000, 3000) on L1',
  action: { kind: 'run_tool', tool: 'create_structural_column' },
  parameters: [
    { name: 'type_name', value: 'UC305x305x97', source: 'tool', evidence: 'tool:family_types' },
    { name: 'level_name', value: 'L1', source: 'answer', evidence: 'q_level_name' },
    { name: 'x', value: 3000, unit: 'mm', source: 'designer', evidence: '(3000, 3000)' },
    { name: 'y', value: 3000, unit: 'mm', source: 'designer', evidence: '(3000, 3000)' },
  ],
  interpretations: [{ param: 'x', text: '3000 read as mm', confirmed: false }],
  snapshot_fingerprint: 'fp1',
  language: 'zh',
}

const notReady: ReconcileResult = { conflicts: [], questions: [], interpretations_required: [{ param: 'x', text: '3000 read as mm', confirmed: false }], ready: false }
const ready: ReconcileResult = { conflicts: [], questions: [], interpretations_required: [], ready: true }

const success: ExecutionResult = {
  success: true, error: null, tool: 'create_structural_column', result: { ElementId: 1234, Status: 'Created' },
  validation: { validator: 'created_ids', passed: true, checks: [{ detail: '1 new OST_StructuralColumns element', passed: true }] },
  evidence_id: 'ev_20260922T090000_abc123', preconditions_failed: [], warnings: [],
}

/* A scripted host: each call to events() answers with the next list; requests are recorded. */
function scriptedHost(...turns: HostEvent[][]) {
  const requests: HostRequest[] = []
  const events = async function* (request: HostRequest): AsyncGenerator<HostEvent> {
    requests.push(request)
    const turn = turns.shift() ?? []
    for (const e of turn) yield e
  }
  return { events, requests }
}

function fakeApi(overrides: Partial<FlowApi> = {}): FlowApi & { calls: Array<[string, unknown[]]> } {
  const calls: Array<[string, unknown[]]> = []
  const record = <T>(name: string, value: T) => (...args: unknown[]) => { calls.push([name, args]); return Promise.resolve(value) }
  return {
    calls,
    reconcile: record('reconcile', ready),
    confirm: record('confirm', { token: 'tok_abcdef0123456789', spec_hash: 'h1', expires_at: '2026-09-22T09:10:00Z', card: 'Task: ...' }),
    runTool: record('runTool', success),
    ...overrides,
  }
}

const specEvent: HostEvent = { type: 'spec', spec, card: 'Task: Place a structural column\nConfirm? (yes / change something)', errors: [], reconcile: notReady }

/* revit-bridge 0.4: reconcile turns a value it cannot find into a question too - an
   ambiguous one for the designer, a missing one as a model fact - next to the open
   parameters, and every question carries kind and blocks. */
const levelQuestion: Question = {
  id: 'q_level_name', param: 'level_name', text: 'Which level?', why: "level_name = 'L9' does not exist in the snapshot",
  options: [{ label: 'L1 (0mm)', value: 'L1', source: 'tool:levels' }, { label: 'L2 (3600mm)', value: 'L2', source: 'tool:levels' }],
  allow_other: true, kind: 'model_fact', blocks: ['spec', 'execute'],
}
const typeQuestion: Question = {
  id: 'q_type_name', param: 'type_name', text: 'Which type?', why: "type_name = 'uc305' matches more than one type",
  options: [{ label: 'UC305x305x97', value: 'UC305x305x97', source: 'tool:family_types' }, { label: 'UC305x305x118', value: 'UC305x305x118', source: 'tool:family_types' }],
  allow_other: true, kind: 'user_intent', blocks: ['spec', 'execute'],
}
const heightQuestion: Question = {
  id: 'q_height', param: 'height', text: 'Height (mm)?', why: 'required by the pack', options: [], allow_other: true,
  kind: 'user_intent', blocks: ['spec', 'execute'],
}
const conflicted: ReconcileResult = {
  conflicts: [
    { param: 'level_name', claimed: 'L9', kind: 'not_found', available: ['L1', 'L2'], message: "level_name = 'L9' does not exist in the snapshot" },
    { param: 'type_name', claimed: 'uc305', kind: 'ambiguous', available: ['UC305x305x97', 'UC305x305x118'], message: "type_name = 'uc305' matches more than one type" },
  ],
  questions: [levelQuestion, typeQuestion, heightQuestion],
  interpretations_required: [],
  ready: false,
}

describe('the task flow', () => {
  it('accepts a click once, retries an expired token with a new confirmation and keeps the execution with its reply', async () => {
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, { ...specEvent, reconcile: ready }],
      [{ type: 'token', text: 'Done.' }],
      [{ ...specEvent, card: 'another', reconcile: ready }],
    )
    const confirm = vi.fn()
      .mockResolvedValueOnce({ token: 'expired', card: 'one' })
      .mockResolvedValueOnce({ token: 'fresh', card: 'two' })
    const runTool = vi.fn().mockResolvedValueOnce({ success: false, error: 'confirmation_invalid', reason: 'expired' })
      .mockResolvedValueOnce(success)
    const flow = createTaskFlow({ events: host.events, api: fakeApi({ confirm, runTool }) })
    await flow.send('wall')
    await flow.send('yes')
    expect(flow.store.getState().highlight).toBe(1)
    expect(confirm).not.toHaveBeenCalled()
    await Promise.all([flow.confirmAndRun(), flow.confirmAndRun()])
    expect(runTool.mock.calls.map(call => call[2])).toEqual(['expired'])
    await flow.confirmAndRun()
    await flow.confirmAndRun()
    expect(runTool.mock.calls.map(call => call[2])).toEqual(['expired', 'fresh'])
    await flow.send('another wall')
    expect(flow.store.getState().archived[0]).toMatchObject({ message: 1, execution: success })
    expect(flow.store.getState().proposal?.card).toBe('another')
  })
  it('folds a turn into the transcript: session, tokens, then the spec card', async () => {
    const host = scriptedHost([
      { type: 'session', id: 's1' },
      { type: 'token', text: 'Which ' }, { type: 'token', text: 'level?' },
      specEvent,
      { type: 'done' },
    ])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('在坐标 (3000, 3000) 放一根结构柱。')
    const s = flow.store.getState()
    expect(host.requests).toEqual([{ message: '在坐标 (3000, 3000) 放一根结构柱。', session_id: null, bridge: true }])
    expect(s.bridge.sessionId).toBe('s1')
    expect(s.bridge.messages).toEqual([
      { role: 'user', content: '在坐标 (3000, 3000) 放一根结构柱。' },
      { role: 'assistant', content: 'Which level?' },
    ])
    expect(s.bridge.streaming).toBe(false)
    expect(s.proposal?.card).toContain('Confirm?')
    expect(s.proposal?.draft).toEqual(spec)
    expect(s.proposal?.draft).not.toBe(spec)          // the designer edits a copy
    expect(canConfirm(s)).toBe(false)                 // reconcile.ready is false
  })

  it('shows a placeholder when the turn was only a spec event, and the reconcile error when Revit was away', async () => {
    const host = scriptedHost([
      { type: 'spec', spec, card: 'Task: x', errors: [], reconcile: null, reconcile_error: { error: 'revit_unreachable', message: 'connect refused' } },
      { type: 'done' },
    ])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('hi')
    const s = flow.store.getState()
    expect(s.bridge.messages[1].content).toBe(SPEC_ONLY_REPLY)
    expect(s.proposal?.reconcile).toBeNull()
    expect(s.proposal?.reconcileError).toEqual({ error: 'revit_unreachable', message: 'connect refused' })
    expect(canConfirm(s)).toBe(false)
  })

  it('runs the same brief without the bridge side by side when compare is on', async () => {
    const host = scriptedHost(
      [{ type: 'session', id: 'with' }, { type: 'token', text: 'asks first' }, { type: 'done' }],
      [{ type: 'session', id: 'without' }, { type: 'token', text: 'guesses' }, { type: 'done' }],
    )
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    flow.setCompare(true)
    await flow.send('brief')
    expect(host.requests).toEqual([
      { message: 'brief', session_id: null, bridge: true },
      { message: 'brief', session_id: null, bridge: false },
    ])
    const s = flow.store.getState()
    expect(s.bridge.messages[1].content).toBe('asks first')
    expect(s.baseline.messages[1].content).toBe('guesses')
    expect(s.baseline.sessionId).toBe('without')
  })

  it('answers a question with the clicked option as a chat message', async () => {
    const host = scriptedHost([{ type: 'done' }], [{ type: 'done' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.answer({ id: 'q_level_name', param: 'level_name', text: 'Which level?', options: [{ label: 'L1 (0mm)', value: 'L1' }] }, { label: 'L1 (0mm)', value: 'L1' })
    expect(host.requests[0].message).toBe('level_name = L1')
  })

  it('keeps every question reconcile asks, conflicts included, with kind and blocks; a click answers in the chat', async () => {
    const host = scriptedHost([{ type: 'spec', spec, card: 'c', errors: [], reconcile: conflicted }, { type: 'done' }], [{ type: 'done' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('brief')
    const s = flow.store.getState()
    expect(s.proposal?.reconcile?.questions).toEqual([levelQuestion, typeQuestion, heightQuestion])
    expect(s.proposal?.reconcile?.questions.map(q => [q.param, q.kind, q.blocks])).toEqual([
      ['level_name', 'model_fact', ['spec', 'execute']],
      ['type_name', 'user_intent', ['spec', 'execute']],
      ['height', 'user_intent', ['spec', 'execute']],
    ])
    expect(canConfirm(s)).toBe(false)
    await flow.answer(levelQuestion, levelQuestion.options[1])
    expect(host.requests[1].message).toBe('level_name = L2')
  })

  it('kind and blocks are shown, never a gate: Confirm follows reconcile.ready alone', async () => {
    const odd: ReconcileResult = { ...ready, questions: [{ ...heightQuestion, kind: 'technical', blocks: ['build'] }] }
    const host = scriptedHost([{ type: 'spec', spec, card: 'c', errors: [], reconcile: odd }, { type: 'done' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('brief')
    expect(canConfirm(flow.store.getState())).toBe(true)
    // a 0.3 server sends neither key
    const old: Question = { id: 'q_x', param: 'x', text: 'x?', options: [] }
    expect(old.kind).toBeUndefined()
  })

  it('ticking an interpretation reconciles the draft again and lights Confirm', async () => {
    const host = scriptedHost([specEvent, { type: 'done' }])
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    expect(interpretationsOf(flow.store.getState().proposal!)).toEqual([{ param: 'x', text: '3000 read as mm', confirmed: false }])
    await flow.toggleInterpretation('3000 read as mm', true)
    const s = flow.store.getState()
    // No session event in this turn: no session id goes with the draft.
    expect(api.calls).toEqual([['reconcile', [{ ...spec, interpretations: [{ param: 'x', text: '3000 read as mm', confirmed: true }] }, null]]])
    expect(s.proposal?.reconcile?.ready).toBe(true)
    expect(s.proposal?.spec.interpretations?.[0].confirmed).toBe(false)   // the model's spec is untouched
    expect(canConfirm(s)).toBe(true)
  })

  it('confirm -> run: the token stays in memory, the run carries the spec values as they are, the result goes back to the model', async () => {
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'execution', execution: success }, { type: 'token', text: 'Created column 1234; validator created_ids passed.' }, { type: 'done' }],
    )
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    let s = flow.store.getState()
    expect(api.calls).toEqual([['confirm', [spec, 's1']]])                // the chat session goes with the spec
    expect(s.confirmation).toMatchObject({ token: 'tok_abcdef0123456789', expires_at: '2026-09-22T09:10:00Z', used: false })
    expect(s.confirmation?.spec).toEqual(spec)

    await flow.run()
    s = flow.store.getState()
    expect(api.calls[1]).toEqual(['runTool', ['create_structural_column', { type_name: 'UC305x305x97', level_name: 'L1', x: 3000, y: 3000 }, 'tok_abcdef0123456789']])
    expect(s.execution).toEqual(success)
    expect(s.confirmation?.used).toBe(true)
    // the report: POST /api/chat {session_id, execution} without a message
    expect(host.requests[1]).toEqual({ execution: success, session_id: 's1' })
    expect(s.reported).toBe(true)
    expect(s.bridge.messages.slice(-2)).toEqual([
      { role: 'host', content: 'Execution result sent to the model (evidence ev_20260922T090000_abc123).' },
      { role: 'assistant', content: 'Created column 1234; validator created_ids passed.' },
    ])
  })

  it("a report the supervisor sends back: its text and the planner's new card replace the execution", async () => {
    const failed: ExecutionResult = { success: false, error: "Level 'L9' not found", tool: 'create_structural_column', evidence_id: 'ev_1' }
    const retried: TaskSpec = { ...spec, parameters: spec.parameters.map(p => p.name === 'level_name' ? { ...p, value: 'L2' } : p) }
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'execution', execution: failed },
        { type: 'stage', role: 'supervisor', model: 'small', status: 'start' },
        { type: 'usage', role: 'supervisor', model: 'small', round: 1, input: 900, output: 40, cache_read: null, cache_write: null, ms: 5 },
        { type: 'stage', role: 'supervisor', model: 'small', status: 'end' },
        { type: 'token', text: 'No level L9.\n\n' },
        { type: 'stage', role: 'planner', model: 'large', status: 'start' },
        { type: 'spec', spec: retried, card: 'c2', errors: [], reconcile: ready },
        { type: 'token', text: 'New card below.' },
        { type: 'stage', role: 'planner', model: 'large', status: 'end' },
        { type: 'done' }],
    )
    const api = fakeApi({ runTool: vi.fn().mockResolvedValue(failed) })
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    await flow.run()
    const s = flow.store.getState()
    expect(host.requests[1]).toEqual({ execution: failed, session_id: 's1' })
    expect(s.proposal?.spec).toEqual(retried)                // the new card, ready to confirm
    expect(s.proposal?.card).toBe('c2')
    expect(s.confirmation).toBeNull()
    expect(s.execution).toBeNull()
    expect(s.reported).toBe(false)
    expect(canConfirm(s)).toBe(true)
    const reply = s.bridge.messages.at(-1)
    expect(reply?.content).toBe('No level L9.\n\nNew card below.')
    expect(reply?.trace?.filter(e => e.type === 'stage').map(e => e.role)).toEqual(['supervisor', 'supervisor', 'planner', 'planner'])
    // What the page received from the run stays, as the previous attempt above the new card.
    expect(s.archived[0].execution).toEqual(failed)
  })

  it('each earlier execution stays with the card it ran', async () => {
    const failed: ExecutionResult = { success: false, error: 'boom', tool: 'create_structural_column', evidence_id: 'ev_1' }
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'execution', execution: failed }, { type: 'spec', spec, card: 'c2', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'execution', execution: success }, { type: 'token', text: 'Done.' }, { type: 'done' }],
      [{ type: 'spec', spec, card: 'c3', errors: [], reconcile: ready }, { type: 'done' }],
    )
    const runTool = vi.fn().mockResolvedValueOnce(failed).mockResolvedValueOnce(success)
    const flow = createTaskFlow({ events: host.events, api: fakeApi({ runTool }) })
    await flow.send('brief')
    await flow.confirm()
    await flow.run()
    expect(flow.store.getState().archived[0].execution).toEqual(failed)
    await flow.confirm()
    await flow.run()
    let s = flow.store.getState()
    expect(s.archived.map(c => c.execution)).toEqual([failed])
    expect(s.execution).toEqual(success)
    await flow.send('another column')
    s = flow.store.getState()
    expect(s.proposal?.card).toBe('c3')
    expect(s.archived.map(c => c.execution)).toEqual([failed, success])
  })

  it('a run the tool refused in Revit (revit-bridge 0.9.1) spent the token and goes to the model like any run', async () => {
    const refused: ExecutionResult = {
      success: false, tool: 'create_structural_column', result: null, error: 'There is already a column at that point.',
      validation: null, evidence_id: 'ev_r', warnings: [], preconditions_failed: [], refused: true,
    }
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'execution', execution: refused }, { type: 'token', text: 'The tool refused: a column is already there.' }, { type: 'done' }],
    )
    const flow = createTaskFlow({ events: host.events, api: fakeApi({ runTool: vi.fn().mockResolvedValue(refused) }) })
    await flow.send('brief')
    await flow.confirm()
    await flow.run()
    const s = flow.store.getState()
    expect(s.execution).toEqual(refused)
    expect(s.confirmation?.used).toBe(true)                  // it reached Revit: the token is spent
    expect(host.requests[1]).toEqual({ execution: refused, session_id: 's1' })
    expect(isGateRefusal(refused) || isGateRefusal({ ...refused, error: 'confirmation_invalid' })).toBe(false)
    expect(isToolRefusal(refused)).toBe(true)
    expect(isToolRefusal({ ...refused, refused: undefined })).toBe(false)
    expect(isToolRefusal({ ...refused, success: true })).toBe(false)
  })

  it('tamper demo: an edited value under the same token is refused and nothing is reported', async () => {
    const refusal: ExecutionResult = { success: false, error: 'confirmation_invalid', reason: 'mismatch', message: 'projection hash differs' }
    const host = scriptedHost([{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const api = fakeApi({ runTool: vi.fn().mockResolvedValueOnce(success).mockResolvedValueOnce(refusal) })
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    await flow.run()
    expect(await flow.demoTamper({ param: 'x', value: '3001' })).toEqual(refusal)
    const s = flow.store.getState()
    expect(api.runTool).toHaveBeenCalledWith('create_structural_column', { type_name: 'UC305x305x97', level_name: 'L1', x: 3001, y: 3000 }, 'tok_abcdef0123456789')
    expect(s.execution).toEqual(success)
    expect(s.confirmation?.used).toBe(true)
    expect(host.requests).toHaveLength(2)        // only the real execution is reported
  })

  it('a spec that is not a saved tool is neither confirmed nor run', async () => {
    const codeSpec: TaskSpec = { ...spec, action: { kind: 'execute_code', code: 'return 1;', code_parameters: [] }, parameters: [], interpretations: [] }
    const host = scriptedHost([{ type: 'spec', spec: codeSpec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    await flow.run()
    await flow.demoTamper({ param: 'x', value: '1' })
    const s = flow.store.getState()
    expect(api.calls).toEqual([])                                   // no /spec/confirm, no /tools//run
    expect(s.confirmation).toBeNull()
    expect(s.execution).toBeNull()
    expect(s.confirmErrors).toEqual([NOT_OFFERED])
    expect(NOT_OFFERED.code).toBe('code_not_offered')
  })

  it('a 422 from /spec/confirm lists the spec errors; a 400 from /run is shown in the result shape', async () => {
    const host = scriptedHost([{ type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const rejected = new BridgeApiError(422, JSON.stringify({ error: 'invalid_spec', errors: [{ code: 'guessed_value', param: 'x', message: 'x looks guessed' }] }))
    const api = fakeApi({ confirm: vi.fn().mockRejectedValue(rejected) })
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    let s = flow.store.getState()
    expect(s.confirmation).toBeNull()
    expect(s.confirmErrors).toEqual([{ code: 'guessed_value', param: 'x', message: 'x looks guessed' }])

    api.confirm = fakeApi().confirm
    api.runTool = vi.fn().mockRejectedValue(new BridgeApiError(400, JSON.stringify({ success: false, error: 'confirmation_required', hint: 'confirm first' })))
    await flow.confirm()
    await flow.run()
    s = flow.store.getState()
    expect(s.execution).toEqual({ success: false, error: 'confirmation_required', message: '400: confirmation_required', hint: 'confirm first' })
    expect(s.confirmation?.used).toBe(false)
  })

  it('a new proposal supersedes the confirmation; reset clears everything but the compare switch', async () => {
    const host = scriptedHost(
      [{ type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }],
      [{ type: 'spec', spec: { ...spec, task: 'again' }, card: 'c2', errors: [], reconcile: ready }, { type: 'done' }],
    )
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('brief')
    await flow.confirm()
    expect(flow.store.getState().confirmation).not.toBeNull()
    await flow.send('change it')
    expect(flow.store.getState().confirmation).toBeNull()
    expect(flow.store.getState().proposal?.spec.task).toBe('again')
    flow.setCompare(true)
    flow.reset()
    expect(flow.store.getState()).toMatchObject({ proposal: null, compare: true, bridge: { messages: [], sessionId: null } })
  })
})

describe('the trace of each reply', () => {
  const stage = (status: 'start' | 'end'): HostEvent => ({ type: 'stage', role: 'planner', model: 'm-1', status })
  const tool = (id: string, status: 'running' | 'ok' | 'error'): HostEvent =>
    ({ type: 'tool', id, role: 'planner', name: 'query', args: { kind: 'levels' }, status })
  const usage = (round: number, input: number | null, cacheRead: number | null = 0): HostEvent =>
    ({ type: 'usage', role: 'planner', model: 'm-1', round, input, output: 10, cache_read: cacheRead, cache_write: null, ms: 5 })

  const traceOf = (turn: HostEvent[]) => turn.filter(e => e.type === 'stage' || e.type === 'tool' || e.type === 'usage')

  it('keeps stage, tool and usage events with the assistant message of their turn, in order', async () => {
    const turn1: HostEvent[] = [
      { type: 'session', id: 's1' }, stage('start'), usage(1, 1000), tool('1.1.1', 'running'), tool('1.1.1', 'ok'),
      { type: 'token', text: 'L1 and L2.' }, usage(2, 1200, 1000), stage('end'), { type: 'done' },
    ]
    const turn2: HostEvent[] = [stage('start'), { type: 'token', text: 'ok' }, usage(1, 1300, 1200), stage('end'), { type: 'done' }]
    const host = scriptedHost(turn1, turn2)
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('levels?')
    let s = flow.store.getState()
    expect(s.bridge.messages[1]).toEqual({ role: 'assistant', content: 'L1 and L2.', trace: traceOf(turn1) })
    expect(s.bridge.messages[0].trace).toBeUndefined()
    await flow.send('thanks')
    s = flow.store.getState()
    expect(s.bridge.messages[3].trace).toEqual(traceOf(turn2))
    expect(s.bridge.messages[1].trace).toEqual(traceOf(turn1))      // the earlier reply keeps its own
    const { turn, session } = usageOf(s.bridge)
    expect(turn.map(u => u.round)).toEqual([1])
    expect(session.map(u => u.input)).toEqual([1000, 1200, 1300])
    expect(sumUsage(session)).toMatchObject({ input: 3500, cache_read: 2200, output: 30, calls: 3, cache_write: null })
  })

  it('an unknown count stays unknown in the totals, and the no-bridge side counts its own calls', async () => {
    const host = scriptedHost(
      [stage('start'), usage(1, null, null), { type: 'token', text: 'asks' }, stage('end'), { type: 'done' }],
      [stage('start'), usage(1, 400), { type: 'token', text: 'guesses' }, stage('end'), { type: 'done' }],
    )
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    flow.setCompare(true)
    await flow.send('brief')
    const s = flow.store.getState()
    const bridge = sumUsage(usageOf(s.bridge).session)
    expect(bridge.input).toBeNull()
    expect(bridge.cache_read).toBeNull()
    expect(bridge.output).toBe(10)
    expect(usageOf(s.baseline).session.map(u => u.input)).toEqual([400])
    expect(sumUsage(usageOf(s.baseline).turn).input).toBe(400)
  })

  it('after an abort, the tool and the stage still open are marked interrupted', async () => {
    const events = async function* (_request: HostRequest, signal?: AbortSignal): AsyncGenerator<HostEvent> {
      yield stage('start')
      yield usage(1, 100)
      yield tool('1.1.1', 'running')
      await new Promise((_, reject) => signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))))
    }
    const flow = createTaskFlow({ events, api: fakeApi() })
    const sending = flow.send('levels?')
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(flow.store.getState().bridge.messages[1].trace?.map(e => e.type === 'usage' ? 'usage' : e.status)).toEqual(['start', 'usage', 'running'])
    flow.abort()
    await sending
    const s = flow.store.getState()
    expect(s.bridge.streaming).toBe(false)
    const trace = s.bridge.messages[1].trace ?? []
    expect(trace.map(e => e.type === 'usage' ? 'usage' : e.status)).toEqual(['start', 'usage', 'running', 'interrupted', 'interrupted'])
    expect(trace.slice(3).map(e => e.type)).toEqual(['tool', 'stage'])
    expect(s.bridge.messages[1].content).not.toContain('Model error')   // an abort is not an error
  })

  it('a stream that breaks off closes the open rows too; a finished one is left alone', async () => {
    const host = scriptedHost([stage('start'), tool('1.1.1', 'running')])                 // ends without done
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('x')
    expect(flow.store.getState().bridge.messages[1].trace?.map(e => e.type === 'usage' ? 'usage' : e.status))
      .toEqual(['start', 'running', 'interrupted', 'interrupted'])
  })

  it('a transcript with no replies has no usage; a model error keeps the trace it had', async () => {
    expect(usageOf(initialState().bridge)).toEqual({ turn: [], session: [] })
    const host = scriptedHost([stage('start'), usage(1, 10), stage('end'), { type: 'error', detail: 'HTTP 500' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('x')
    const reply = flow.store.getState().bridge.messages[1]
    expect(reply.content).toBe('**Model error:** HTTP 500')
    expect(reply.trace?.map(e => e.type)).toEqual(['stage', 'usage', 'stage'])
  })
})

describe('the helpers', () => {
  it('paramsOf keeps the spec values untouched', () => {
    expect(paramsOf(spec)).toEqual({ type_name: 'UC305x305x97', level_name: 'L1', x: 3000, y: 3000 })
  })

  it('coerceLike keeps numbers numeric and booleans boolean', () => {
    expect(coerceLike('3001', 3000)).toBe(3001)
    expect(coerceLike('abc', 3000)).toBe('abc')
    expect(coerceLike('true', false)).toBe(true)
    expect(coerceLike('L2', 'L1')).toBe('L2')
  })
})

describe("the designer's picks", () => {
  const f2: Mention = { kind: 'level', id: 312, name: 'F2' }
  const f9: Mention = { kind: 'level', id: 99, name: 'F9' }
  const report: HostEvent = {
    type: 'mentions',
    resolved: [{ kind: 'level', id: 312, name: 'F2', label: 'F2', detail: '3000 mm', elevation_mm: 3000 }],
    not_found: [{ kind: 'level', id: 99, reason: 'not_found' }], invalid: [], unresolved: [],
  }

  it('go with the message on the bridge side only, and the sent message takes what the mentions event says', async () => {
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, report, { type: 'token', text: 'On F2.' }, { type: 'done' }],
      [{ type: 'session', id: 'without' }, { type: 'token', text: 'guesses' }, { type: 'done' }],
    )
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    flow.setCompare(true)
    await flow.send('  a wall on @level:F2 or @level:F9 ', [f2, f9, f2])          // F2 twice: sent once
    const message = 'a wall on @level:F2 or @level:F9'
    expect(host.requests).toEqual([
      { message, session_id: null, bridge: true,
        mentions: [{ kind: 'level', id: 312 }, { kind: 'level', id: 99 }] },
      { message, session_id: null, bridge: false },               // the comparison has no model to check them against
    ])
    const s = flow.store.getState()
    expect(s.bridge.messages[0]).toEqual({ role: 'user', content: message, mentions: [
      { ...f2, status: 'resolved' }, { ...f9, status: 'not_found', reason: 'not_found' }] })
    expect(s.bridge.messages[1]).toMatchObject({ role: 'assistant', content: 'On F2.' })
    expect(s.baseline.messages[0]).toEqual({ role: 'user', content: message })
  })

  it('a message without picks is sent as before: no mentions field, nothing on the message', async () => {
    const host = scriptedHost([{ type: 'token', text: 'ok' }, { type: 'done' }], [{ type: 'done' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('plain')
    await flow.send('also plain', [])
    expect(host.requests).toEqual([{ message: 'plain', session_id: null, bridge: true },
                                   { message: 'also plain', session_id: null, bridge: true }])
    expect(flow.store.getState().bridge.messages.filter(m => m.role === 'user')).toEqual([
      { role: 'user', content: 'plain' }, { role: 'user', content: 'also plain' }])
  })

  it('picks stay pending when the turn ends before the host said anything about them', async () => {
    const host = scriptedHost([{ type: 'error', detail: 'model down' }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi() })
    await flow.send('on @level:F2', [f2])
    expect(flow.store.getState().bridge.messages[0].mentions).toEqual([{ ...f2, status: 'pending' }])
  })

  it('a reconcile of the draft carries the chat session too', async () => {
    const host = scriptedHost([{ type: 'session', id: 's1' }, specEvent, { type: 'done' }])
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.toggleInterpretation('3000 read as mm', true)
    expect(api.calls.map(([name, args]) => [name, args[1]])).toEqual([['reconcile', 's1']])
  })
})

/* A used-up output limit (backend: Turn.limit_notice) ends the turn with an error event whose
   detail is the notice: it must reach the designer whole, in the chat, in every chat mode, and
   leave what the turn already produced (a card, a reported run) as it was. */
describe('the notice of a used-up output limit', () => {
  const planner = 'Output limit used up: the planner (main-model) spent its whole output limit (max_tokens 16384) before ' +
    'it wrote a reply or called a tool - most likely on thinking, which counts toward the limit. The spec card proposed ' +
    'in this turn is kept: it can still be confirmed and run; only the text after it is missing. To raise it, set a ' +
    "larger max_tokens in this model's entry in the host's models file."
  const supervisor = 'Output limit used up: the supervisor (small-model) spent its whole output limit (max_tokens ' +
    '16384) before it wrote a reply or called a tool - most likely on thinking, which counts toward the limit. The run ' +
    "you reported and its result (evidence ev_9) stay as the page received them. The supervisor's report on it is " +
    'missing, and the planner has not been told of this run: say what it did in your next message. To raise it, set ' +
    "max_tokens in this model's entry in the host's models file (it sets none, so the host's default, 16384, " +
    'applies), or LLM_MAX_TOKENS on the host for every entry without one.'
  const readySpec: HostEvent = { ...specEvent, reconcile: ready } as HostEvent
  const stageOf = (role: 'planner' | 'supervisor', status: 'start' | 'end'): HostEvent =>
    ({ type: 'stage', role, model: role === 'planner' ? 'main-model' : 'small-model', status })

  for (const mode of ['single', 'roles', 'roles_analyst'] as const) {
    it(`is shown in the chat whole, and the card before it stays (mode ${mode})`, async () => {
      const host = scriptedHost([{ type: 'session', id: 's1' }, stageOf('planner', 'start'), readySpec,
        stageOf('planner', 'end'), { type: 'error', detail: planner }])
      const flow = createTaskFlow({ events: host.events, api: fakeApi(), mode: () => mode })
      await flow.send('a column at (3000, 3000)')
      const s = flow.store.getState()
      expect(host.requests[0].mode).toBe(mode)
      expect(s.bridge.messages[1].content).toBe(`**Model error:** ${planner}`)
      expect(s.bridge.streaming).toBe(false)
      expect(s.proposal?.spec).toEqual(spec)                     // the card is still there to confirm
      expect(canConfirm(s)).toBe(true)
    })
  }

  it("after a reported run the supervisor's notice is shown and the run stays as the page received it", async () => {
    const host = scriptedHost(
      [{ type: 'session', id: 's1' }, readySpec, { type: 'done' }],
      [{ type: 'execution', execution: { ...success, evidence_id: 'ev_9' } }, stageOf('supervisor', 'start'),
        stageOf('supervisor', 'end'), { type: 'error', detail: supervisor }])
    const flow = createTaskFlow({ events: host.events, api: fakeApi({ runTool: async () => ({ ...success, evidence_id: 'ev_9' }) }),
      mode: () => 'roles' })
    await flow.send('a column')
    await flow.confirm()
    await flow.run()
    const s = flow.store.getState()
    expect(host.requests[1].execution?.evidence_id).toBe('ev_9')
    expect(s.bridge.messages.at(-1)?.content).toBe(`**Model error:** ${supervisor}`)
    expect(s.execution?.evidence_id).toBe('ev_9')                // the run's result stays on the page
    expect(s.reported).toBe(true)
    expect(s.bridge.streaming).toBe(false)
  })
})
