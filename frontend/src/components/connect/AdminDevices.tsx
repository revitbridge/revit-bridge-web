/* The admin view: every device this host knows, not only the ones this browser paired. */

import type { DeviceStatus } from '../../types/api'
import { lastSeen } from './format'

interface Props {
  devices: DeviceStatus[] | null
  error: string
  busy: Record<string, string>
  onLoad: () => void
  onRevoke: (deviceId: string) => void
  now: number
}

export default function AdminDevices({ devices, error, busy, onLoad, onRevoke, now }: Props) {
  return (
    <section className="card section">
      <div className="panel-head">
        <h3 className="heading-display section-title">All devices (admin)</h3>
        <button className="btn-secondary" onClick={onLoad}>{devices ? 'Refresh' : 'Load'}</button>
      </div>
      <p className="section-copy small" style={{ marginTop: 0 }}>
        Needs the admin password from the section below. Revoking here closes that add-in's connection and
        invalidates both its token and the browser key that paired it.
      </p>
      {error && <p className="tool-warning-status">{error}</p>}
      {devices && devices.length === 0 && <p className="section-copy small">No device has been paired on this host.</p>}
      {devices && devices.length > 0 && (
        <div className="table-scroll">
          <table className="evidence-table">
            <thead>
              <tr><th>Device</th><th>Label</th><th>State</th><th>Last seen</th><th>Requests</th><th>Add-in</th><th></th></tr>
            </thead>
            <tbody>
              {devices.map(d => (
                <tr key={d.device_id}>
                  <td className="mono">{d.device_id}</td>
                  <td>{d.label || <span className="muted">-</span>}</td>
                  <td>
                    {d.revoked
                      ? <span className="outcome failed">revoked</span>
                      : <span className={`outcome ${d.online ? 'ok' : ''}`}>{d.online ? 'online' : 'offline'}</span>}
                  </td>
                  <td>{lastSeen(d.last_seen, now)}</td>
                  <td>{d.requests ?? 0}</td>
                  <td>{d.addin_version || <span className="muted">-</span>}</td>
                  <td>
                    {!d.revoked && (
                      <button className="btn-ghost danger" onClick={() => onRevoke(d.device_id)} disabled={!!busy[d.device_id]}>
                        {busy[d.device_id] || 'Revoke'}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
