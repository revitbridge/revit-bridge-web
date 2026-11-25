import { useEffect, useState } from 'react'
import { apiFetch } from '../../api/client'
import { getConfig } from '../../config'
import { useSessionStore } from '../../store'
import { browserLanguage } from '../../utils/language'

const endpoint = '/api/v1/settings/remote-mcp'
interface TokenInfo { id: string; label: string; device_id: string; created_at: string; last_used_at: string | null }
interface Call { time: string; tool: string; success: boolean; evidence_id: string }
interface Status { enabled: boolean; device_id: string | null; tokens: TokenInfo[]; calls: Call[] }
const options: RequestInit = { cache: 'no-store', redirect: 'error' }

function Panel() {
  const zh = browserLanguage() === 'zh'
  const [status, setStatus] = useState<Status>()
  const [label, setLabel] = useState('')
  const [issued, setIssued] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [reload, setReload] = useState(0)
  const [withToken, setWithToken] = useState(getConfig().features.mode === 'cloud')
  useEffect(() => {
    const controller = new AbortController()
    apiFetch<Status>(endpoint, { ...options, signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) { setStatus(result); setFailed(false) }
    }, () => { if (!controller.signal.aborted) setFailed(true) })
    return () => controller.abort()
  }, [reload])
  async function change(id?: string) {
    setBusy(true); setFailed(false); setIssued('')
    try {
      if (id) {
        await apiFetch(endpoint + '/tokens/' + encodeURIComponent(id), { ...options, method: 'DELETE' })
      } else {
        const result = await apiFetch<{ token: string }>(endpoint + '/tokens', {
          ...options, method: 'POST', body: JSON.stringify({ label }),
        })
        setIssued(result.token); setLabel('')
      }
      setReload(n => n + 1)
    } catch { setFailed(true) } finally { setBusy(false) }
  }
  const address = new URL(getConfig().apiBase.replace(/\/$/, '') + '/mcp', window.location.origin).href
  const cloud = getConfig().features.mode === 'cloud'
  const claude = { type: 'http', url: address, ...(withToken ? { headers: { Authorization: 'Bearer ${REVIT_MCP_TOKEN}' } } : {}) }
  return <div className="page remote-mcp"><section className="card section">
    <h2>{zh ? '远程 MCP' : 'Remote MCP'}</h2>
    <p><code>{address}</code> · {status ? (status.enabled ? (zh ? '已开启' : 'Enabled') : (zh ? '已关闭' : 'Disabled'))
      : (zh ? '状态未知' : 'Status unavailable')}</p>
    <button className="btn-ghost" onClick={() => setReload(n => n + 1)}>{zh ? '刷新' : 'Refresh'}</button>
    {failed && <p role="alert">{zh ? '无法读取或更改远程 MCP 设置。请检查配对和设置存储。'
      : 'Could not read or change Remote MCP settings. Check the pairing and the settings store.'}</p>}
    {!cloud && <label><input type="checkbox" checked={withToken} onChange={e => setWithToken(e.target.checked)} />
      {zh ? '在配置中使用访问令牌' : 'Use an access token in the configuration'}</label>}
    <p>{withToken ? (zh ? '将下方创建的令牌设为 MCP 客户端的环境变量 REVIT_MCP_TOKEN。'
      : 'Set REVIT_MCP_TOKEN in your MCP client environment to the token you create below.')
      : (zh ? '在本机通过回环地址连接时可免令牌。' : 'Direct loopback connections can use these snippets without a token.')}</p>
    <h3>Codex CLI</h3><pre>{'codex mcp add revit --url ' + address + (withToken ? ' --bearer-token-env-var REVIT_MCP_TOKEN' : '')}</pre>
    <h3>Claude Code · .mcp.json</h3><pre>{JSON.stringify({ mcpServers: { revit: claude } }, null, 2)}</pre>
  </section>
  <section className="card section"><h3>{zh ? '访问令牌' : 'Access tokens'}</h3>
    {status?.device_id ? <>
      <p>{zh ? '设备' : 'Device'} · {status.device_id}</p>
      <form onSubmit={e => { e.preventDefault(); void change() }}>
        <label>{zh ? '令牌名称' : 'Token label'} <input className="input-field" required maxLength={80} value={label}
          onChange={e => setLabel(e.target.value)} /></label>{' '}
        <button className="btn-primary" disabled={busy || !label.trim()}>{zh ? '创建令牌' : 'Create token'}</button>
      </form>
      {issued && <div role="status"><p>{zh ? '现在复制令牌。只显示这一次。' : 'Copy this token now. It is shown once.'}</p>
        <code>{issued}</code>{' '}<button className="btn-ghost" onClick={() => setIssued('')}>{zh ? '隐藏' : 'Dismiss'}</button></div>}
      <ul>{status.tokens.map(t => <li key={t.id}><strong>{t.label}</strong> · {zh ? '创建时间' : 'Created'} {t.created_at}
        {' · '}{zh ? '上次使用' : 'Last used'} {t.last_used_at || (zh ? '从未使用' : 'Never')}{' '}
        <button className="btn-ghost" disabled={busy} onClick={() => void change(t.id)}>{zh ? '撤销' : 'Revoke'}</button></li>)}</ul>
    </> : cloud && <p>{zh ? '先在“设置 → 连接”中选择已配对的设备。' : 'Select a paired device in Settings → Connection.'}</p>}
  </section>
  <section className="card section"><h3>{zh ? '最近调用' : 'Recent calls'}</h3>
    {status && (status.calls.length ? <ul>{status.calls.map(c => <li key={c.evidence_id}><time>{c.time}</time> · {c.tool}
      {' · '}{c.success ? (zh ? '成功' : 'Succeeded') : (zh ? '失败' : 'Failed')} · <code>{c.evidence_id}</code></li>)}</ul>
      : <p>{zh ? '尚无远程调用记录。' : 'No remote calls recorded.'}</p>)}
  </section></div>
}

export default function RemoteMcpSettings() {
  const { deviceId, browserKey, adminToken } = useSessionStore()
  // Drop the one-time display and pending reads when the selected identity changes.
  return <Panel key={JSON.stringify([deviceId, browserKey, adminToken])} />
}
