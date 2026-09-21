import { describe, expect, it } from 'vitest'
import { describeFailure } from './client'

describe('describeFailure', () => {
  it('reads the bridge error body: error code, then message', () => {
    expect(describeFailure(503, JSON.stringify({ error: 'revit_unreachable', message: 'connect refused', endpoint: 'x:1' })))
      .toBe('503: revit_unreachable: connect refused')
    expect(describeFailure(400, JSON.stringify({ error: 'confirmation_required', success: false, hint: 'confirm first' })))
      .toBe('400: confirmation_required')
    expect(describeFailure(422, JSON.stringify({ error: 'invalid_pack', problems: ['a', 'b'] })))
      .toBe('422: invalid_pack')
  })

  it('still reads FastAPI detail, as a string or as a list', () => {
    expect(describeFailure(403, JSON.stringify({ detail: 'Admin password required' }))).toBe('403: Admin password required')
    expect(describeFailure(422, JSON.stringify({ detail: [{ loc: ['body', 'name'], msg: 'field required' }] })))
      .toBe('422: [{"loc":["body","name"],"msg":"field required"}]')
  })

  it('falls back to the raw text and spots an HTML page', () => {
    expect(describeFailure(500, 'boom')).toBe('500: boom')
    expect(describeFailure(502, '<!doctype html><html></html>')).toBe('502: backend unreachable (HTML page instead of JSON)')
    expect(describeFailure(404, '{}', 'text/html')).toBe('404: backend unreachable (HTML page instead of JSON)')
  })
})
