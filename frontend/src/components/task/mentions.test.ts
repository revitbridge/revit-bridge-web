import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage, MentionReport, SentMention } from '../../types/api'
import type { Mention } from '../../types/hostv2'
import { MentionController } from '../hostv2/mention/controller'
import {
  distinctMentions, fetchMentionCandidates, MAX_PICKS, MENTION_TIMEOUT_MS, mentionNote, mentionRefs, pendingMentions,
  TIMEOUT_MESSAGE, tooManyPicks, unavailableInInput, unavailableMentions, withReport,
} from './mentions'

const f2: Mention = { kind: 'level', id: 312, name: 'F2' }
const wall: Mention = { kind: 'type', id: 401, name: 'Generic - 200mm' }
const mep: Mention = { kind: 'link', id: 502, name: 'MEP.rvt' }
const levels = { kind: 'level', items: [{ kind: 'level', id: 312, name: 'F2', label: 'F2', detail: '3000 mm' }], total: 1, truncated: false }

type Call = { url: string; init: RequestInit }

function stubFetch(status: number, body: unknown) {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
  }))
  return calls
}

/* A host that never answers, except to the abort. */
function stubHangingFetch() {
  const calls: Call[] = []
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit) => {
    calls.push({ url, init })
    return new Promise<Response>((_, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')))
    })
  }))
  return calls
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('the picker asks the host', () => {
  it('GET /mentions with the kind, the category and the text typed so far', async () => {
    const calls = stubFetch(200, levels)
    expect(await fetchMentionCandidates('level', { q: 'F' })).toEqual(levels)
    await fetchMentionCandidates('type', { category: 'OST_Walls', q: '200 mm' })
    await fetchMentionCandidates('type', {})
    expect(calls.map(c => c.url)).toEqual([
      '/api/v1/bridge/mentions?kind=level&q=F',
      '/api/v1/bridge/mentions?kind=type&category=OST_Walls&q=200+mm',
      '/api/v1/bridge/mentions?kind=type',
    ])
    expect(calls[0].init.method).toBeUndefined()
    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal)
  })

  it('says why when Revit is not connected, or when Revit answered with an error', async () => {
    stubFetch(503, { error: 'revit_unreachable', message: 'connection refused', kind: 'level' })
    await expect(fetchMentionCandidates('level', {})).rejects.toThrow('Revit is not connected, so there is nothing to pick: connection refused')
    stubFetch(200, { error: 'revit_error', kind: 'type', message: 'busy: Revit is busy' })
    await expect(fetchMentionCandidates('type', { category: 'OST_Walls' })).rejects.toThrow('busy: Revit is busy')
    stubFetch(422, { error: 'invalid_args', kind: 'type', message: "invalid category 'Walls'" })
    await expect(fetchMentionCandidates('type', { category: 'Walls' })).rejects.toThrow("invalid category 'Walls'")
  })

  it('gives up after 10 seconds', async () => {
    vi.useFakeTimers()
    const calls = stubHangingFetch()
    const pending = fetchMentionCandidates('level', {})
    const outcome = pending.then(() => 'answered', (e: Error) => e.message)
    await vi.advanceTimersByTimeAsync(MENTION_TIMEOUT_MS - 1)
    expect(calls[0].init.signal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    expect(await outcome).toBe(TIMEOUT_MESSAGE)
  })

  it('a listing that hangs holds Enter only until the timeout: then Enter sends', async () => {
    vi.useFakeTimers()
    stubHangingFetch()
    const onSubmit = vi.fn()
    const controller = new MentionController('', [], { onChange: vi.fn(), onSubmit, fetchCandidates: fetchMentionCandidates })
    controller.editNative('on @level:', { start: 10, end: 10, direction: 'none' })
    await vi.advanceTimersByTimeAsync(200)                    // the picker's debounce: the request goes out
    expect(controller.getSnapshot().status).toBe('loading')
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(MENTION_TIMEOUT_MS)
    expect(controller.getSnapshot()).toMatchObject({ status: 'error', error: TIMEOUT_MESSAGE })
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('on @level:', [])
  })
})

