import { useEffect, useId, useState } from 'react'
import { apiFetch, BridgeApiError } from '../../api/client'
import { getConfig, ROLES, updateModels, type RoleName } from '../../config'
import { canUseBrowserModel, cleanBrowserKey, useSessionStore } from '../../store'
import { browserLanguage } from '../../utils/language'

const endpoint = '/api/v1/settings/models'
type Scope = 'shared' | RoleName
type Summary = { base_url: string; model: string; key_set: boolean; key_tail: string | null }
type Details = { configured: boolean; saved: Partial<Record<Scope, Summary>>; models: Record<RoleName, Summary> }

const labels = {
  en: { shared: 'All roles', planner: 'Planner', analyst: 'Analyst', supervisor: 'Supervisor',
    heading: 'Models', scope: 'Apply to', url: 'Endpoint URL', model: 'Model name', key: 'API key',
    save: 'Save', clear: 'Clear saved group', saved: 'Saved', cleared: 'Cleared', loading: 'Loading settings…',
    missing: 'No key set', present: 'Key set', noModel: 'Choose a role and enter a model endpoint, name and key.',
    keep: 'Leave blank to keep the saved key at this address.', effective: 'Current models',
    env: 'Environment settings take priority. Role settings override All roles.',
    admin: 'Admin password', unlock: 'Enter the admin password to manage host models.',
    guest: 'Bring your own model', guestHint: 'Guest credentials are kept in this tab until it closes.',
    reset: 'Clear model settings', error: 'Could not load or save model settings. Check the endpoint and settings directory.',
    storeError: 'Could not read saved model settings. Check secrets.json and its permissions.',
    connected: 'The host model is configured.', unconfigured: 'Ask an administrator to configure a model.' },
  zh: { shared: '所有角色', planner: '规划员', analyst: '分析员', supervisor: '监督员',
    heading: '模型', scope: '应用到', url: '接口地址', model: '模型名', key: '密钥',
    save: '保存', clear: '清除本组设置', saved: '已保存', cleared: '已清除', loading: '正在加载设置…',
    missing: '未设置密钥', present: '已设置密钥', noModel: '选择角色并输入接口地址、模型名和密钥。',
    keep: '留空可保留该地址已保存的密钥。', effective: '当前使用的模型',
    env: '环境变量优先。单个角色的设置优先于所有角色的通用设置。',
    admin: '管理员密码', unlock: '输入管理员密码可管理服务器模型。',
    guest: '使用自己的模型', guestHint: '访客密钥仅保留在当前标签页，关闭后清除。',
    reset: '清除标签页模型设置', error: '无法读取或保存模型设置，请检查接口地址和设置目录。',
    storeError: '无法读取已保存的模型设置。请检查 secrets.json 及其访问权限。',
    connected: '服务器模型已配置。', unconfigured: '请联系管理员配置模型。' },
} as const

function ModelKeyInput({ value, onChange }: { value: string; onChange: (key: string) => void }) {
  return <input className="input-field" type="password" autoComplete="off" data-1p-ignore data-lpignore="true"
    value={value} onChange={e => onChange(e.target.value)} />
}

function GuestModel() {
  const t = labels[browserLanguage()]
  const { llmBaseUrl, llmModel, setLlm } = useSessionStore()
  const [key, setKey] = useState('')
  return <section className="card section">
    <h3>{t.guest}</h3><p>{t.guestHint}</p>
    <div className="form-grid">
      <label>{t.url}<input className="input-field" value={llmBaseUrl} onChange={e => setLlm({ llmBaseUrl: e.target.value })} /></label>
      <label>{t.model}<input className="input-field" value={llmModel} onChange={e => setLlm({ llmModel: e.target.value })} /></label>
      <label>{t.key}<ModelKeyInput value={key} onChange={value => { setKey(value); setLlm({ llmKey: value }) }} /></label>
    </div>
    <button className="btn-ghost" onClick={() => { setKey(''); setLlm({ llmBaseUrl: '', llmModel: '', llmKey: '' }) }}>{t.reset}</button>
  </section>
}

