/* Three pages: Connect, Task, Capabilities. */

import { useState } from 'react'
import { getConfig } from './config'
import { useSessionStore } from './store'
import ConnectPage from './components/pages/ConnectPage'
import TaskPage from './components/pages/TaskPage'
import CapabilitiesPage from './components/pages/CapabilitiesPage'
import EvidencePage from './components/pages/EvidencePage'

const TABS = ['Connect', 'Task', 'Capabilities', 'Evidence'] as const

export default function App() {
  const [active, setActive] = useState(0)
  const slot = useSessionStore(s => s.slot)
  const llmModel = useSessionStore(s => s.llmModel)
  const config = getConfig()
  const modelLabel = llmModel || (config.features.serverModel ? 'server default' : 'not set')

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
          <div className="runtime-chip"><span>Revit</span><strong>{slot ? `slot ${slot}` : 'local'}</strong></div>
          <div className="runtime-chip"><span>Model</span><strong>{modelLabel}</strong></div>
          <div className="runtime-chip"><span>API</span><strong>{config.apiBase || 'same-origin'}</strong></div>
        </div>
      </header>

      <nav className="tab-bar">
        {TABS.map((tab, i) => (
          <button key={tab} onClick={() => setActive(i)} className={`tab-button${active === i ? ' is-active' : ''}`}>{tab}</button>
        ))}
      </nav>

      <main className="flex-1 overflow-y-auto">
        {active === 0 && <ConnectPage />}
        {active === 1 && <TaskPage />}
        {active === 2 && <CapabilitiesPage />}
        {active === 3 && <EvidencePage />}
      </main>
    </div>
  )
}
