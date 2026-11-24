import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { TraceEvent, ToolEvent, UsageEvent } from '../../../types/hostv2'
import { groupTraceEvents, interruptOpen, mergeToolEvents, sumUsage, truncatedArgs } from './events'
import TokenPanel from './TokenPanel'
import TraceTimeline from './TraceTimeline'

const tool = (overrides: Partial<ToolEvent> = {}): ToolEvent => ({
  type: 'tool', id: 'tool-1', role: 'planner', name: 'query', args: { kind: 'levels' }, status: 'running', ...overrides,
})
const usage = (overrides: Partial<UsageEvent> = {}): UsageEvent => ({
  type: 'usage', role: 'planner', model: 'test-model', round: 1,
  input: 100, output: 20, cache_read: 25, cache_write: 10, ms: 50, ...overrides,
})

describe('mergeToolEvents', () => {
  it('keeps first-seen order and replaces the running event with its latest completion', () => {
    const events: TraceEvent[] = [tool(), tool({ id: 'tool-2' }), usage(),
      tool({ status: 'ok', ms: 15, size: 7, preview: '[1,2,3]' }),
      tool({ id: 'tool-2', status: 'error', error: 'No document', ms: 9 }),
    ]
    expect(mergeToolEvents(events)).toEqual([events[3], events[4]])
    expect(events[0]).toEqual(tool())
    expect(mergeToolEvents(events)[0]).not.toBe(events[3])
  })

  it('ignores stage and usage events and uses exact ids, including special property names', () => {
    expect(mergeToolEvents([])).toEqual([])
    expect(mergeToolEvents([
      { type: 'stage', role: 'planner', model: 'test-model', status: 'start' }, usage(),
      tool({ id: '__proto__' }), tool({ id: 'constructor' }), tool({ id: '__proto__', status: 'ok' }),
    ]).map(event => [event.id, event.status])).toEqual([['__proto__', 'ok'], ['constructor', 'running']])
  })
})

describe('groupTraceEvents', () => {
  it('groups by stage and keeps late completions with their original stage', () => {
    const groups = groupTraceEvents([
      { type: 'stage', role: 'analyst', model: 'small', status: 'start' },
      tool({ role: 'analyst' }),
      { type: 'stage', role: 'analyst', model: 'small', status: 'end' },
      { type: 'stage', role: 'planner', model: 'large', status: 'start' },
      tool({ id: 'tool-2' }),
      tool({ role: 'analyst', status: 'ok' }),
      { type: 'stage', role: 'planner', model: 'large', status: 'end' },
    ])
    expect(groups).toHaveLength(2)
    expect(groups.map(group => group.stage)).toEqual([
      { type: 'stage', role: 'analyst', model: 'small', status: 'end' },
      { type: 'stage', role: 'planner', model: 'large', status: 'end' },
    ])
    expect(groups.map(group => group.tools.map(event => [event.id, event.status])))
      .toEqual([[['tool-1', 'ok']], [['tool-2', 'running']]])
  })

  it('supports repeated roles, overlapping stages, and tools without a matching stage', () => {
    const groups = groupTraceEvents([
      tool({ id: 'before' }),
      { type: 'stage', role: 'analyst', model: 'small', status: 'start' },
      { type: 'stage', role: 'planner', model: 'large', status: 'start' },
      tool({ role: 'analyst' }), tool({ id: 'during' }),
      { type: 'stage', role: 'planner', model: 'large', status: 'end' },
      tool({ id: 'after' }),
      { type: 'stage', role: 'planner', model: 'other', status: 'start' },
      tool({ id: 'last' }),
    ])
    // 'after' is a planner tool once the planner stage has closed: it goes to the stage
    // still open around it (analyst), not to a detached group.
    expect(groups.map(group => group.tools.map(event => event.id)))
      .toEqual([['before'], ['tool-1', 'after'], ['during'], ['last']])
    expect(groups[0].stage).toBeUndefined()
    expect(groups[3].stage?.model).toBe('other')
  })

  it('keeps a tool whose role differs from the open stage inside that stage', () => {
    const groups = groupTraceEvents([
      { type: 'stage', role: 'planner', model: 'large', status: 'start' },
      tool({ id: 'mine' }),
      tool({ id: 'odd', role: 'supervisor' }),
      tool({ id: 'odd', role: 'supervisor', status: 'ok' }),
      { type: 'stage', role: 'planner', model: 'large', status: 'end' },
      tool({ id: 'outside', role: 'supervisor' }),
    ])
    expect(groups.map(group => [group.stage?.role, group.tools.map(event => [event.id, event.status])])).toEqual([
      ['planner', [['mine', 'running'], ['odd', 'ok']]],
      [undefined, [['outside', 'running']]],
    ])
  })

  it('puts a mismatched tool in the stage opened last when several are open', () => {
    const groups = groupTraceEvents([
      { type: 'stage', role: 'planner', model: 'large', status: 'start' },
      { type: 'stage', role: 'analyst', model: 'small', status: 'start' },
      tool({ id: 'x', role: 'supervisor' }),
      { type: 'stage', role: 'planner', model: 'large', status: 'start' },   // the planner opens again
      tool({ id: 'y', role: 'supervisor' }),
    ])
    expect(groups.map(group => group.tools.map(event => event.id))).toEqual([[], ['x'], ['y']])
  })
})

