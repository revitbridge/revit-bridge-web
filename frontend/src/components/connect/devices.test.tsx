/* The device rows and the admin table render what the server says about a device. */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { DeviceStatus } from '../../types/api'
import AdminDevices from './AdminDevices'
import DeviceList from './DeviceList'
import PairPanel from './PairPanel'

const NOW = Date.parse('2026-09-23T09:00:00Z')
const noop = () => {}

const device = { device_id: 'dev_a1b2c3d4e5f6', browser_key: 'bk_secret_value', label: 'studio-01' }
const online: DeviceStatus = { device_id: device.device_id, label: 'studio-01', online: true, last_seen: '2026-09-23T08:59:40Z', requests: 7, revoked: false }
const offline: DeviceStatus = { ...online, online: false, last_seen: '2026-09-23T08:30:00Z', requests: 2 }

function list(statuses: Record<string, DeviceStatus>, selected: string, busy: Record<string, string> = {}) {
  return renderToStaticMarkup(
    <DeviceList devices={[device]} statuses={statuses} busy={busy} selected={selected} error=""
      onSelect={noop} onRevoke={noop} onRefresh={noop} now={NOW} />,
  )
}

describe('DeviceList', () => {
  it('shows a connected device with its label, activity and request count, and never its key', () => {
    const html = list({ [device.device_id]: online }, device.device_id)
    expect(html).toContain('studio-01')
    expect(html).toContain('status-dot online')
    expect(html).toContain('>online<')
    expect(html).toContain('last seen just now')
    expect(html).toContain('7 requests')
    expect(html).toContain(device.device_id)
    expect(html).toContain('device-row is-selected')
    expect(html).not.toContain(device.browser_key)
  })

  it('marks an offline device and keeps the local add-in as the other choice', () => {
    const html = list({ [device.device_id]: offline }, '')
    expect(html).toContain('status-dot offline')
    expect(html).toContain('last seen 30 min ago')
    expect(html).toContain('Local add-in (TCP)')
    expect(html).toContain('device-row is-selected')     // the local row is the selected one
  })

  it('says unknown while nothing has been asked, and shows what a revoke is doing', () => {
    const html = list({}, '', { [device.device_id]: 'Revoking...' })
    expect(html).toContain('unknown')
    expect(html).toContain('Revoking...')
    expect(html).toContain('disabled')
  })
})

describe('PairPanel', () => {
  it('shows the code, the install command and both ways to use it', () => {
    const html = renderToStaticMarkup(
      <PairPanel pairing={{ code: 'K7QD-2M9X', device_id: 'dev_x', expires_at: '2026-09-23T09:10:00Z', browser_key: 'bk', install_command: 'irm https://host/install.ps1 -Pair K7QD-2M9X' }}
        waiting gaveUp={false} error="" label="" onLabelChange={noop} onPair={noop} onCancel={noop} />,
    )
    expect(html).toContain('K7QD-2M9X')
    expect(html).toContain('irm https://host/install.ps1 -Pair K7QD-2M9X')
    expect(html).toContain('settings window')
    expect(html).toContain('Waiting for the add-in to connect...')
    expect(html).toContain('status-dot waiting')
    expect(html).not.toContain('>bk<')
  })

  it('tells the designer what to do when nobody redeemed the code', () => {
    const html = renderToStaticMarkup(
      <PairPanel pairing={null} waiting={false} gaveUp error="" label="" onLabelChange={noop} onPair={noop} onCancel={noop} />,
    )
    expect(html).toContain('No add-in redeemed that code within ten minutes.')
    expect(html).toContain('Pair a Revit')
  })
})

describe('AdminDevices', () => {
  it('lists every device with its state, and offers Revoke only for the live ones', () => {
    const revoked: DeviceStatus = { device_id: 'dev_old', label: 'old', online: false, last_seen: '2026-09-22T09:00:00Z', requests: 0, revoked: true }
    const html = renderToStaticMarkup(
      <AdminDevices devices={[{ ...online, addin_version: '0.2.0' }, revoked]} error="" busy={{}} onLoad={noop} onRevoke={noop} now={NOW} />,
    )
    expect(html).toContain('dev_a1b2c3d4e5f6')
    expect(html).toContain('0.2.0')
    expect(html).toContain('>online<')
    expect(html).toContain('>revoked<')
    expect(html.match(/>Revoke</g)).toHaveLength(1)        // not for the revoked one
  })

  it('shows why the list could not be read', () => {
    const html = renderToStaticMarkup(
      <AdminDevices devices={null} error="admin_required" busy={{}} onLoad={noop} onRevoke={noop} now={NOW} />,
    )
    expect(html).toContain('admin_required')
    expect(html).toContain('>Load<')
  })
})
