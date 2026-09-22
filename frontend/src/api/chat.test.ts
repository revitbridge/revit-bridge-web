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

  it('tolerates CRLF line endings', () => {
    expect(parseSseFrame('event: done\r\ndata: [DONE]\r\n')).toEqual({ type: 'done' })
    expect(parseSseFrame('data: "a"\r')).toEqual({ type: 'token', text: 'a' })
  })
})
