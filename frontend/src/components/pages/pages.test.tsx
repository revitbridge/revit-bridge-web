/* The page shells render: the Connect page's devices, the task sequence's panels,
   the evidence table. */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DeviceStatus, HostEvent } from '../../types/api'
import ConnectPage from './ConnectPage'
import EvidencePage from './EvidencePage'
import TaskPage from './TaskPage'

async function* noEvents(): AsyncGenerator<HostEvent> {}

const api = {
  evidence: async () => [],
  validateEvidence: async () => ({ evidence_id: 'x', tool: 't', validator: 'created_ids', passed: true, checks: [] }),
}

const online: DeviceStatus = { device_id: 'dev_a1b2c3', label: 'studio-01', online: true, last_seen: '2026-09-23T09:00:00Z', requests: 7 }

const deviceApi = {
  pair: async () => ({ code: 'K7QD-2M9X', device_id: 'dev_a1b2c3', expires_at: '2026-09-23T09:10:00Z', browser_key: 'bk', install_command: 'irm https://host/install.ps1' }),
  status: async () => online,
  revoke: async () => ({ status: 'revoked' }),
  all: async () => [online],
  capacity: async () => ({ max_devices: 20, connected: 1 }),
}

describe('the pages', () => {
  it('the connect page offers pairing, the local add-in and the model settings', () => {
    const html = renderToStaticMarkup(<ConnectPage deps={{ api: deviceApi, wait: async () => {}, now: () => 0 }} />)
    expect(html).toContain('Pair a Revit')
    expect(html).toContain('settings window')
    expect(html).toContain('My devices')
    expect(html).toContain('Local add-in (TCP)')
    expect(html).toContain('No paired device in this browser tab yet.')
    expect(html).toContain('Check the connection')
    expect(html).toContain('Bring your own model')
    // nothing about slots or slot tokens survives
    expect(html.toLowerCase()).not.toContain('slot')
  })

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