function Editor({ details, role, onChange }: { details: Details; role: Scope; onChange: (d: Details) => void }) {
  const t = labels[browserLanguage()]
  const formId = useId()
  const saved = details.saved[role]
  const [url, setUrl] = useState(saved?.base_url || '')
  const [model, setModel] = useState(saved?.model || '')
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const submit = async (clear: boolean) => {
    setBusy(true)
    setMessage('')
    try {
      const result = await apiFetch<Details>(clear ? endpoint + '?role=' + role : endpoint, {
        method: clear ? 'DELETE' : 'PUT', redirect: 'error', cache: 'no-store',
        body: clear ? undefined : JSON.stringify({ role, base_url: url, model, ...(key ? { api_key: key } : {}) }),
      })
      setKey('')
      if (clear) { setUrl(''); setModel('') }
      setMessage(clear ? t.cleared : t.saved)
      onChange(result)
    } catch { setMessage(t.error) } finally { setBusy(false) }
  }
  return <>
    <form id={formId} onSubmit={e => { e.preventDefault(); void submit(false) }}>
      <fieldset disabled={busy} className="form-grid">
        <label>{t.url}<input className="input-field" type="url" required value={url} onChange={e => setUrl(e.target.value)} /></label>
        <label>{t.model}<input className="input-field" required value={model} onChange={e => setModel(e.target.value)} /></label>
      </fieldset>
    </form>
    <fieldset disabled={busy} className="form-grid">
      <label>{t.key}<ModelKeyInput value={key} onChange={setKey} /></label>
    </fieldset>
    {saved?.key_set && <p>{t.present} {saved.key_tail && '••••' + saved.key_tail}. {t.keep}</p>}
    <button className="btn-primary" type="submit" form={formId} disabled={busy}>{t.save}</button>{' '}
    <button className="btn-ghost" type="button" disabled={busy || !saved} onClick={() => void submit(true)}>{t.clear}</button>
    <span role="status">{message}</span>
  </>
}

function ServerModels({ token }: { token: string }) {
  const t = labels[browserLanguage()]
  const [details, setDetails] = useState<Details | null>(null)
  const [role, setRole] = useState<Scope>('shared')
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    apiFetch<Details>(endpoint, { signal: controller.signal, redirect: 'error', cache: 'no-store' })
      .then(result => { if (!controller.signal.aborted) setDetails(result) })
      .catch(e => {
        if (controller.signal.aborted) return
        const status = e instanceof BridgeApiError ? e.status : null
        setError(status === 503 ? t.storeError : status === 403 ? t.unlock : t.error)
      })
    return () => controller.abort()
  }, [token, t])
  if (!details) return <p role="status">{error || t.loading}</p>
  return <>
    <h3>{t.effective}</h3>
    <ul>{ROLES.map(r => <li key={r}>{t[r]} · {details.models[r].model || '—'} · {details.models[r].base_url || '—'} · {details.models[r].key_set
      ? t.present + (details.models[r].key_tail ? ' ••••' + details.models[r].key_tail : '') : t.missing}</li>)}</ul>
    <p>{details.configured ? t.env : t.noModel}</p>
    <label>{t.scope} <select value={role} onChange={e => setRole(e.target.value as Scope)}>
      {(['shared', ...ROLES] as const).map(r => <option key={r} value={r}>{t[r]}</option>)}
    </select></label>
    <Editor key={role} role={role} details={details} onChange={d => {
      setDetails(d)
      updateModels(d.configured, { analyst: d.models.analyst.model, planner: d.models.planner.model, supervisor: d.models.supervisor.model })
      cleanBrowserKey()
    }} />
  </>
}

export default function ModelSettings() {
  const t = labels[browserLanguage()]
  const { mode, serverModel } = getConfig().features
  const { adminToken, setAdminToken } = useSessionStore()
  return <div className="page">
    <section className="card section">
      <h2>{t.heading}</h2>
      {mode === 'cloud' && <label>{t.admin}<input className="input-field" type="password" autoComplete="off"
        value={adminToken} onChange={e => setAdminToken(e.target.value)} /></label>}
      {mode === 'local' || adminToken ? <ServerModels key={adminToken} token={adminToken} /> : <p>{serverModel ? t.connected : t.unconfigured} {t.unlock}</p>}
    </section>
    {canUseBrowserModel() && <GuestModel />}
  </div>
}
