import { afterEach, describe, expect, it, vi } from 'vitest'
import { useSessionStore } from '../../store'
import { taskApi, TaskApiError } from './api'

type Call = { url: string; init: RequestInit }

function stubFetch(status: number, body: unknown) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    const text = typeof body === 'string' ? body : JSON.stringify(body)
    return new Response(text, { status, headers: { 'content-type': 'application/json' } })
  }))
  return calls
}

const parse = (c: Call) => JSON.parse(String(c.init.body))

afterEach(() => {
  vi.unstubAllGlobals()
  useSessionStore.getState().setSlot('')
})

describe('taskApi', () => {
  it('confirms as the designer and returns the token payload', async () => {
    const calls = stubFetch(200, { token: 'tok_1', spec_hash: 'h', expires_at: 'e', card: 'c' })
    const spec = { task: 't', action: { kind: 'run_tool' as const, tool: 'query_levels' }, parameters: [] }
    expect(await taskApi.confirm(spec)).toEqual({ token: 'tok_1', spec_hash: 'h', expires_at: 'e', card: 'c' })
    expect(calls[0].url).toBe('/api/v1/bridge/spec/confirm')
    expect(calls[0].init.method).toBe('POST')
    expect(parse(calls[0])).toEqual({ spec, confirmed_by: 'designer' })
  })

  it('runs a pack and code with the token in the body, values untouched', async () => {
    const calls = stubFetch(200, { success: true, error: null })
    await taskApi.runTool('create structural/column', { x: 3000, level_name: 'L1' }, 'tok_1')
    await taskApi.execute('return 1;', [{ name: 'a', value: 1 }], 'tok_2')
    expect(calls[0].url).toBe('/api/v1/bridge/tools/create%20structural%2Fcolumn/run')
    expect(parse(calls[0])).toEqual({ params: { x: 3000, level_name: 'L1' }, token: 'tok_1' })
    expect(calls[1].url).toBe('/api/v1/bridge/execute')
    expect(parse(calls[1])).toEqual({ code: 'return 1;', parameters: [{ name: 'a', value: 1 }], token: 'tok_2' })
  })

  it('asks for missing params, reconciles, lists and validates evidence', async () => {
    const calls = stubFetch(200, [])
    await taskApi.missingParams('create_wall', { height: 3000 }, undefined, 'zh')
    await taskApi.reconcile({ task: 't', action: { kind: 'execute_code', code: 'x' }, parameters: [] })
    await taskApi.evidence(10, 'create_wall')
    await taskApi.evidence()
    await taskApi.validateEvidence('ev_1')
    expect(calls.map(c => c.url)).toEqual([
      '/api/v1/bridge/tools/create_wall/missing-params',
      '/api/v1/bridge/spec/reconcile',
      '/api/v1/bridge/evidence?limit=10&tool=create_wall',
      '/api/v1/bridge/evidence?limit=50',
      '/api/v1/bridge/evidence/ev_1/validate',
    ])
    expect(parse(calls[0])).toEqual({ known: { height: 3000 }, snapshot: null, language: 'zh' })
    expect(parse(calls[1])).toEqual({ spec: { task: 't', action: { kind: 'execute_code', code: 'x' }, parameters: [] }, snapshot: null })
    expect(calls[2].init.method).toBeUndefined()
    expect(calls[4].init.method).toBe('POST')
  })

  it('sends the slot headers like the rest of the client', async () => {
    const calls = stubFetch(200, [])
    useSessionStore.getState().setSlot('2')
    useSessionStore.getState().setSlotToken('secret')
    await taskApi.evidence()
    expect(calls[0].init.headers).toMatchObject({ 'X-Slot-Id': '2', 'X-Slot-Token': 'secret', 'Content-Type': 'application/json' })
  })

  it('keeps the rejected body: spec errors of /spec/confirm, pack problems of /solidify', async () => {
    stubFetch(422, { error: 'invalid_spec', errors: [{ code: 'missing_param', param: 'x', message: 'x is required' }] })
    const spec = { task: 't', action: { kind: 'run_tool' as const, tool: 'q' }, parameters: [] }
    const err = await taskApi.confirm(spec).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(TaskApiError)
    const failure = err as TaskApiError
    expect(failure.status).toBe(422)
    expect(failure.code).toBe('invalid_spec')
    expect(failure.message).toBe('422: invalid_spec')
    expect(failure.body?.errors).toEqual([{ code: 'missing_param', param: 'x', message: 'x is required' }])

    stubFetch(422, { error: 'invalid_pack', problems: ['parameters[0] (x): required must be true or false', 'validator: unknown kind'] })
    const pack = await taskApi.solidify({ name: 'n', code: 'c', description: '', parameters: [], source_query: '' }).catch((e: unknown) => e as TaskApiError)
    expect((pack as TaskApiError).body?.problems).toHaveLength(2)

    stubFetch(502, '<!doctype html><html></html>')
    const html = await taskApi.evidence().catch((e: unknown) => e as TaskApiError)
    expect((html as TaskApiError).body).toBeNull()
    expect((html as TaskApiError).message).toBe('502: backend unreachable (HTML page instead of JSON)')
  })
})
