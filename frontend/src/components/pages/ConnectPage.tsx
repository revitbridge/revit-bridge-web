/* Connect: which Revit (local TCP or a remote slot), which model, admin access. */

import { useCallback, useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { addinWsEndpoint, getConfig } from '../../config'
import { useSessionStore } from '../../store'
import type { SlotsStatus } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'

export default function ConnectPage() {
  const config = getConfig()
  const { slot, slotToken, setSlot, setSlotToken, llmBaseUrl, llmModel, llmKey, setLlm, adminToken, setAdminToken } = useSessionStore()
  const [status, setStatus] = useState('Not checked yet')
  const [checking, setChecking] = useState(false)
  const [slots, setSlots] = useState<SlotsStatus | null>(null)
  const [units, setUnits] = useState('')

  const refreshSlots = useCallback(async () => {
    try { setSlots(await bridgeApi.slots()) } catch { /* relay status is optional */ }
  }, [])

  useEffect(() => { refreshSlots() }, [refreshSlots])

  const check = async () => {
    if (slot && config.features.slotTokenRequired && !slotToken) {
      setStatus('This host requires a slot token: enter it before connecting.')
      return
    }
    setChecking(true)
    setUnits('')
    try {
      await refreshSlots()
      const h = await bridgeApi.revitHealth()
      if (h.revit_connected) {
        setStatus(`Connected | ${h.mode}${h.latency_ms != null ? ` | ${h.latency_ms} ms` : ''} | ${h.endpoint || ''} | ${h.timestamp}`)
        try {
          const u = await bridgeApi.projectUnits()
          setUnits(u.error ? `Project units: ${u.error}` : `Project units: ${u.display_name} (${u.detected})`)
        } catch (e: unknown) {
          setUnits(`Project units: ${getErrorMessage(e)}`)
        }
      } else {
        setStatus(`Disconnected | ${h.detail}`)
      }
    } catch (e: unknown) {
      setStatus(`Connection failed: ${getErrorMessage(e)}`)
    } finally {
      setChecking(false)
    }
  }

  const wsEndpoint = addinWsEndpoint()
  const installCommand = `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/revitbridge/revit-bridge-addin/main/installer/install.ps1))) -Mode remote -Server ${wsEndpoint}${slot ? ` -Slot ${slot}` : ''}`

  return (
    <div className="page">
      <section className="card section">
        <h3 className="heading-display section-title">Revit</h3>
        <p className="section-copy">
          Pick the Revit this host should talk to. <strong>Local</strong> uses the add-in's TCP port on the machine
          running this server; a <strong>slot</strong> is a remote add-in connected through the WebSocket relay.
        </p>
        <div className="flex items-center gap-2 bridge-status-row">
          <select className="input-field" value={slot} onChange={e => setSlot(e.target.value)} style={{ maxWidth: 220 }}>
            <option value="">Local add-in (TCP)</option>
            {Array.from({ length: slots?.max_slots ?? config.features.maxSlots }, (_, i) => {
              const sid = String(i + 1)
              const info = slots?.slots?.[sid]
              const connected = info?.status === 'connected'
              return (
                <option key={sid} value={sid}>
                  Slot {sid} {connected ? `● online (${info?.requests ?? 0} req)` : '○ free'}
                </option>
              )
            })}
          </select>
          {slot && (
            <input
              type="password"
              className="input-field"
              value={slotToken}
              onChange={e => setSlotToken(e.target.value.trim())}
              placeholder={config.features.slotTokenRequired ? 'Slot token (required)' : 'Slot token (if configured)'}
              autoComplete="off"
              aria-label="Revit slot token"
              style={{ maxWidth: 260 }}
            />
          )}
          <button onClick={check} disabled={checking} className="btn-primary">{checking ? 'Checking...' : 'Connect'}</button>
          <button onClick={refreshSlots} className="btn-secondary">Refresh slots</button>
        </div>
        <div className="status-line">{status}</div>
        {units && <div className="status-line">{units}</div>}
      </section>

      <section className="card section">
        <h3 className="heading-display section-title">Add-in for a remote Revit</h3>
        <p className="section-copy">
          On the designer's machine (Revit closed), install the add-in in remote mode pointing at this host.
          The add-in then appears as a slot above.
        </p>
        <pre className="command-block">{installCommand}</pre>
        <p className="section-copy small">
          Relay endpoint: <code>{wsEndpoint}/&lt;slot&gt;</code>. Without <code>-Slot</code> the installer uses slot 1.
          Local mode needs no server: <code>irm https://raw.githubusercontent.com/revitbridge/revit-bridge-addin/main/installer/install.ps1 | iex</code>
        </p>
      </section>

      <section className="card section">
        <h3 className="heading-display section-title">Model</h3>
        <p className="section-copy">
          Bring your own model: any OpenAI-compatible endpoint. These values stay in this browser tab
          (<code>sessionStorage</code>) and travel as request headers; the server never stores them.
          {config.features.serverModel
            ? ' Leave a field empty to use the server default for it.'
            : ' This host has no server-side model configured, so a key is required.'}
        </p>
        <div className="form-grid">
          <label>
            <span className="label-text">Base URL</span>
            <input className="input-field" value={llmBaseUrl} onChange={e => setLlm({ llmBaseUrl: e.target.value.trim() })}
              placeholder="https://openrouter.ai/api/v1" autoComplete="off" />
          </label>
          <label>
            <span className="label-text">Model</span>
            <input className="input-field" value={llmModel} onChange={e => setLlm({ llmModel: e.target.value.trim() })}
              placeholder="anthropic/claude-sonnet-4.6" autoComplete="off" />
          </label>
          <label className="form-wide">
            <span className="label-text">API key</span>
            <input type="password" className="input-field" value={llmKey} onChange={e => setLlm({ llmKey: e.target.value.trim() })}
              placeholder={config.features.serverModel ? 'Optional: server default is used when empty' : 'Required on this host'} autoComplete="off" />
          </label>
        </div>
        <button className="btn-ghost" onClick={() => setLlm({ llmBaseUrl: '', llmModel: '', llmKey: '' })}>Clear model settings</button>
      </section>

      {config.features.admin && (
        <section className="card section">
          <h3 className="heading-display section-title">Admin</h3>
          <p className="section-copy">Needed to add, edit or delete skills and to read interaction logs.</p>
          <input type="password" className="input-field" value={adminToken} onChange={e => setAdminToken(e.target.value)}
            placeholder="Admin password" autoComplete="off" style={{ maxWidth: 320 }} />
        </section>
      )}
    </div>
  )
}
