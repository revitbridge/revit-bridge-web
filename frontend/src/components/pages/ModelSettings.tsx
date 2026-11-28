import { useEffect, useId, useState } from 'react'
import { apiFetch, BridgeApiError } from '../../api/client'
import { getConfig, ROLES, SELECTOR_ROLES } from '../../config'
import { canUseBrowserModel, useSessionStore } from '../../store'
import { browserLanguage } from '../../utils/language'
import { ModelSelectors } from '../connect/ModelPopover'
import { applyModelDetails, endpoint, subscribeModelDetails, type LibraryModel, type ModelDetails as Details, type Scope } from '../connect/modelLibrary'

const labels = {
  en: { shared: 'All roles', planner: 'Planner', analyst: 'Analyst', supervisor: 'Supervisor',
    optimizer: 'Optimizer', library: 'Model library', name: 'Entry name', api: 'API', images: 'Can read images',
    file: 'models file', savedSource: 'saved', shadowed: 'shadowed', edit: 'Edit', remove: 'Delete',
    add: 'Add entry', update: 'Save entry', cancel: 'Cancel', advanced: 'Advanced: one saved group per role',
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
    optimizer: '优化', library: '模型库', name: '条目名称', api: '接口类型', images: '可读取图像',
    file: '模型文件', savedSource: '已保存', shadowed: '已被同名文件条目覆盖', edit: '编辑', remove: '删除',
    add: '添加条目', update: '保存条目', cancel: '取消', advanced: '高级：为每个角色单独保存一组设置',
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
    <button className="btn-ghost danger" type="button" disabled={busy || !saved} onClick={() => void submit(true)}>{t.clear}</button>
    <span role="status">{message}</span>
  </>
}

const blankEntry = { name: '', base_url: '', model: '', api_key: '', api: 'chat', vision: false }

function ModelLibrary({ entries }: { entries: LibraryModel[] }) {
  const t = labels[browserLanguage()]
  const formId = useId()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(blankEntry)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  function reset() {
    setDraft(blankEntry)
    setEditing(false)
    setError(false)
  }
  async function write(method: 'PUT' | 'DELETE', name: string) {
    setBusy(true)
    setError(false)
    const { base_url, model, api_key, api, vision } = draft
    try {
      const details = await apiFetch<Details>(endpoint + '/library/' + encodeURIComponent(name), {
        method, redirect: 'error', cache: 'no-store',
        body: method === 'PUT' ? JSON.stringify({ base_url, model, api, vision, ...(api_key ? { api_key } : {}) }) : undefined,
      })
      applyModelDetails(details)
      if (method === 'PUT' || name === draft.name) reset()
    } catch { setError(true) }
    finally { setBusy(false) }
  }
  function edit(entry: LibraryModel) {
    const { name, base_url, model, api, vision } = entry
    setDraft({ name, base_url, model, api, vision, api_key: '' })
    setEditing(true)
    setError(false)
  }
  return <section className="model-library">
    <h3>{t.library}</h3>
    <ul className="library-entries">{entries.map(entry =>
      <li key={entry.source + '/' + entry.name}>
        <div><strong>{entry.name}</strong> <small className="badge">{entry.source === 'file' ? t.file : t.savedSource}</small>
          {entry.shadowed && <small className="badge">{t.shadowed}</small>}</div>
        <span>{entry.model}</span><span>{entry.base_url}</span>
        <small>{entry.key_set ? t.present + (entry.key_tail ? ' ****' + entry.key_tail : '') : t.missing}</small>
        {entry.source === 'saved' && <div>
          <button type="button" disabled={busy || entry.shadowed} onClick={() => edit(entry)}>{t.edit}</button>{' '}
          <button type="button" className="btn-ghost danger" disabled={busy} onClick={() => void write('DELETE', entry.name)}>{t.remove}</button>
        </div>}
      </li>)}</ul>
    <fieldset disabled={busy} className="library-fields">
      <form id={formId} className="form-grid" onSubmit={e => { e.preventDefault(); void write('PUT', draft.name) }}>
        <label>{t.name}<input className="input-field" required disabled={editing} maxLength={40} pattern="[A-Za-z][A-Za-z0-9_\-]{0,39}"
          value={draft.name} onChange={e => setDraft({ ...draft, name: e.target.value })} /></label>
        <label>{t.url}<input className="input-field" required type="url" value={draft.base_url}
          onChange={e => setDraft({ ...draft, base_url: e.target.value })} /></label>
        <label>{t.model}<input className="input-field" required value={draft.model}
          onChange={e => setDraft({ ...draft, model: e.target.value })} /></label>
        <label>{t.api}<select value={draft.api} onChange={e => setDraft({ ...draft, api: e.target.value })}>
          <option>chat</option><option>responses</option></select></label>
        <label><input type="checkbox" checked={draft.vision} onChange={e => setDraft({ ...draft, vision: e.target.checked })} />{t.images}</label>
      </form>
      <label>{t.key}<ModelKeyInput value={draft.api_key} onChange={api_key => setDraft({ ...draft, api_key })} /></label>
      {editing && <small>{t.keep}</small>}
      <div><button className="btn-primary" form={formId} type="submit">{editing ? t.update : t.add}</button>{' '}
        {editing && <button type="button" onClick={reset}>{t.cancel}</button>}</div>
    </fieldset>
    {error && <p role="alert">{t.error}</p>}
    <ModelSelectors />
  </section>
}

function ServerModels({ token }: { token: string }) {
  const t = labels[browserLanguage()]
  const [details, setDetails] = useState<Details | null>(null)
  const [role, setRole] = useState<Scope>('shared')
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    const unsubscribe = subscribeModelDetails(result => { controller.abort(); setDetails(result) })
    apiFetch<Details>(endpoint, { signal: controller.signal, redirect: 'error', cache: 'no-store' })
      .then(result => { if (!controller.signal.aborted) applyModelDetails(result) })
      .catch(e => {
        if (controller.signal.aborted) return
        const status = e instanceof BridgeApiError ? e.status : null
        setError(status === 503 ? t.storeError : status === 403 ? t.unlock : t.error)
      })
    return () => { unsubscribe(); controller.abort() }
  }, [token, t])
  if (!details) return <p className={error ? "notice" : "skeleton status-line"} role="status">{error || t.loading}</p>
  return <>
    <h3>{t.effective}</h3>
    <ul className="model-profiles">{SELECTOR_ROLES.filter(r => details.models[r]).map(r => <li key={r}><strong>{t[r]}</strong>
      <span>{details.models[r].model || '—'} · {details.models[r].base_url || '—'}</span>
      <small className="badge">{details.models[r].key_set
        ? t.present + (details.models[r].key_tail ? ' ••••' + details.models[r].key_tail : '') : t.missing}</small></li>)}</ul>
    {details.library && <ModelLibrary entries={details.library} />}
    <details className="model-advanced"><summary>{t.advanced}</summary>
    <p>{details.configured ? t.env : t.noModel}</p>
    <label>{t.scope} <select value={role} onChange={e => setRole(e.target.value as Scope)}>
      {(['shared', ...ROLES] as const).map(r => <option key={r} value={r}>{t[r]}</option>)}
    </select></label>
    <Editor key={role} role={role} details={details} onChange={d => applyModelDetails(d, true)} />
    </details>
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
