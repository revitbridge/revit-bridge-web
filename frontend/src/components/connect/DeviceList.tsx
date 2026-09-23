/* My devices: the ones this browser holds keys for. Selecting one sends its
   headers with every request from every page; Revoke ends it everywhere. */

import type { DeviceStatus } from '../../types/api'
import type { PairedDevice } from '../../store'
import { lastSeen } from './format'

interface Props {
  devices: PairedDevice[]
  statuses: Record<string, DeviceStatus>
  busy: Record<string, string>
  selected: string
  error: string
  onSelect: (deviceId: string) => void
  onRevoke: (device: PairedDevice) => void
  onRefresh: () => void
  now: number            // the clock the page ticks, so the rows stay pure
}

export default function DeviceList({ devices, statuses, busy, selected, error, onSelect, onRevoke, onRefresh, now }: Props) {
  return (
    <section className="card section">
      <div className="panel-head">
        <h3 className="heading-display section-title">My devices</h3>
        <button className="btn-secondary" onClick={onRefresh}>Refresh</button>
      </div>
      <p className="section-copy small" style={{ marginTop: 0 }}>
        The Revit this host talks to. <strong>Local add-in</strong> is the one on the machine running this
        server, over TCP; a device is a paired add-in connected through the relay. Everything the other pages
        do - snapshots, queries, confirmed executions, evidence - happens on the one selected here.
      </p>

      {error && <p className="tool-warning-status">{error}</p>}

      <div className="device-list">
        <button className={`device-row${selected === '' ? ' is-selected' : ''}`} onClick={() => onSelect('')}>
          <span className="device-name">Local add-in (TCP)</span>
          <span className="muted small-mono">this machine, no pairing</span>
        </button>

        {devices.length === 0 && <p className="section-copy small">No paired device in this browser tab yet.</p>}

        {devices.map(d => {
          const status = statuses[d.device_id]
          const online = status?.online === true
          return (
            <div key={d.device_id} className={`device-row${selected === d.device_id ? ' is-selected' : ''}`}>
              <button className="device-pick" onClick={() => onSelect(d.device_id)}>
                <span className="device-name">{status?.label || d.label || d.device_id}</span>
                <span className={`status-dot ${online ? 'online' : 'offline'}`} aria-hidden="true" />
                <span className="small-mono">{status ? (online ? 'online' : 'offline') : 'unknown'}</span>
                <span className="muted small-mono">last seen {lastSeen(status?.last_seen, now)}</span>
                <span className="muted small-mono">{status?.requests ?? 0} requests</span>
                <span className="muted small-mono">{d.device_id}</span>
              </button>
              <button className="btn-ghost danger" onClick={() => onRevoke(d)} disabled={!!busy[d.device_id]}>
                {busy[d.device_id] || 'Revoke'}
              </button>
            </div>
          )
        })}
      </div>
    </section>
  )
}
