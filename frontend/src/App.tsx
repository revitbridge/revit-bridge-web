import { uiText } from './utils/strings'
import { useEffect, useState, useSyncExternalStore } from 'react'
import { bridgeApi } from './api/bridge'
import { getConfig, subscribeConfig } from './config'
import { cleanBrowserKey, useSessionStore } from './store'
import { browserLanguage, setLanguage, subscribeLanguage } from './utils/language'
import Icon from './components/shared/Icon'
import ConnectPage from './components/pages/ConnectPage'
import TaskPage from './components/pages/TaskPage'
import CapabilitiesPage from './components/pages/CapabilitiesPage'
import EvidencePage from './components/pages/EvidencePage'
import TokenPage from './components/pages/TokenPage'
import ModelSettings from './components/pages/ModelSettings'
import ModelPopover from './components/connect/ModelPopover'
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
  return <button className={"runtime-chip connection-chip " + (state === null ? "" : state ? "is-online" : "is-offline")} onClick={refresh} title={uiText("Check connection")}>
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

  return <div className="app-shell">
    <header className="app-header">
      <div className="brand-block"><div className="brand-mark" aria-hidden="true"><Icon name="mark" /></div><h1 className="app-title">revit-bridge</h1></div>
      <nav className="tab-bar">
        {TABS.map(tab => <button key={tab} onClick={() => setView({ ...view, tab, tool: undefined })}
          className={`tab-button${active === tab ? ' is-active' : ''}`} aria-current={active === tab ? 'page' : undefined}><Icon name={tab} />{zh ? tabsZh[tab] : tab}</button>)}
      </nav>
      <div className="runtime-chips"><ConnectionStatus state={health?.connected ?? null} refresh={refresh} />
        <ModelPopover model={model} onManage={() => setView({ ...view, tab: 'Settings', settings: 'models' })} />
        <button type="button" className="runtime-chip" aria-label={uiText("Switch to Chinese")}
          onClick={() => setLanguage(zh ? 'en' : 'zh')}>中 / EN</button>
      </div>
    </header>
    {removed && <p role="status">{uiText("The previously saved model key was removed from this browser.")}</p>}
    <main className={active === 'Chat' ? 'main-chat' : undefined}>
      {active === 'Chat' && <>
        {!features.serverModel && !(features.byoModel && guestModel) &&
          <div className="configuration-notice"><a href="/settings">{uiText("Open Settings to configure a model and start chatting.")}</a></div>}
        <TaskPage initialTool={view.tool} support={!health?.connected ? 'offline' : health.selection === false ? 'legacy' : 'ready'}
          onDestination={(destination, brief) => setView(destination === 'authoring'
            ? { tab: 'Tools', capabilities: 'authoring', brief }
            : { tab: 'Settings', capabilities: 'packs', settings: 'remote-mcp' })} />
      </>}
      {active === 'Tools' && <CapabilitiesPage view={view.capabilities} brief={view.brief} connected={health?.connected} onConnectionLost={refresh} tool={view.tool}
        onSavedLink={(tool, chat) => setView({ tab: chat ? 'Chat' : 'Tools', capabilities: 'packs', tool })}
        onRemote={() => setView({ tab: 'Settings', capabilities: 'packs', settings: 'remote-mcp' })} onView={capabilities => setView({ ...view, capabilities, tool: undefined })} />}
      {active === 'Evidence' && <EvidencePage />}
      {active === 'Workflows' && <WorkflowsPage />}
      {active === 'Settings' && <div className="settings-layout">
        <nav className="settings-nav" aria-label={uiText("Settings")}>{(Object.keys(sections) as SettingsView[]).map(s =>
          <button key={s} className={`subtab${s === settings ? ' is-active' : ''}`}
            onClick={() => setView({ ...view, settings: s })}>{sections[s]}</button>)}</nav>
        {settings === 'models' && <ModelSettings />}
        {settings === 'remote-mcp' && <RemoteMcpSettings />}
        {settings === 'connection' && <ConnectPage />}
        {settings === 'usage' && <TokenPage />}
      </div>}
    </main>
  </div>
}
