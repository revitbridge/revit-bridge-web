import { describe, expect, it, vi } from 'vitest'
import { TaskApiError } from './api'
import { canConfirm, coerceLike, createTaskFlow, interpretationsOf, paramsOf, SPEC_ONLY_REPLY, type FlowApi } from './flow'
import type { ExecutionResult, HostEvent, HostRequest, ReconcileResult, TaskSpec } from '../../types/api'

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
    execute: record('execute', success),
    ...overrides,
  }
}

const specEvent: HostEvent = { type: 'spec', spec, card: 'Task: Place a structural column\nConfirm? (yes / change something)', errors: [], reconcile: notReady }

describe('the task flow', () => {
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

  it('ticking an interpretation reconciles the draft again and lights Confirm', async () => {
    const host = scriptedHost([specEvent, { type: 'done' }])
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    expect(interpretationsOf(flow.store.getState().proposal!)).toEqual([{ param: 'x', text: '3000 read as mm', confirmed: false }])
    await flow.toggleInterpretation('3000 read as mm', true)
    const s = flow.store.getState()
    expect(api.calls).toEqual([['reconcile', [{ ...spec, interpretations: [{ param: 'x', text: '3000 read as mm', confirmed: true }] }]]])
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
    expect(api.calls).toEqual([['confirm', [spec]]])
    expect(s.confirmation).toMatchObject({ token: 'tok_abcdef0123456789', expires_at: '2026-09-22T09:10:00Z', used: false })
    expect(s.confirmation?.spec).toEqual(spec)

    await flow.run()
    s = flow.store.getState()
    expect(api.calls[1]).toEqual(['runTool', ['create_structural_column', { type_name: 'UC305x305x97', level_name: 'L1', x: 3000, y: 3000 }, 'tok_abcdef0123456789']])
    expect(s.execution).toEqual(success)
    expect(s.executionKind).toBe('genuine')
    expect(s.confirmation?.used).toBe(true)
    // the report: POST /api/chat {session_id, execution} without a message
    expect(host.requests[1]).toEqual({ execution: success, session_id: 's1' })
    expect(s.reported).toBe(true)
    expect(s.bridge.messages.slice(-2)).toEqual([
      { role: 'host', content: 'Execution result sent to the model (evidence ev_20260922T090000_abc123).' },
      { role: 'assistant', content: 'Created column 1234; validator created_ids passed.' },
    ])
  })

  it('tamper demo: an edited value under the same token is refused and nothing is reported', async () => {
    const refusal: ExecutionResult = { success: false, error: 'confirmation_invalid', reason: 'mismatch', message: 'projection hash differs' }
    const host = scriptedHost([{ type: 'session', id: 's1' }, { type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const api = fakeApi({ runTool: vi.fn().mockResolvedValue(refusal) })
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    await flow.runTampered({ param: 'x', value: '3001' })
    const s = flow.store.getState()
    expect(api.runTool).toHaveBeenCalledWith('create_structural_column', { type_name: 'UC305x305x97', level_name: 'L1', x: 3001, y: 3000 }, 'tok_abcdef0123456789')
    expect(s.execution).toEqual(refusal)
    expect(s.executionKind).toBe('tampered')
    expect(s.confirmation?.used).toBe(false)     // a mismatch consumes nothing
    expect(s.reported).toBe(false)
    expect(host.requests).toHaveLength(1)        // no report went to the model
  })

  it('execute_code runs the confirmed code with its parameters; tampered code is what gets sent', async () => {
    const codeSpec: TaskSpec = { ...spec, action: { kind: 'execute_code', code: 'return 1;', code_parameters: [] }, parameters: [], interpretations: [] }
    const host = scriptedHost([{ type: 'spec', spec: codeSpec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const api = fakeApi()
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    await flow.runTampered({ code: 'return 2;' })
    expect(api.calls[1]).toEqual(['execute', ['return 2;', [], 'tok_abcdef0123456789']])
    // no session yet: the report is skipped, not attempted
    expect(host.requests).toHaveLength(1)
  })

  it('a 422 from /spec/confirm lists the spec errors; a 400 from /run is shown in the result shape', async () => {
    const host = scriptedHost([{ type: 'spec', spec, card: 'c', errors: [], reconcile: ready }, { type: 'done' }])
    const rejected = new TaskApiError(422, JSON.stringify({ error: 'invalid_spec', errors: [{ code: 'guessed_value', param: 'x', message: 'x looks guessed' }] }))
    const api = fakeApi({ confirm: vi.fn().mockRejectedValue(rejected) })
    const flow = createTaskFlow({ events: host.events, api })
    await flow.send('brief')
    await flow.confirm()
    let s = flow.store.getState()
    expect(s.confirmation).toBeNull()
    expect(s.confirmErrors).toEqual([{ code: 'guessed_value', param: 'x', message: 'x looks guessed' }])

    api.confirm = fakeApi().confirm
    api.runTool = vi.fn().mockRejectedValue(new TaskApiError(400, JSON.stringify({ success: false, error: 'confirmation_required', hint: 'confirm first' })))
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
