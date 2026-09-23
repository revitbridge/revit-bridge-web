/* Connect: which Revit this host talks to (a paired device or the local add-in),
   which model, admin access. */

import { useCallback, useEffect, useState } from 'react'
import { useStore } from 'zustand'
import { bridgeApi } from '../../api/bridge'
import { getConfig } from '../../config'
import { useSessionStore } from '../../store'
import type { DevicesStatus } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import AdminDevices from '../connect/AdminDevices'
import DeviceList from '../connect/DeviceList'
import PairPanel from '../connect/PairPanel'
import { createPairingFlow, type PairingDeps, type PairingFlow } from '../connect/pairing'

/* One flow for the tab's lifetime, so a pairing survives a visit to another page. */
let sharedFlow: PairingFlow | null = null
function flowFor(deps?: PairingDeps): PairingFlow {
  if (deps) return createPairingFlow(deps)
  sharedFlow ??= createPairingFlow()
  return sharedFlow
}

export default function ConnectPage({ deps }: { deps?: PairingDeps }) {
  const config = getConfig()
  const { deviceId, devices, llmBaseUrl, llmModel, llmKey, setLlm, adminToken, setAdminToken } = useSessionStore()
  const [flow] = useState(() => flowFor(deps))
  const pairing = useStore(flow.store)
  const [label, setLabel] = useState('')
  const [status, setStatus] = useState('Not checked yet')
  const [checking, setChecking] = useState(false)
  const [capacity, setCapacity] = useState<DevicesStatus | null>(null)
  const [units, setUnits] = useState('')
  const [now, setNow] = useState(0)          // "last seen" ages on its own, not on a render

  const refresh = useCallback(async () => {
    flow.refresh()
    try { setCapacity(await bridgeApi.devices()) } catch { /* the relay's capacity is optional */ }
  }, [flow])

  useEffect(() => { refresh() }, [refresh])

  useEffect(() => {
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, 30000)
    return () => { clearTimeout(first); clearInterval(timer) }
  }, [])

  const check = async () => {
    setChecking(true)
    setUnits('')
    try {
      await refresh()
      const h = await bridgeApi.revitHealth()
      if (h.revit_connected) {
        setStatus(`Connected | ${h.mode}${h.latency_ms != null ? ` | ${h.latency_ms} ms` : ''} | ${h.endpoint || ''} | ${h.timestamp}`)
        try {
          const snap = await bridgeApi.snapshot([])
          setUnits(`Project units: ${snap.units.raw || '?'} (${snap.units.length}) | ${snap.document.title}`)
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

  return (
    <div className="page">
      <PairPanel pairing={pairing.pairing} waiting={pairing.waiting} gaveUp={pairing.gaveUp} error={pairing.pairError}
        label={label} onLabelChange={setLabel} onPair={() => flow.pair(label)} onCancel={() => flow.cancelPairing()} />

      <DeviceList devices={devices} statuses={pairing.statuses} busy={pairing.busy} selected={deviceId}
        error={pairing.listError} onSelect={id => flow.select(id)} onRevoke={d => flow.revoke(d)} onRefresh={refresh} now={now} />

      <section className="card section">
        <h3 className="heading-display section-title">Check the connection</h3>
        <div className="flex items-center gap-2 bridge-status-row">
          <button onClick={check} disabled={checking} className="btn-primary">{checking ? 'Checking...' : 'Check'}</button>
          <span className="section-copy small" style={{ margin: 0 }}>
            {deviceId ? `Device ${deviceId}` : 'Local add-in (TCP)'}
            {capacity ? ` | ${capacity.connected} of ${capacity.max_devices} devices connected` : ''}
          </span>
        </div>
        <div className="status-line">{status}</div>
        {units && <div className="status-line">{units}</div>}
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
        <>
          <section className="card section">
            <h3 className="heading-display section-title">Admin</h3>
            <p className="section-copy">Needed to list every device, to add, edit or delete skills and to read interaction logs.</p>
            <input type="password" className="input-field" value={adminToken} onChange={e => setAdminToken(e.target.value)}
              placeholder="Admin password" autoComplete="off" style={{ maxWidth: 320 }} />
          </section>
          {adminToken && (
            <AdminDevices devices={pairing.admin} error={pairing.adminError} busy={pairing.busy}
              onLoad={() => flow.loadAdmin()} onRevoke={id => flow.adminRevoke(id)} now={now} />
          )}
        </>
      )}
    </div>
  )
}
