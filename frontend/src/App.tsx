import { useEffect, useState, useSyncExternalStore } from 'react'
import { bridgeApi } from './api/bridge'
import { getConfig, subscribeConfig } from './config'
import { cleanBrowserKey, useSessionStore } from './store'
import { browserLanguage } from './utils/language'
import ConnectPage from './components/pages/ConnectPage'
import TaskPage from './components/pages/TaskPage'
import CapabilitiesPage from './components/pages/CapabilitiesPage'
import EvidencePage from './components/pages/EvidencePage'
import TokenPage from './components/pages/TokenPage'
import ModelSettings from './components/pages/ModelSettings'
import { currentPath, showPath, TABS, viewOf, type SettingsView, type View } from './route'

const tabsZh = { Chat: '对话', Tools: '工具', Evidence: '证据', Settings: '设置' }

function ConnectionStatus({ zh }: { zh: boolean }) {
  const device = useSessionStore(s => s.deviceId)
  const [status, setStatus] = useState<{ device: string; connected: boolean } | null>(null)
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    const report = (connected: boolean) => { if (active) setStatus({ device, connected }) }
    bridgeApi.revitHealth().then(v => report(v.revit_connected), () => report(false))
    return () => { active = false }
  }, [device, refresh])
  const state = status?.device === device ? status.connected : null
  const label = state === null ? (zh ? '检查中' : 'Checking') : state ? (zh ? '已连接' : 'Connected') : (zh ? '未连接' : 'Disconnected')
  return <button className="runtime-chip" onClick={() => setRefresh(r => r + 1)} title={zh ? '重新检查连接' : 'Check connection'}>
    <span>Revit</span><strong>{label}</strong></button>
}

export default function App({ path }: { path?: string }) {
  const [view, setView] = useState<View>(() => viewOf(path ?? currentPath()))
  const { features } = useSyncExternalStore(subscribeConfig, getConfig, getConfig)
  const guestModel = useSessionStore(s => s.llmModel)
  const removed = useSessionStore(s => s.removedLegacyKey)
  const zh = browserLanguage() === 'zh'
  const active = view.tab
  const settings = view.settings || 'models'
  const model = features.models.planner || (features.byoModel && !features.serverModel ? guestModel : '') || (zh ? '未设置' : 'Not set')
  useEffect(() => { showPath(view) }, [view])
  useEffect(() => { cleanBrowserKey() }, [features.serverModel])
  const sections: Record<SettingsView, string> = { models: zh ? '模型' : 'Models', connection: zh ? '连接' : 'Connection', usage: zh ? '用量' : 'Usage' }

  return <div className="app-shell flex flex-col h-screen">
    <header className="app-header">
      <div className="brand-block"><div className="brand-mark" aria-hidden="true">R</div><h1 className="app-title">revit-bridge</h1></div>
      <div className="runtime-chips"><ConnectionStatus zh={zh} />
        <div className="runtime-chip"><span>{zh ? '模型' : 'Model'}</span><strong>{model}</strong></div>
      </div>
    </header>
    <nav className="tab-bar">
      {TABS.map(tab => <button key={tab} onClick={() => setView({ ...view, tab })}
        className={`tab-button${active === tab ? ' is-active' : ''}`}>{zh ? tabsZh[tab] : tab}</button>)}
    </nav>
    {removed && <p role="status">{zh ? '已清除浏览器中原先保存的模型密钥。' : 'The previously saved model key was removed from this browser.'}</p>}
    <main className="flex-1 overflow-y-auto">
      {active === 'Chat' && <>
        {!features.serverModel && !(features.byoModel && guestModel) &&
          <div className="page"><a href="/settings">{zh ? '打开设置配置模型，开始对话。' : 'Open Settings to configure a model and start chatting.'}</a></div>}
        <TaskPage />
      </>}
      {active === 'Tools' && <CapabilitiesPage view={view.capabilities} onView={capabilities => setView({ ...view, capabilities })} />}
      {active === 'Evidence' && <EvidencePage />}
      {active === 'Settings' && <>
        <div className="subtab-bar">{(Object.keys(sections) as SettingsView[]).map(s =>
          <button key={s} className={`subtab${s === settings ? ' is-active' : ''}`}
            onClick={() => setView({ ...view, settings: s })}>{sections[s]}</button>)}</div>
        {settings === 'models' && <ModelSettings />}
        {settings === 'connection' && <ConnectPage />}
        {settings === 'usage' && <TokenPage />}
      </>}
    </main>
  </div>
}
