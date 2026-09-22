/* Panel 6: the v1 pack draft from a confirmed execute_code spec that worked.

   Every spec parameter becomes a pack parameter with a source the v1 schema
   accepts (designer | tool:<query> | answer | default); the designer edits the
   draft before POST /solidify, and the server's problems come back line by line. */

import type { PackParameter, SolidifyRequest, TaskSpec } from '../../types/api'

const USABLE_QUERY = /^(levels|floor_types|family_types:OST_[A-Za-z]+|elements:OST_[A-Za-z]+)$/
const UNITS = ['mm', 'm', 'feet']
const IDENTIFIER = /^[A-Za-z_]\w*$/

export function packType(value: unknown): string {
  if (typeof value === 'number') return 'double'
  if (typeof value === 'boolean') return 'bool'
  return 'string'
}

/* A pack name from the task sentence: ascii words, snake_case, at most 40 chars. */
export function suggestName(task: string): string {
  const words = task.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(/\s+/).filter(Boolean)
  const name = words.join('_').slice(0, 40).replace(/_+$/, '')
  return IDENTIFIER.test(name) ? name : 'my_pack'
}

export function draftParameter(p: TaskSpec['parameters'][number]): PackParameter {
  const draft: PackParameter = { name: p.name, type: packType(p.value), description: p.name, source: 'designer', required: true }
  if (p.unit && UNITS.includes(p.unit)) draft.unit = p.unit
  switch (p.source) {
    case 'tool': {
      // evidence is "tool:<query>": usable as choices when the query is one the schema knows
      const query = p.evidence.startsWith('tool:') ? p.evidence.slice(5) : ''
      if (USABLE_QUERY.test(query)) {
        draft.source = `tool:${query}`
        draft.choices_from = query
      } else {
        draft.source = 'answer'
        draft.description = `${p.name} (from ${p.evidence})`
      }
      break
    }
    case 'answer':
      draft.source = 'answer'
      break
    case 'default':
      draft.source = 'default'
      draft.required = false
      draft.default = p.value
      break
    default:
      draft.source = 'designer'     // designer and preference: the designer's own value
  }
  return draft
}

export function draftPack(spec: TaskSpec): SolidifyRequest {
  return {
    name: suggestName(spec.task),
    code: spec.action.code ?? '',
    description: spec.task,
    parameters: spec.parameters.map(draftParameter),
    source_query: spec.task,
    validator: null,
  }
}
