/* The two new page shells render: the task sequence's panels and the evidence table. */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { HostEvent } from '../../types/api'
import EvidencePage from './EvidencePage'
import TaskPage from './TaskPage'

async function* noEvents(): AsyncGenerator<HostEvent> {}

const api = {
  evidence: async () => [],
  validateEvidence: async () => ({ evidence_id: 'x', tool: 't', validator: 'created_ids', passed: true, checks: [] }),
}

describe('the pages', () => {
  it('the task page lays out the sequence: snapshot, brief, the no-bridge switch', () => {
    const html = renderToStaticMarkup(<TaskPage events={noEvents} />)
    expect(html).toContain('1. Snapshot')
    expect(html).toContain('2. Brief')
    expect(html).toContain('3. Compare with no bridge')
    expect(html).toContain('With the bridge')
    expect(html).toContain('bridge=false')
    // nothing proposed yet: no card, no token panel, no solidify
    expect(html).not.toContain('4. Spec card')
    expect(html).not.toContain('5. Confirm and run')
    expect(html).not.toContain('6. Solidify')
  })

  it('the evidence page renders its empty state', () => {
    const html = renderToStaticMarkup(<EvidencePage api={api} />)
    expect(html).toContain('Evidence')
    expect(html).toContain('filter by pack name')
    expect(html).toContain('No executions recorded yet.')
  })
})
