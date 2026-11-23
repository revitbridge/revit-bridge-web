import type { Role, StageEvent, ToolEvent, TraceEvent, UsageEvent } from '../../../types/hostv2'

/** The latest event wins; a tool keeps its first position in the timeline. */
export function mergeToolEvents(events: readonly TraceEvent[]): ToolEvent[] {
  const tools = new Map<string, ToolEvent>()
  for (const event of events) {
    if (event.type === 'tool') tools.set(event.id, { ...event })
  }
  return [...tools.values()]
}

export interface TraceGroup {
  stage?: StageEvent
  tools: ToolEvent[]
}

/** A completion belongs to the stage in which its tool started. A tool goes to the open
    stage of its own role; when its role has none, to the stage opened last that is still
    open (the one enclosing it), so a role that differs from the stage's never detaches
    the row; with no open stage at all it is ungrouped. */
export function groupTraceEvents(events: readonly TraceEvent[]): TraceGroup[] {
  const tools = new Map(mergeToolEvents(events).map(event => [event.id, event]))
  const seen = new Set<string>()
  const active = new Map<Role, TraceGroup>()
  const groups: TraceGroup[] = []
  let ungrouped: TraceGroup | undefined
  for (const event of events) {
    if (event.type === 'stage') {
      const current = active.get(event.role)
      if (event.status === 'end' && current) {
        current.stage = { ...event }
        active.delete(event.role)
      } else {
        const group: TraceGroup = { stage: { ...event }, tools: [] }
        groups.push(group)
        if (event.status === 'start') {
          active.delete(event.role)          // re-insert: the Map's order is the opening order
          active.set(event.role, group)
        }
      }
      ungrouped = undefined
    } else if (event.type === 'tool' && !seen.has(event.id)) {
      let group = active.get(event.role) ?? [...active.values()].at(-1)
      if (!group) {
        if (!ungrouped) {
          ungrouped = { tools: [] }
          groups.push(ungrouped)
        }
        group = ungrouped
      }
      group.tools.push(tools.get(event.id)!)
      seen.add(event.id)
    }
  }
  return groups
}

type TokenField = 'input' | 'output' | 'cache_read' | 'cache_write'

export interface UsageTotal {
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  ms: number
  calls: number
  cache_hit_rate: number | null
}

const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)

/** Unknown values propagate independently through each token column: a value that is not
    a number - null, or a field missing altogether - makes that column's total unknown. */
export function sumUsage(events: readonly UsageEvent[]): UsageTotal {
  const sum = (field: TokenField): number | null => {
    let total = 0
    for (const event of events) {
      const value: unknown = event[field]
      if (!isCount(value)) return null
      total += value
    }
    return total
  }
  const input = sum('input')
  const cacheRead = sum('cache_read')
  return {
    input,
    output: sum('output'),
    cache_read: cacheRead,
    cache_write: sum('cache_write'),
    ms: events.reduce((total, event) => total + (isCount(event.ms) ? event.ms : 0), 0),
    calls: events.length,
    cache_hit_rate: input === null || input === 0 || cacheRead === null ? null : cacheRead / input,
  }
}
