/* Connect: which Revit this host talks to (a paired device or the local add-in),
   which model (each role's, read-only, and this browser's own settings), admin access. A local host (features.mode) has no pairing, no
   devices and no admin password: its page is the local add-in's status and the
   model settings only; the pairing and device components render on a cloud host. */

import { uiText } from '../../utils/strings'
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
import RoleModels from '../connect/RoleModels'
import { createPairingFlow, type PairingDeps, type PairingFlow } from '../connect/pairing'

/* One flow for the tab's lifetime, so a pairing survives a visit to another page. */
let sharedFlow: PairingFlow | null = null
function flowFor(deps?: PairingDeps): PairingFlow {
  if (deps) return createPairingFlow(deps)
  sharedFlow ??= createPairingFlow()
  return sharedFlow
}

export default function ConnectPage({ deps }: { deps?: PairingDeps }) {
  return getConfig().features.mode === 'local' ? <LocalConnectPage /> : <CloudConnectPage deps={deps} />
}

/* "Check": the health of the Revit this tab drives, then the project's units. */
function useRevitCheck(before?: () => Promise<void>) {
  const [status, setStatus] = useState('Not checked yet')
  const [checking, setChecking] = useState(false)
  const [units, setUnits] = useState('')

  const check = async () => {
    setChecking(true)
    setUnits('')
    try {
      if (before) await before()
      const h = await bridgeApi.revitHealth()
      if (h.revit_connected) {
        setStatus(uiText("Connected | {0}{1} | {2} | {3}", h.mode, h.latency_ms != null ? ` | ${h.latency_ms} ms` : '', h.endpoint || '', h.timestamp))
        try {
          const snap = await bridgeApi.snapshot([])
          setUnits(uiText("Project units: {0} ({1}) | {2}", snap.units.raw || '?', snap.units.length, snap.document.title))
        } catch (e: unknown) {
          setUnits(uiText("Project units: {0}", getErrorMessage(e)))
        }
      } else {
        setStatus(uiText("Disconnected | {0}", h.detail))
      }
    } catch (e: unknown) {
      setStatus(uiText("Connection failed: {0}", getErrorMessage(e)))
    } finally {
      setChecking(false)
    }
  }

  return { status, checking, units, check }
}

function LocalConnectPage() {
  const revit = useRevitCheck()

  return (
    <div className="page">
      <section className="card section">
        <h3 className="heading-display section-title">{uiText("Check the connection")}</h3>
        <p className="section-copy">{uiText("This host runs in local mode: it talks to the Revit add-in on this machine over TCP and is reachable from this machine only, so there is nothing to pair and no admin password.")}</p>
        <div className="flex items-center gap-2 bridge-status-row">
          <button onClick={revit.check} disabled={revit.checking} className="btn-primary">{revit.checking ? uiText("Checking...") : uiText("Check")}</button>
          <span className="section-copy small" style={{ margin: 0 }}>{uiText("Local add-in (TCP)")}</span>
        </div>
        <div className="status-line">{uiText(revit.status)}</div>
        {revit.units && <div className="status-line">{revit.units}</div>}
      </section>

      <HostModels />

    </div>
  )
}

function CloudConnectPage({ deps }: { deps?: PairingDeps }) {
  const config = getConfig()
  const { deviceId, devices, adminToken, setAdminToken } = useSessionStore()
  const [flow] = useState(() => flowFor(deps))
  const pairing = useStore(flow.store)
  const [label, setLabel] = useState('')
  const [capacity, setCapacity] = useState<DevicesStatus | null>(null)
  const [now, setNow] = useState(0)          // "last seen" ages on its own, not on a render

  const refresh = useCallback(async () => {
    flow.refresh()
    try { setCapacity(await bridgeApi.devices()) } catch { /* the relay's capacity is optional */ }
  }, [flow])
  const revit = useRevitCheck(refresh)

  // On mount: what refresh() does, with the capacity set from the answer's callback.
  useEffect(() => {
    flow.refresh()
    bridgeApi.devices().then(setCapacity, () => { /* the relay's capacity is optional */ })
  }, [flow])

  useEffect(() => {
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, 30000)
    return () => { clearTimeout(first); clearInterval(timer) }
  }, [])

  return (
    <div className="page">
      <PairPanel pairing={pairing.pairing} waiting={pairing.waiting} gaveUp={pairing.gaveUp} error={pairing.pairError}
        label={label} onLabelChange={setLabel} onPair={() => flow.pair(label)} onCancel={() => flow.cancelPairing()} />

      <DeviceList devices={devices} statuses={pairing.statuses} busy={pairing.busy} selected={deviceId}
        error={pairing.listError} onSelect={id => flow.select(id)} onRevoke={d => flow.revoke(d)} onRefresh={refresh} now={now} />

      <section className="card section">
        <h3 className="heading-display section-title">{uiText("Check the connection")}</h3>
        <div className="flex items-center gap-2 bridge-status-row">
          <button onClick={revit.check} disabled={revit.checking} className="btn-primary">{revit.checking ? uiText("Checking...") : uiText("Check")}</button>
          <span className="section-copy small" style={{ margin: 0 }}>
            {deviceId ? uiText("Device {0}", deviceId) : uiText("Local add-in (TCP)")}
            {capacity ? uiText(" | {0} of {1} devices connected", capacity.connected, capacity.max_devices) : ''}
          </span>
        </div>
        <div className="status-line">{uiText(revit.status)}</div>
        {revit.units && <div className="status-line">{revit.units}</div>}
      </section>

      <HostModels />


      {config.features.admin && (
        <>
          <section className="card section">
            <h3 className="heading-display section-title">{uiText("Admin")}</h3>
            <p className="section-copy">{uiText("Needed to list every device, to add, edit or delete skills, to edit, delete or save capability packs and to read interaction logs.")}</p>
            <input type="password" className="input-field" value={adminToken} onChange={e => setAdminToken(e.target.value)}
              placeholder={uiText("Admin password")} autoComplete="off" style={{ maxWidth: 320 }} />
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

/* Each role's model as the host announces it (/config.json features.models). */
function HostModels() {
  const { models, modelsFile, roles, chatModeDefault } = getConfig().features
  return <RoleModels models={models} modelsFile={modelsFile} roles={roles} chatModeDefault={chatModeDefault} />
}