describe('sumUsage', () => {
  it('totals calls and tokens and derives the hit rate from total input, not an average', () => {
    expect(sumUsage([usage(), usage({ input: 300, output: 30, cache_read: 0, cache_write: 0, ms: 100 })]))
      .toEqual({ input: 400, output: 50, cache_read: 25, cache_write: 10, ms: 150, calls: 2, cache_hit_rate: 0.0625 })
  })

  it.each(['input', 'output', 'cache_read', 'cache_write'] as const)('propagates null in %s only', field => {
    const total = sumUsage([usage(), usage({ [field]: null })])
    expect(total[field]).toBeNull()
    expect(total.calls).toBe(2)
    for (const other of ['input', 'output', 'cache_read', 'cache_write'] as const) {
      if (other !== field) expect(total[other]).toBe(usage()[other]! * 2)
    }
    expect(total.cache_hit_rate).toBe(field === 'input' || field === 'cache_read' ? null : 0.25)
  })

  it('does not turn a missing value into zero when it occurs first', () => {
    expect(sumUsage([usage({ input: null, cache_read: null }), usage()]))
      .toMatchObject({ input: null, cache_read: null, cache_hit_rate: null })
  })

  it('treats a field that is missing altogether as unknown, never NaN', () => {
    const missing = { ...usage() } as Partial<UsageEvent>
    delete missing.cache_write
    delete missing.input
    const total = sumUsage([usage(), missing as UsageEvent])
    expect(total).toMatchObject({ input: null, cache_write: null, output: 40, cache_read: 50, calls: 2, cache_hit_rate: null })
    expect(Object.values(total).some(value => typeof value === 'number' && Number.isNaN(value))).toBe(false)
    const noMs = { ...usage() } as Partial<UsageEvent>
    delete noMs.ms
    expect(sumUsage([noMs as UsageEvent]).ms).toBe(0)
  })

  it('represents empty totals as zero calls and tokens with an undefined hit rate', () => {
    expect(sumUsage([])).toEqual({ input: 0, output: 0, cache_read: 0, cache_write: 0, ms: 0, calls: 0, cache_hit_rate: null })
    expect(sumUsage([usage({ input: 0, cache_read: 0 })]).cache_hit_rate).toBeNull()
  })
})

