import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { TraceEvent, ToolEvent, UsageEvent } from '../../../types/hostv2'
import { groupTraceEvents, mergeToolEvents, sumUsage } from './events'
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
    expect(groups.map(group => group.tools.map(event => event.id)))
      .toEqual([['before'], ['tool-1'], ['during'], ['after'], ['last']])
    expect(groups[0].stage).toBeUndefined()
    expect(groups[3].stage).toBeUndefined()
    expect(groups[4].stage?.model).toBe('other')
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
    expect(html).toContain('0 bytes')
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
    expect(html).toContain('This turn: unknown in / 20 out (1 calls)')
    expect(html).toContain('Session: unknown in / 40 out (2 calls)')
    expect(html).toContain('aria-label="This turn"')
    expect(html).toContain('aria-label="This session"')
    expect(html).toContain('<td data-label="Input">unknown</td>')
    expect(html).toContain('<td data-label="Hit rate">unknown</td>')
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
    expect(html).toContain('<td data-label="Cache write">unknown</td>')
  })

  it('shows actual zero totals for an empty turn and session without NaN or Infinity', () => {
    const html = renderToStaticMarkup(<TokenPanel turn={[]} session={[]} />)
    expect(html).toContain('This turn: 0 in / 0 out (0 calls)')
    expect(html).toContain('Session: 0 in / 0 out (0 calls)')
    expect(html).toContain('<td data-label="Calls">0</td>')
    expect(html).not.toMatch(/NaN|Infinity/)
  })
})
