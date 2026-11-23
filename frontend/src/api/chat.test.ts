import { describe, expect, it } from 'vitest'
import { parseSseFrame } from './chat'

const spec = {
  task: 'run create_wall',
  action: { kind: 'run_tool', tool: 'create_wall', code: null, code_parameters: null },
  parameters: [{ name: 'level_name', value: 'L1', unit: null, source: 'answer', evidence: 'q_level_name' }],
}

describe('parseSseFrame', () => {
  it('reads reply tokens from plain data frames', () => {
    expect(parseSseFrame('data: "Hel"')).toEqual({ type: 'token', text: 'Hel' })
    expect(parseSseFrame('data: "多行\\n文本"')).toEqual({ type: 'token', text: '多行\n文本' })
    expect(parseSseFrame('data:"tight"')).toEqual({ type: 'token', text: 'tight' })
  })

  it('reads the spec event as the flattened data object', () => {
    const reconcile = { conflicts: [], questions: [], interpretations_required: [], ready: true }
    const frame = `event: spec\ndata: ${JSON.stringify({ spec, card: 'Task: run create_wall', errors: [], reconcile })}`
    expect(parseSseFrame(frame)).toEqual({ type: 'spec', spec, card: 'Task: run create_wall', errors: [], reconcile })
  })

  it('passes the questions of reconcile through as they come: kind, blocks and any newer key', () => {
    const questions = [
      { id: 'q_level_name', param: 'level_name', text: 'Which level?', why: "level_name = 'L9' does not exist in the snapshot",
        options: [{ label: 'L1 (0mm)', value: 'L1', source: 'tool:levels' }], allow_other: true, kind: 'model_fact', blocks: ['spec', 'execute'] },
      { id: 'q_type_name', param: 'type_name', text: 'Which type?', why: 'ambiguous', options: [], allow_other: true,
        kind: 'user_intent', blocks: ['spec', 'execute'], later_key: 1 },
      { id: 'q_height', param: 'height', text: 'Height?', why: 'required', options: [], allow_other: true },   // a 0.3 server
    ]
    const reconcile = { conflicts: [{ param: 'level_name', claimed: 'L9', kind: 'not_found', available: ['L1'], message: 'm' }],
      questions, interpretations_required: [], ready: false }
    const event = parseSseFrame(`event: spec\ndata: ${JSON.stringify({ spec, card: 'c', errors: [], reconcile })}`)
    expect(event).toEqual({ type: 'spec', spec, card: 'c', errors: [], reconcile })
    if (event?.type !== 'spec') throw new Error('not a spec event')
    expect(event.reconcile?.questions.map(q => q.kind)).toEqual(['model_fact', 'user_intent', undefined])
  })

  it('keeps reconcile null and carries reconcile_error when Revit was not reachable', () => {
    const data = { spec, card: 'Task: x', errors: [{ code: 'missing_param', param: 'height', message: 'm' }],
      reconcile: null, reconcile_error: { error: 'revit_unreachable', message: 'connect refused' } }
    const event = parseSseFrame(`event: spec\ndata: ${JSON.stringify(data)}`)
    expect(event).toEqual({ type: 'spec', ...data })
  })

  it('wraps the execution result under execution', () => {
    const execution = { success: false, error: 'validation_failed', tool: 'create_wall', result: { Status: 'Created' },
      validation: { validator: 'count_delta', passed: false, checks: [{ name: 'count_delta', passed: false, detail: 'd' }] },
      evidence_id: 'ev_1', warnings: [], preconditions_failed: [] }
    expect(parseSseFrame(`event: execution\ndata: ${JSON.stringify(execution)}`)).toEqual({ type: 'execution', execution })
  })

  it('reads error frames: message, then error, then detail, then the raw text', () => {
    expect(parseSseFrame('event: error\ndata: {"error": "rate_limited", "message": "slow down"}')).toEqual({ type: 'error', detail: 'slow down' })
    expect(parseSseFrame('event: error\ndata: {"error": "revit_unreachable"}')).toEqual({ type: 'error', detail: 'revit_unreachable' })
    expect(parseSseFrame('event: error\ndata: {"detail": "Model endpoint returned HTTP 401"}')).toEqual({ type: 'error', detail: 'Model endpoint returned HTTP 401' })
    expect(parseSseFrame('event: error\ndata: not json')).toEqual({ type: 'error', detail: 'not json' })
  })

  it('ends on done and ignores blank, comment and malformed frames', () => {
    expect(parseSseFrame('event: done\ndata: [DONE]')).toEqual({ type: 'done' })
    expect(parseSseFrame('data: [DONE]')).toEqual({ type: 'done' })
    expect(parseSseFrame('')).toBeNull()
    expect(parseSseFrame(': keep-alive')).toBeNull()
    expect(parseSseFrame('event: spec\ndata: {"card": "x"}')).toBeNull()      // no spec object
    expect(parseSseFrame('data: {"not": "a token"}')).toBeNull()
    expect(parseSseFrame('event: execution\ndata: 42')).toBeNull()
    expect(parseSseFrame('event: unknown\ndata: "x"')).toBeNull()
  })

  it('reads the stage, tool and usage events (plan 2.2)', () => {
    expect(parseSseFrame('event: stage\ndata: {"role": "planner", "model": "m-1", "status": "start"}'))
      .toEqual({ type: 'stage', role: 'planner', model: 'm-1', status: 'start' })
    expect(parseSseFrame('event: tool\ndata: {"id": "1.1.1", "role": "planner", "name": "query", "args": {"kind": "levels"}, "status": "running"}'))
      .toEqual({ type: 'tool', id: '1.1.1', role: 'planner', name: 'query', args: { kind: 'levels' }, status: 'running' })
    const closing = { id: '1.1.1', role: 'planner', name: 'query', args: { kind: 'levels' }, status: 'error', ms: 12, size: 40,
      preview: '{"error": "revit_unreachable"}', error: 'revit_unreachable' }
    expect(parseSseFrame(`event: tool\ndata: ${JSON.stringify(closing)}`)).toEqual({ type: 'tool', ...closing })
    const usage = { role: 'planner', model: 'm-1', round: 2, input: 1200, output: 40, cache_read: 1024, cache_write: 0, ms: 850 }
    expect(parseSseFrame(`event: usage\ndata: ${JSON.stringify(usage)}`)).toEqual({ type: 'usage', ...usage })
  })

  it('keeps an unreported count unknown: null or absent is null, never 0', () => {
    expect(parseSseFrame('event: usage\ndata: {"role": "planner", "model": "m", "round": 1, "input": null, "output": 5, "ms": 3}'))
      .toEqual({ type: 'usage', role: 'planner', model: 'm', round: 1, input: null, output: 5, cache_read: null, cache_write: null, ms: 3 })
    expect(parseSseFrame('event: usage\ndata: {"role": "planner", "model": "m", "round": 1, "input": "12", "output": 0, "cache_read": 0, "cache_write": null, "ms": 1}'))
      .toMatchObject({ input: null, output: 0, cache_read: 0, cache_write: null })
  })

  it('ignores trace frames it cannot read, and a tool without optional fields has none', () => {
    expect(parseSseFrame('event: stage\ndata: {"role": "boss", "model": "m", "status": "start"}')).toBeNull()
    expect(parseSseFrame('event: stage\ndata: {"role": "planner", "model": "m", "status": "paused"}')).toBeNull()
    expect(parseSseFrame('event: tool\ndata: {"role": "planner", "name": "q", "status": "ok"}')).toBeNull()       // no id
    expect(parseSseFrame('event: tool\ndata: {"id": "1", "role": "planner", "name": "q", "status": "done"}')).toBeNull()
    expect(parseSseFrame('event: usage\ndata: [1, 2]')).toBeNull()
    const bare = parseSseFrame('event: tool\ndata: {"id": 7, "role": "planner", "name": "q", "args": "x", "status": "ok", "ms": null}')
    expect(bare).toEqual({ type: 'tool', id: '7', role: 'planner', name: 'q', args: {}, status: 'ok' })
    expect(parseSseFrame('event: thinking\ndata: {"role": "planner"}')).toBeNull()                       // unknown event
  })

  it('tolerates CRLF line endings', () => {
    expect(parseSseFrame('event: done\r\ndata: [DONE]\r\n')).toEqual({ type: 'done' })
    expect(parseSseFrame('data: "a"\r')).toEqual({ type: 'token', text: 'a' })
  })
})
