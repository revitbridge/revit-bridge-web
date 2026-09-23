import { describe, expect, it, vi } from 'vitest'
import type { ToolParam } from '../../types/api'
import { BridgeApiError } from '../shared/http'
import { confirmAndRun, describePreconditions, specFromForm, typedValue } from './directRun'

const params: ToolParam[] = [
  { name: 'type_name', type: 'string', source: 'tool:family_types', choices_from: 'family_types:OST_StructuralColumns', required: true },
  { name: 'level_name', type: 'string', source: 'tool:levels', choices_from: 'levels', required: true },
  { name: 'x', type: 'double', unit: 'mm', source: 'designer', required: true },
  { name: 'y', type: 'double', unit: 'mm', source: 'designer', required: true },
  { name: 'comment', type: 'string', source: 'default', default: 'none', required: false },
]

const choices = {
  type_name: [{ label: 'UC305x305x97', value: 'UC305x305x97' }],
  level_name: [{ label: 'L1 (0mm)', value: 'L1' }, { label: 'L2 (4000mm)', value: 'L2' }],
}

describe('specFromForm', () => {
  it('binds every filled value with a source the rules accept: picked = tool, typed = answer/designer, default = default', () => {
    const spec = specFromForm({ name: 'create_structural_column', display_name: 'Create Structural Column' }, params,
      { type_name: 'UC305x305x97', level_name: 'Roof', x: '3000', y: '0', comment: 'none' }, choices)
    expect(spec.action).toEqual({ kind: 'run_tool', tool: 'create_structural_column' })
    expect(spec.parameters).toEqual([
      { name: 'type_name', value: 'UC305x305x97', unit: null, source: 'tool', evidence: 'tool:family_types:OST_StructuralColumns' },
      { name: 'level_name', value: 'Roof', unit: null, source: 'answer', evidence: 'capabilities page' },
      { name: 'x', value: 3000, unit: 'mm', source: 'designer', evidence: 'capabilities page' },
      { name: 'y', value: 0, unit: 'mm', source: 'designer', evidence: 'capabilities page' },
      { name: 'comment', value: 'none', unit: null, source: 'default', evidence: 'default:create_structural_column' },
    ])
    expect(spec.task).toContain('Create Structural Column')
    expect(spec.interpretations).toEqual([])
  })

  it('skips empty fields and keeps a non-numeric text for a numeric parameter as typed', () => {
    const spec = specFromForm({ name: 't' }, params, { x: 'abc', y: '' }, {})
    expect(spec.parameters).toEqual([{ name: 'x', value: 'abc', unit: 'mm', source: 'designer', evidence: 'capabilities page' }])
    expect(typedValue({ name: 'b', type: 'bool' }, 'yes')).toBe(true)
    expect(typedValue({ name: 'n', type: 'int' }, '7')).toBe(7)
  })
})

describe('confirmAndRun', () => {
  const spec = specFromForm({ name: 'query_levels' }, [], {}, {})

  it('confirms, then runs with the token and the spec values', async () => {
    const api = {
      confirm: vi.fn().mockResolvedValue({ token: 'tok_1', spec_hash: 'h', expires_at: 'e', card: 'c' }),
      runTool: vi.fn().mockResolvedValue({ success: true, error: null, evidence_id: 'ev_1' }),
    }
    const outcome = await confirmAndRun(api, spec)
    expect(api.confirm).toHaveBeenCalledWith(spec)
    expect(api.runTool).toHaveBeenCalledWith('query_levels', {}, 'tok_1')
    expect(outcome).toEqual({ status: 'executed', token: 'tok_1', expires_at: 'e', result: { success: true, error: null, evidence_id: 'ev_1' } })
  })

  it('a refused confirmation returns the spec errors and never runs', async () => {
    const api = {
      confirm: vi.fn().mockRejectedValue(new BridgeApiError(422, JSON.stringify({ error: 'invalid_spec', errors: [{ code: 'missing_param', param: 'x', message: 'x is not bound' }] }))),
      runTool: vi.fn(),
    }
    expect(await confirmAndRun(api, spec)).toEqual({ status: 'rejected', errors: [{ code: 'missing_param', param: 'x', message: 'x is not bound' }] })
    expect(api.runTool).not.toHaveBeenCalled()
  })

  it('any other failure propagates', async () => {
    const api = { confirm: vi.fn().mockRejectedValue(new Error('503: revit_unreachable')), runTool: vi.fn() }
    await expect(confirmAndRun(api, spec)).rejects.toThrow('503: revit_unreachable')
  })
})

describe('describePreconditions', () => {
  it('reads the three shapes', () => {
    expect(describePreconditions([{ kind: 'levels_min', value: 1 }, { kind: 'category_present', category: 'OST_Walls' }, { text: 'a floor plan is open' }]))
      .toBe('levels >= 1; has OST_Walls; a floor plan is open')
    expect(describePreconditions([])).toBe('-')
  })
})
