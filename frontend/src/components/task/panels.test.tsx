/* The spec card and the execution view, rendered from the events' shapes (no browser needed). */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ExecutionResult, HostEvent, TaskSpec } from '../../types/api'
import { ExecutionView } from './ExecutionPanel'
import type { Proposal } from './flow'
import { expiresIn, tokenPrefix } from './format'
import SpecCard from './SpecCard'

const spec: TaskSpec = {
  task: '在 L1 的 (3000, 3000) 放一根结构柱',
  action: { kind: 'run_tool', tool: 'create_structural_column' },
  parameters: [
    { name: 'type_name', value: 'UC305x305x97', source: 'tool', evidence: 'tool:family_types' },
    { name: 'level_name', value: 'L1', source: 'answer', evidence: 'q_level_name' },
    { name: 'x', value: 3000, unit: 'mm', source: 'designer', evidence: '(3000, 3000)' },
    { name: 'y', value: 3000, unit: null, source: 'designer', evidence: '(3000, 3000)' },
  ],
  interpretations: [{ param: 'y', text: 'y 未标单位，按 mm 理解，请确认', confirmed: false }],
  snapshot_fingerprint: 'fp-1',
}

/* A spec event as the flow stores it. */
function proposalFrom(evt: Extract<HostEvent, { type: 'spec' }>): Proposal {
  return { spec: evt.spec, card: evt.card, errors: evt.errors, reconcile: evt.reconcile, reconcileError: evt.reconcile_error ?? null, draft: structuredClone(evt.spec), reconciling: false }
}

const noop = () => {}

function render(p: Proposal, canConfirm: boolean) {
  return renderToStaticMarkup(
    <SpecCard proposal={p} canConfirm={canConfirm} confirming={false} confirmErrors={[]} onToggleInterpretation={noop} onConfirm={noop} />,
  )
}

describe('SpecCard', () => {
  it('shows every parameter with its source and evidence, the interpretation as a checkbox, Confirm dark', () => {
    const html = render(proposalFrom({
      type: 'spec', spec, card: 'Task: ...\nConfirm? (yes / change something)', errors: [],
      reconcile: { conflicts: [], questions: [], interpretations_required: [{ param: 'y', text: 'y 未标单位，按 mm 理解，请确认', confirmed: false }], ready: false },
    }), false)
    for (const text of ['type_name', 'UC305x305x97', 'source-tool', 'tool:family_types', 'level_name', 'source-answer', 'q_level_name', '3000 mm', '(3000, 3000)']) {
      expect(html).toContain(text)
    }
    expect(html).toContain('type="checkbox"')
    expect(html).toContain('y 未标单位，按 mm 理解，请确认')
    expect(html).toMatch(/<button class="btn-primary" disabled="">Confirm<\/button>/)
    expect(html).toContain('not ready')
    expect(html).toContain('fp-1')
  })

  it('paints conflicts red on their rows and lists them; a reconcile error is the reason Confirm stays off', () => {
    const html = render(proposalFrom({
      type: 'spec', spec, card: 'c', errors: [{ code: 'guessed_value', param: 'x', message: 'x must be the designer\'s words' }],
      reconcile: { conflicts: [{ param: 'level_name', claimed: 'L1', kind: 'not_found', available: ['Level 1', 'Level 2'], message: "level_name = 'L1' does not exist in the snapshot" }], questions: [], interpretations_required: [], ready: false },
    }), false)
    expect(html).toContain('class="has-conflict"')
    expect(html).toContain('class="spec-conflict"')
    expect(html).toContain('does not exist in the snapshot')
    expect(html).toContain('available: Level 1, Level 2')
    expect(html).toContain('guessed_value')

    const away = render(proposalFrom({ type: 'spec', spec, card: 'c', errors: [], reconcile: null, reconcile_error: { error: 'revit_unreachable', message: 'connect refused' } }), false)
    expect(away).toContain('Not reconciled: connect refused (revit_unreachable)')
    expect(away).toContain('not reconciled')
  })

  it('lights Confirm when reconcile says ready', () => {
    const html = render(proposalFrom({ type: 'spec', spec, card: 'c', errors: [], reconcile: { conflicts: [], questions: [], interpretations_required: [], ready: true } }), true)
    expect(html).toMatch(/<button class="btn-primary">Confirm<\/button>/)
    expect(html).toContain('data-ready="true"')
  })
})

describe('ExecutionView', () => {
  it('a success lists the validator checks and the evidence id', () => {
    const result: ExecutionResult = {
      success: true, error: null, tool: 'create_structural_column', result: { ElementId: 42 },
      validation: { validator: 'created_ids', passed: true, checks: [{ detail: '1 new element in OST_StructuralColumns', passed: true }] },
      evidence_id: 'ev_1', preconditions_failed: [], warnings: [],
    }
    const html = renderToStaticMarkup(<ExecutionView execution={result} />)
    expect(html).toContain('Executed and validated by created_ids')
    expect(html).toContain('check-passed')
    expect(html).toContain('1 new element in OST_StructuralColumns')
    expect(html).toContain('ev_1')
  })

  it('validation_failed is the loud banner; a gate refusal says nothing reached Revit', () => {
    const failed: ExecutionResult = {
      success: false, error: 'validation_failed', tool: 'create_wall', result: { Status: 'Created' },
      validation: { validator: 'count_delta', passed: false, checks: [{ detail: 'expected +1 OST_Walls, got +0', passed: false }] },
      evidence_id: 'ev_2',
    }
    const html = renderToStaticMarkup(<ExecutionView execution={failed} />)
    expect(html).toContain('execution-banner validation-failed')
    expect(html).toContain('VALIDATION FAILED')
    expect(html).toContain('check-failed')

    const refused: ExecutionResult = { success: false, error: 'confirmation_invalid', reason: 'mismatch', message: 'the projection differs' }
    const refusal = renderToStaticMarkup(<ExecutionView execution={refused} tampered />)
    expect(refusal).toContain('execution-banner refused')
    expect(refusal).toContain('confirmation_invalid')
    expect(refusal).toContain('(mismatch)')
    expect(refusal).toContain('Nothing reached Revit; the token is not consumed.')
  })
})

describe('format', () => {
  it('shows the token prefix only, and the time left', () => {
    expect(tokenPrefix('tok_abcdef0123456789')).toBe('tok_ab...')
    const now = Date.parse('2026-09-22T09:00:00Z')
    expect(expiresIn('2026-09-22T09:09:30Z', now)).toBe('in 9 min 30 s')
    expect(expiresIn('2026-09-22T09:00:20Z', now)).toBe('in 20 s')
    expect(expiresIn('2026-09-22T08:59:59Z', now)).toBe('expired')
    expect(expiresIn('soon', now)).toBe('soon')
  })
})
