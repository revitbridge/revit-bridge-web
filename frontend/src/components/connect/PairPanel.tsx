/* Step 1 of the three-step flow: a pairing code, the install command that carries
   it, and the same code typed into the add-in's settings window. */

import { useEffect, useState } from 'react'
import type { PairingResponse } from '../../types/api'
import { timeLeft } from './format'

interface Props {
  pairing: PairingResponse | null
  waiting: boolean
  gaveUp: boolean
  error: string
  label: string
  onLabelChange: (label: string) => void
  onPair: () => void
  onCancel: () => void
}

export default function PairPanel({ pairing, waiting, gaveUp, error, label, onLabelChange, onPair, onCancel }: Props) {
  // the countdown's clock: only an interval writes it, so no render depends on Date.now()
  const [now, setNow] = useState<number | null>(null)
  useEffect(() => {
    if (!pairing) return
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, 1000)
    return () => { clearTimeout(first); clearInterval(timer) }
  }, [pairing])

  const clock = pairing === null ? null : now
  const expired = pairing !== null && clock !== null && Date.parse(pairing.expires_at) - clock <= 0

  return (
    <section className="card section">
      <h3 className="heading-display section-title">Pair a Revit</h3>
      <p className="section-copy">
        A <strong>device</strong> is one add-in installation on one machine. Pairing gives you a code that is
        good for ten minutes: the designer runs the install command below (it carries the code) or types the
        code into the add-in's settings window. The add-in keeps its own token, this browser keeps the key
        that drives and revokes the device - neither can stand in for the other.
      </p>

      {!pairing && (
        <div className="pair-row">
          <input className="input-field" value={label} onChange={e => onLabelChange(e.target.value)}
            placeholder="Name this device (e.g. studio-01)" aria-label="Device label" style={{ maxWidth: 280 }}
            onKeyDown={e => e.key === 'Enter' && onPair()} />
          <button className="btn-primary" onClick={onPair} disabled={waiting}>Pair a Revit</button>
        </div>
      )}

      {error && <p className="tool-warning-status">{error}</p>}
      {gaveUp && !pairing && (
        <p className="tool-warning-status">
          No add-in redeemed that code within ten minutes. Pair again and run the install command on the
          designer's machine, with Revit closed.
        </p>
      )}

      {pairing && (
        <div className="pairing-live">
          <div className="pairing-code-row">
            <code className="pairing-code">{pairing.code}</code>
            <span className={`countdown${expired ? ' is-expired' : ''}`}>
              {clock === null ? 'expires in ten minutes' : expired ? 'expired: pair again' : `expires in ${timeLeft(pairing.expires_at, clock)}`}
            </span>
            <span className="muted small-mono">device {pairing.device_id}</span>
          </div>

          <p className="section-copy small" style={{ marginTop: 12 }}>
            <strong>Either</strong> run this in PowerShell on the designer's machine, with Revit closed:
          </p>
          <pre className="command-block">{pairing.install_command}</pre>
          <p className="section-copy small">
            <strong>or</strong>, if the add-in is already installed, open its settings window in Revit and type
            the code above.
          </p>

          <div className="flex gap-2 items-center flex-wrap" style={{ marginTop: 10 }}>
            <span className="status-dot waiting" aria-hidden="true" />
            <span className="small-mono">{waiting ? 'Waiting for the add-in to connect...' : 'Not waiting any more'}</span>
            <button className="btn-ghost" onClick={onCancel}>Stop waiting</button>
          </div>
        </div>
      )}
    </section>
  )
}
