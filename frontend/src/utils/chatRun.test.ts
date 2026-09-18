import { describe, expect, it } from 'vitest'
import type { ChatEvent } from '../api/chat'
import { consumeChat, NO_RESPONSE } from './chatRun'

async function* events(...list: ChatEvent[]): AsyncGenerator<ChatEvent> {
  for (const e of list) yield e
}

function recorder() {
  const updates: string[] = []
  const sessions: string[] = []
  return { updates, sessions, cb: { onUpdate: (t: string) => updates.push(t), onSession: (id: string) => sessions.push(id) } }
}

describe('consumeChat', () => {
  it('renders a model error that arrives before any token', async () => {
    const r = recorder()
    const reply = await consumeChat(events(
      { type: 'session', id: 's1' },
      { type: 'error', detail: 'Model endpoint returned HTTP 401: bad key' },
    ), r.cb)
    expect(r.sessions).toEqual(['s1'])
    expect(r.updates).toEqual(['**Model error:** Model endpoint returned HTTP 401: bad key'])
    expect(reply).toContain('HTTP 401')
    expect(reply).not.toContain(NO_RESPONSE)
  })

  it('keeps the tokens already shown when the error comes mid-stream', async () => {
    const r = recorder()
    const reply = await consumeChat(events(
      { type: 'token', text: 'Hel' },
      { type: 'token', text: 'lo' },
      { type: 'error', detail: 'upstream closed' },
      { type: 'token', text: 'never shown' },
    ), r.cb)
    expect(r.updates).toEqual(['Hel', 'Hello', 'Hello\n\n**Model error:** upstream closed'])
    expect(reply).toBe('Hello\n\n**Model error:** upstream closed')
  })

  it('shows the empty-reply fallback only when nothing at all arrived', async () => {
    const r = recorder()
    expect(await consumeChat(events({ type: 'session', id: 's' }), r.cb)).toBe(NO_RESPONSE)
    expect(r.updates).toEqual([NO_RESPONSE])
  })

  it('accumulates tokens in order', async () => {
    const r = recorder()
    const reply = await consumeChat(events({ type: 'token', text: 'a' }, { type: 'token', text: 'b' }), r.cb)
    expect(reply).toBe('ab')
    expect(r.updates).toEqual(['a', 'ab'])
  })
})