describe('TraceTimeline', () => {
  it('renders nothing for no events or only usage events', () => {
    expect(renderToStaticMarkup(<TraceTimeline events={[]} />)).toBe('')
    expect(renderToStaticMarkup(<TraceTimeline events={[usage()]} />)).toBe('')
  })

  it('renders one row per id, with final metadata and no stage heading without stage events', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[tool(), tool({ status: 'ok', ms: 0, size: 0, preview: '[]' })]} />)
    expect(html.match(/<li /g)).toHaveLength(1)
    expect(html).toContain('is-ok')
    expect(html).toContain('0 ms')
    expect(html).toContain('0 characters')        // size is the result's length in characters
    expect(html).not.toContain('bytes')
    expect(html).not.toContain('<h3')
    expect(html).not.toContain('hostv2-trace-running')
  })

  it('uses native keyboard-accessible disclosures, initially closed, with full parameters and preview', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[tool({ preview: 'a\nb\nc' })]} />)
    expect(html).toMatch(/<details><summary class="hostv2-trace-summary">/)
    expect(html).not.toMatch(/<details[^>]+open/)
    expect(html).toContain('hostv2-trace-running')
    expect(html).toContain('time unknown')
    expect(html).toContain('size unknown')
    expect(html).toContain('aria-label="query parameters"')
    expect(html).toContain('{\n  &quot;kind&quot;: &quot;levels&quot;\n}')
    expect(html).toContain('aria-label="query result preview"')
    expect(html).toContain('a\nb\nc')
  })

  it('shows errors outside the collapsed detail and escapes untrusted tool values as text', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[
      tool({ status: 'error', error: '<script>bad()</script>', preview: '<img src="bad">' }),
    ]} />)
    expect(html).toContain('is-error')
    expect(html).toContain('</details><p class="hostv2-trace-error">&lt;script&gt;bad()&lt;/script&gt;</p>')
    expect(html).not.toContain('<script>')
    expect(html).not.toContain('<img')
  })

  it('groups a turn of the three roles by role, each header with its role and model', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[
      { type: 'stage', role: 'analyst', model: 'small-model', status: 'start' },
      usage({ role: 'analyst', model: 'small-model' }),
      { type: 'stage', role: 'analyst', model: 'small-model', status: 'end' },
      { type: 'stage', role: 'planner', model: 'large-model', status: 'start' },
      tool({ id: '1.2.1' }), tool({ id: '1.2.1', status: 'ok' }),
      { type: 'stage', role: 'planner', model: 'large-model', status: 'end' },
      { type: 'stage', role: 'supervisor', model: 'small-model', status: 'start' },
      tool({ id: '2.1.1', role: 'supervisor' }), tool({ id: '2.1.1', role: 'supervisor', status: 'ok' }),
      { type: 'stage', role: 'supervisor', model: 'small-model', status: 'end' },
    ]} />)
    const headers = [...html.matchAll(/<h3 class="hostv2-trace-stage"><span>(\w+)<\/span><span class="hostv2-trace-model">([\w-]+)<\/span>/g)]
      .map(m => [m[1], m[2]])
    expect(headers).toEqual([['analyst', 'small-model'], ['planner', 'large-model'], ['supervisor', 'small-model']])
    const groups = html.split('<div class="hostv2-trace-group">').slice(1)
    expect(groups.map(g => (g.match(/<li /g) ?? []).length)).toEqual([0, 1, 1])
  })

  it('labels stage groups with the role, model and current state', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[
      { type: 'stage', role: 'analyst', model: 'small-model', status: 'start' },
      tool({ role: 'analyst' }),
      { type: 'stage', role: 'analyst', model: 'small-model', status: 'end' },
      { type: 'stage', role: 'planner', model: 'large-model', status: 'start' },
    ]} />)
    expect(html.match(/<h3 /g)).toHaveLength(2)
    for (const text of ['analyst', 'small-model', 'complete', 'planner', 'large-model', 'in progress']) expect(html).toContain(text)
  })
})

describe('TokenPanel', () => {
  it('provides a collapsed summary and two native-disclosure columns with unknown totals', () => {
    const html = renderToStaticMarkup(<TokenPanel turn={[usage({ input: null })]} session={[usage(), usage({ input: null })]} />)
    expect(html).toMatch(/^<details class="hostv2-tokens"><summary>/)
    expect(html).not.toMatch(/<details[^>]+open/)
    expect(html).toContain('This turn: - in / 20 out (1 calls)')
    expect(html).toContain('Session: - in / 40 out (2 calls)')
    expect(html).toContain('aria-label="This turn"')
    expect(html).toContain('aria-label="This session"')
    expect(html).toContain('<td data-label="Input">-</td>')
    expect(html).toContain('<td data-label="Hit rate">-</td>')
    expect(html).not.toContain('scope="col">Role')
  })

  it('renders role rows only for multiple roles, with independent subtotals and a one-decimal rate', () => {
    const html = renderToStaticMarkup(<TokenPanel turn={[usage()]} session={[
      usage(), usage({ role: 'analyst', input: 200, cache_read: 60, cache_write: null }),
    ]} />)
    expect(html.match(/scope="col">Role/g)).toHaveLength(1)
    expect(html).toContain('scope="row">analyst')
    expect(html).toContain('scope="row">planner')
    expect(html).toContain('scope="row">Total')
    expect(html).not.toContain('scope="row">supervisor')
    for (const text of ['25.0%', '30.0%', '28.3%']) expect(html).toContain(text)
    expect(html).toContain('<td data-label="Cache write">-</td>')
  })

  it('one row per role that ran plus the total, even when one role made every call of the turn', () => {
    const analystOnly = [usage({ role: 'analyst', input: 300, cache_read: null })]
    const html = renderToStaticMarkup(<TokenPanel turn={analystOnly} session={[
      ...analystOnly, usage(), usage({ role: 'supervisor', input: 50, output: null }),
    ]} />)
    expect(html.match(/scope="col">Role/g)).toHaveLength(2)            // both columns
    const turn = html.split('aria-label="This session"')[0]
    expect(turn).toContain('scope="row">analyst')
    expect(turn).not.toContain('scope="row">planner')
    const session = html.split('aria-label="This session"')[1]
    for (const role of ['analyst', 'planner', 'supervisor', 'Total']) expect(session).toContain(`scope="row">${role}`)
    expect(session).not.toMatch(/data-label="Output">0</)                // unknown, never 0
    // The one loop (planner only) keeps the plain total.
    expect(renderToStaticMarkup(<TokenPanel turn={[usage()]} session={[usage()]} />)).not.toContain('scope="col">Role')
  })

  it('shows - (unknown), not NaN or 0, when a usage event lacks a field', () => {
    const partial = { ...usage() } as Partial<UsageEvent>
    delete partial.input
    const html = renderToStaticMarkup(<TokenPanel title="Tokens without the bridge" turn={[partial as UsageEvent]} session={[usage(), partial as UsageEvent]} />)
    expect(html).toContain('<strong>Tokens without the bridge</strong>')
    expect(html).toContain('This turn: - in / 20 out (1 calls)')
    expect(html).toContain('<td data-label="Input">-</td>')
    expect(html).not.toMatch(/NaN|Infinity/)
  })

  it('shows actual zero totals for an empty turn and session without NaN or Infinity', () => {
    const html = renderToStaticMarkup(<TokenPanel turn={[]} session={[]} />)
    expect(html).toContain('This turn: 0 in / 0 out (0 calls)')
    expect(html).toContain('Session: 0 in / 0 out (0 calls)')
    expect(html).toContain('<td data-label="Calls">0</td>')
    expect(html).not.toMatch(/NaN|Infinity/)
  })
})

