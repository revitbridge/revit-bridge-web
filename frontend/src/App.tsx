import { uiText } from './utils/strings'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { bridgeApi } from './api/bridge'
import { getConfig, subscribeConfig } from './config'
import { cleanBrowserKey, useSessionStore } from './store'
import { browserLanguage, setLanguage, subscribeLanguage } from './utils/language'
import ConnectPage from './components/pages/ConnectPage'
import TaskPage from './components/pages/TaskPage'
import CapabilitiesPage from './components/pages/CapabilitiesPage'
import EvidencePage from './components/pages/EvidencePage'
import TokenPage from './components/pages/TokenPage'
import ModelSettings from './components/pages/ModelSettings'
import WorkflowsPage from './components/workflows/WorkflowsPage'
import RemoteMcpSettings from './components/pages/RemoteMcpSettings'
import { currentPath, showPath, TABS, viewOf, type SettingsView, type View } from './route'

const tabsZh = { Chat: '对话', Tools: '工具', Workflows: '工作流', Evidence: '证据', Settings: '设置' }

function useConnection() {
  const device = useSessionStore(s => s.deviceId)
  const [status, setStatus] = useState<{ device: string; connected: boolean; selection?: boolean | null } | null>(null)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    let busy = false
    const check = async () => {
      clearTimeout(timer)
      if (!active || document.hidden || busy) return
      busy = true
      const health = await bridgeApi.revitHealth().catch(() => null)
      busy = false
      if (!active) return
      setStatus({ device, connected: !!health?.revit_connected, selection: health?.selection_state_supported })
      if (!health?.revit_connected) timer = setTimeout(check, 10000)
    }
    void check()
    document.addEventListener('visibilitychange', check)
    return () => { active = false; clearTimeout(timer); document.removeEventListener('visibilitychange', check) }
  }, [device, refresh])
  return { health: status?.device === device ? status : null, refresh: () => setRefresh(r => r + 1) }
}

function ConnectionStatus({ state, refresh }: { state: boolean | null; refresh: () => void }) {
  const label = state === null ? uiText("Checking") : state ? uiText("Connected") : uiText("Disconnected")
  return <button className="runtime-chip" onClick={refresh} title={uiText("Check connection")}>
    <span>Revit</span><strong>{label}</strong></button>
}

export default function App({ path }: { path?: string }) {
  const [view, setView] = useState<View>(() => viewOf(path ?? currentPath()))
  const { features } = useSyncExternalStore(subscribeConfig, getConfig, getConfig)
  const guestModel = useSessionStore(s => s.llmModel)
  const removed = useSessionStore(s => s.removedLegacyKey)
  const zh = useSyncExternalStore(subscribeLanguage, browserLanguage, browserLanguage) === 'zh'
  const { health, refresh } = useConnection()
  useEffect(() => { document.documentElement.lang = zh ? 'zh' : 'en' }, [zh])
  const active = view.tab
  const settings = view.settings || 'models'
  const model = features.models.planner || (features.byoModel && !features.serverModel ? guestModel : '') || uiText("Not set")
  useEffect(() => { showPath(view) }, [view])
  useEffect(() => { cleanBrowserKey() }, [features.serverModel])
  const sections: Record<SettingsView, string> = { models: uiText("Models"), connection: uiText("Connection"),
    'remote-mcp': uiText("Remote MCP"), usage: uiText("Usage") }

  return <div className="app-shell flex flex-col h-screen">
    <header className="app-header">
      <div className="brand-block"><div className="brand-mark" aria-hidden="true">R</div><h1 className="app-title">revit-bridge</h1></div>
      <div className="runtime-chips"><ConnectionStatus state={health?.connected ?? null} refresh={refresh} />
        <div className="runtime-chip"><span>{uiText("Model")}</span><strong>{model}</strong></div>
        <button type="button" className="runtime-chip" aria-label={uiText("Switch to Chinese")}
          onClick={() => setLanguage(zh ? 'en' : 'zh')}>中 / EN</button>
      </div>
    </header>
    <nav className="tab-bar">
      {TABS.map(tab => <button key={tab} onClick={() => setView({ ...view, tab })}
        className={`tab-button${active === tab ? ' is-active' : ''}`}>{zh ? tabsZh[tab] : tab}</button>)}
    </nav>
    {removed && <p role="status">{uiText("The previously saved model key was removed from this browser.")}</p>}
    <main className="flex-1 overflow-y-auto">
      {active === 'Chat' && <>
        {!features.serverModel && !(features.byoModel && guestModel) &&
          <div className="page"><a href="/settings">{uiText("Open Settings to configure a model and start chatting.")}</a></div>}
        <TaskPage support={!health?.connected ? 'offline' : health.selection === false ? 'legacy' : 'ready'}
          onDestination={(destination, brief) => setView(destination === 'authoring'
            ? { tab: 'Tools', capabilities: 'authoring', brief }
            : { tab: 'Settings', capabilities: 'packs', settings: 'remote-mcp' })} />
      </>}
      {active === 'Tools' && <CapabilitiesPage view={view.capabilities} brief={view.brief} onRemote={() => setView({ tab: 'Settings', capabilities: 'packs', settings: 'remote-mcp' })} onView={capabilities => setView({ ...view, capabilities })} />}
      {active === 'Evidence' && <EvidencePage />}
      {active === 'Workflows' && <WorkflowsPage />}
      {active === 'Settings' && <>
        <div className="subtab-bar">{(Object.keys(sections) as SettingsView[]).map(s =>
          <button key={s} className={`subtab${s === settings ? ' is-active' : ''}`}
            onClick={() => setView({ ...view, settings: s })}>{sections[s]}</button>)}</div>
        {settings === 'models' && <ModelSettings />}
        {settings === 'remote-mcp' && <RemoteMcpSettings />}
        {settings === 'connection' && <ConnectPage />}
        {settings === 'usage' && <TokenPage />}
      </>}
    </main>
  </div>
}
