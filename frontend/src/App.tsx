/* Three pages: Connect, Task, Capabilities. /capabilities/workbench opens the Capabilities
   page's workbench (route.ts). */

import { useEffect, useState } from 'react'
import { getConfig } from './config'
import { useSessionStore, type PairedDevice } from './store'
import ConnectPage from './components/pages/ConnectPage'
import TaskPage from './components/pages/TaskPage'
import CapabilitiesPage from './components/pages/CapabilitiesPage'
import EvidencePage from './components/pages/EvidencePage'
import { currentPath, showPath, TABS, viewOf, type View } from './route'

/* The header chip: the selected device's name, or the local add-in. */
function deviceLabel(deviceId: string, devices: PairedDevice[]): string {
  if (!deviceId) return 'local'
  return devices.find(d => d.device_id === deviceId)?.label || deviceId
}

export default function App({ path }: { path?: string }) {
  const [view, setView] = useState<View>(() => viewOf(path ?? currentPath()))
  const active = view.tab
  const deviceId = useSessionStore(s => s.deviceId)
  const devices = useSessionStore(s => s.devices)
  const llmModel = useSessionStore(s => s.llmModel)
  const config = getConfig()
  const modelLabel = llmModel || (config.features.serverModel ? 'server default' : 'not set')
  // A local host has no devices: whatever this tab still holds, Revit is the local add-in.
  const revitLabel = config.features.mode === 'local' ? 'local' : deviceLabel(deviceId, devices)

  useEffect(() => { showPath(view) }, [view])

  return (
    <div className="app-shell flex flex-col h-screen">
      <header className="app-header">
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">R</div>
          <div>
            <h1 className="app-title">revit-bridge</h1>
            <p className="app-kicker">Designer intent · Task spec · Revit execution</p>
          </div>
        </div>
        <div className="runtime-chips">
          <div className="runtime-chip"><span>Revit</span><strong>{revitLabel}</strong></div>
          <div className="runtime-chip"><span>Model</span><strong>{modelLabel}</strong></div>
          <div className="runtime-chip"><span>API</span><strong>{config.apiBase || 'same-origin'}</strong></div>
        </div>
      </header>

      <nav className="tab-bar">
        {TABS.map((tab, i) => (
          <button key={tab} onClick={() => setView({ ...view, tab: i })} className={`tab-button${active === i ? ' is-active' : ''}`}>{tab}</button>
        ))}
      </nav>

      <main className="flex-1 overflow-y-auto">
        {active === 0 && <ConnectPage />}
        {active === 1 && <TaskPage />}
        {active === 2 && <CapabilitiesPage view={view.capabilities} onView={capabilities => setView({ ...view, capabilities })} />}
        {active === 3 && <EvidencePage />}
      </main>
    </div>
  )
}
