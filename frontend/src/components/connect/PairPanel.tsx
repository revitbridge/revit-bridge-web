/* Step 1 of the three-step flow: a pairing code, the install command that carries
   it, and the same code typed into the add-in's settings window. */

import Snippet from '../shared/Snippet'
import { uiText } from '../../utils/strings'
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
      <h3 className="heading-display section-title">{uiText("Pair a Revit")}</h3>
      <p className="section-copy">{uiText("A")} <strong>{uiText("device")}</strong> {uiText("is one add-in installation on one machine. Pairing gives you a code that is good for ten minutes: the designer runs the install command below (it carries the code) or types the code into the add-in's settings window. The add-in keeps its own token, this browser keeps the key that drives and revokes the device - neither can stand in for the other.")}</p>

      {!pairing && (
        <div className="pair-row">
          <input className="input-field" value={label} onChange={e => onLabelChange(e.target.value)}
            placeholder={uiText("Name this device (e.g. studio-01)")} aria-label={uiText("Device label")} style={{ maxWidth: 280 }}
            onKeyDown={e => e.key === 'Enter' && onPair()} />
          <button className="btn-primary" onClick={onPair} disabled={waiting}>{uiText("Pair a Revit")}</button>
        </div>
      )}

      {error && <p className="tool-warning-status">{error}</p>}
      {gaveUp && !pairing && (
        <p className="tool-warning-status">{uiText("No add-in redeemed that code within ten minutes. Pair again and run the install command on the designer's machine, with Revit closed.")}</p>
      )}

      {pairing && (
        <div className="pairing-live">
          <div className="pairing-code-row">
            <code className="pairing-code">{pairing.code}</code>
            <span className={`countdown${expired ? ' is-expired' : ''}`}>
              {clock === null ? uiText("expires in ten minutes") : expired ? uiText("expired: pair again") : uiText("expires in {0}", timeLeft(pairing.expires_at, clock))}
            </span>
            <span className="muted small-mono">{uiText("device")} {pairing.device_id}</span>
          </div>

          <p className="section-copy small" style={{ marginTop: 12 }}>
            <strong>{uiText("Either")}</strong> {uiText("run this in PowerShell on the designer's machine, with Revit closed:")}</p>
          <Snippet text={pairing.install_command} />
          <p className="section-copy small">
            <strong>{uiText("or")}</strong>{uiText(", if the add-in is already installed, open its settings window in Revit and type the code above.")}</p>

          <div className="flex gap-2 items-center flex-wrap" style={{ marginTop: 10 }}>
            <span className="status-dot waiting" aria-hidden="true" />
            <span className="small-mono">{waiting ? uiText("Waiting for the add-in to connect...") : uiText("Not waiting any more")}</span>
            <button className="btn-ghost" onClick={onCancel}>{uiText("Stop waiting")}</button>
          </div>
        </div>
      )}
    </section>
  )
}