describe('long arguments', () => {
  const marker = { _truncated: true, _chars: 312045, _head: '{"spec": {"task": "' + 'x'.repeat(1981) }

  it('reads the truncation marker and nothing else', () => {
    expect(truncatedArgs(marker)).toEqual({ chars: 312045, head: marker._head })
    expect(truncatedArgs({ kind: 'levels' })).toBeNull()
    expect(truncatedArgs({ _truncated: 'yes', _chars: 1, _head: 'x' })).toBeNull()
    expect(truncatedArgs({ _truncated: true, _chars: '9', _head: 'x' })).toBeNull()
  })

  it('shows truncated arguments as their head with the full size, not as an object', () => {
    const html = renderToStaticMarkup(<TraceTimeline events={[tool({ args: marker, status: 'ok', size: 10 })]} />)
    expect(html).toContain('Parameters (truncated: the first 2,000 of 312,045 characters)')
    expect(html).toContain('aria-label="query parameters, truncated"')
    expect(html).toContain('(312,045 characters)')
    expect(html).not.toContain('_truncated')
    expect(html).not.toContain('_chars')
  })
})

describe('an interrupted turn', () => {
  it('closes what was still open as interrupted, and nothing else', () => {
    const open: TraceEvent[] = [
      { type: 'stage', role: 'planner', model: 'm', status: 'start' },
      tool({ id: 'done' }), tool({ id: 'done', status: 'ok' }),
      tool({ id: 'hung' }),
    ]
    const closed = interruptOpen(open)
    expect(closed.slice(0, 4)).toEqual(open)
    expect(closed.slice(4)).toEqual([
      tool({ id: 'hung', status: 'interrupted' }),
      { type: 'stage', role: 'planner', model: 'm', status: 'interrupted' },
    ])
    const finished: TraceEvent[] = [...open.slice(0, 3), { type: 'stage', role: 'planner', model: 'm', status: 'end' }]
    expect(interruptOpen(finished)).toBe(finished)
    expect(interruptOpen([])).toEqual([])
  })

  it('renders interrupted rows and stages as their own state, neither ok nor error', () => {
    const events = interruptOpen([{ type: 'stage', role: 'planner', model: 'm', status: 'start' }, tool({ id: 'hung' })])
    const groups = groupTraceEvents(events)
    expect(groups).toHaveLength(1)
    expect(groups[0].stage?.status).toBe('interrupted')
    expect(groups[0].tools.map(t => t.status)).toEqual(['interrupted'])
    const html = renderToStaticMarkup(<TraceTimeline events={events} />)
    expect(html).toContain('is-interrupted')
    expect(html).toContain('The turn ended before this call reported back.')
    expect(html).toContain('hostv2-trace-stage-state is-interrupted">interrupted')
    expect(html).not.toContain('in progress')
    expect(html).not.toContain('hostv2-trace-running')
    expect(html).not.toContain('is-error')
  })
})