describe('the picks of a message', () => {
  it('go to the chat as kind and id only, each item once', () => {
    expect(mentionRefs([f2, wall, { ...f2, name: 'F2 again' }])).toEqual([{ kind: 'level', id: 312 }, { kind: 'type', id: 401 }])
    expect(pendingMentions([f2, f2])).toEqual([{ ...f2, status: 'pending' }])
    expect(distinctMentions([wall, f2, wall])).toEqual([wall, f2])
  })

  it('at most 20 different items go with one message; the designer is told before it is sent', () => {
    const many = (n: number) => Array.from({ length: n }, (_, i): Mention => ({ kind: 'level', id: 1000 + i, name: `L${i}` }))
    expect(tooManyPicks(many(MAX_PICKS))).toBeNull()
    expect(tooManyPicks([...many(MAX_PICKS), ...many(MAX_PICKS)])).toBeNull()     // the same 20 twice
    expect(tooManyPicks(many(MAX_PICKS + 1))).toBe(
      'This message picks 21 model items; at most 20 can go with one message. Remove some tags, then send.')
  })

  it('take what the mentions event says about each, by kind and id', () => {
    const report: MentionReport = {
      resolved: [{ kind: 'level', id: 312, name: 'F2', label: 'F2', detail: '3000 mm', elevation_mm: 3000 }],
      not_found: [{ kind: 'link', id: 502, reason: 'not_loaded' }],
      invalid: [{ kind: 'type', id: 401, reason: 'invalid' }],
      unresolved: [],
    }
    const other: Mention = { kind: 'level', id: 999, name: 'F9' }
    expect(withReport(pendingMentions([f2, mep, wall, other]), report)).toEqual([
      { ...f2, status: 'resolved', reason: undefined },
      { ...mep, status: 'not_found', reason: 'not_loaded' },
      { ...wall, status: 'invalid', reason: 'invalid' },
      { ...other, status: 'pending' },                       // nothing said about it: as it was
    ])
    const down: MentionReport = { resolved: [], not_found: [], invalid: [],
      unresolved: [{ kind: 'level', id: 312, reason: 'revit_unreachable' }] }
    expect(withReport(pendingMentions([f2]), down)).toEqual([{ ...f2, status: 'unresolved', reason: 'revit_unreachable' }])
  })

  it('found gone or not valid are marked in the input; checked again later, they are not', () => {
    const sent = (...mentions: SentMention[]): ChatMessage => ({ role: 'user', content: 'x', mentions })
    const messages: ChatMessage[] = [
      sent({ ...f2, status: 'resolved' }, { ...mep, status: 'not_found', reason: 'not_loaded' }),
      { role: 'assistant', content: 'ok' },
      sent({ ...wall, status: 'invalid' }, { ...f2, status: 'unresolved', reason: 'revit_unreachable' }),
    ]
    expect(unavailableMentions(messages)).toEqual([mep, wall])
    messages.push(sent({ ...mep, status: 'resolved' }), sent({ ...wall, status: 'pending' }))
    expect(unavailableMentions(messages)).toEqual([wall])   // a pending pick says nothing yet
    expect(unavailableMentions([])).toEqual([])
    // The input marks only those of them that are in it now.
    expect(unavailableInInput(messages, [f2, wall])).toEqual([wall])
    expect(unavailableInInput(messages, [f2])).toEqual([])
  })

  it('read on their chips as what happened to them', () => {
    expect(mentionNote({ ...f2, status: 'resolved' })).toBe('checked against the model')
    expect(mentionNote({ ...mep, status: 'not_found', reason: 'not_loaded' })).toBe('not used: not loaded')
    expect(mentionNote({ ...wall, status: 'invalid', reason: 'invalid' })).toBe('not used: not a model item')
    expect(mentionNote({ ...f2, status: 'unresolved', reason: 'revit_unreachable' })).toBe('not checked: revit unreachable')
    expect(mentionNote({ ...f2, status: 'pending' })).toBe('not checked yet')
  })
})
