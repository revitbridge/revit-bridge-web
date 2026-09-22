import { describe, expect, it } from 'vitest'
import type { TaskSpec } from '../../types/api'
import { draftPack, draftParameter, suggestName } from './solidifyDraft'

const spec: TaskSpec = {
  task: 'Create a 200 mm wall on L1 from the selection',
  action: { kind: 'execute_code', code: 'return 1;', code_parameters: [] },
  parameters: [
    { name: 'level_name', value: 'L1', source: 'tool', evidence: 'tool:levels' },
    { name: 'type_name', value: 'Basic Wall', source: 'tool', evidence: 'tool:family_types' },
    { name: 'thickness', value: 200, unit: 'mm', source: 'designer', evidence: '200 mm' },
    { name: 'height', value: 3000, unit: 'mm', source: 'default', evidence: 'default:create_wall' },
    { name: 'structural', value: true, source: 'preference', evidence: 'preference:walls' },
    { name: 'count', value: 2, source: 'answer', evidence: 'q_count' },
  ],
}

describe('the solidify draft', () => {
  it('maps every spec parameter to a v1 pack parameter the schema accepts', () => {
    const params = spec.parameters.map(draftParameter)
    expect(params).toEqual([
      { name: 'level_name', type: 'string', description: 'level_name', source: 'tool:levels', required: true, choices_from: 'levels' },
      { name: 'type_name', type: 'string', description: 'type_name (from tool:family_types)', source: 'answer', required: true },
      { name: 'thickness', type: 'double', description: 'thickness', source: 'designer', required: true, unit: 'mm' },
      { name: 'height', type: 'double', description: 'height', source: 'default', required: false, unit: 'mm', default: 3000 },
      { name: 'structural', type: 'bool', description: 'structural', source: 'designer', required: true },
      { name: 'count', type: 'double', description: 'count', source: 'answer', required: true },
    ])
  })

  it('names the pack from the task and keeps the code and the sentence', () => {
    const draft = draftPack(spec)
    expect(draft.name).toBe('create_a_200_mm_wall_on_l1_from_the_sele')
    expect(draft.code).toBe('return 1;')
    expect(draft.description).toBe(spec.task)
    expect(draft.source_query).toBe(spec.task)
    expect(draft.validator).toBeNull()
    expect(draft.parameters).toHaveLength(6)
  })

  it('falls back to an identifier when the task has no ascii words', () => {
    expect(suggestName('在坐标放一根结构柱')).toBe('my_pack')
    expect(suggestName('  Place   column!  ')).toBe('place_column')
  })
})
