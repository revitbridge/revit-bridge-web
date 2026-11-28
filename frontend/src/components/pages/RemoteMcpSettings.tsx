import Snippet from '../shared/Snippet'
import { uiText } from '../../utils/strings'
import { useEffect, useId, useState } from 'react'
import { apiFetch, BridgeApiError } from '../../api/client'
import { getConfig } from '../../config'
import { useSessionStore } from '../../store'

const endpoint = '/api/v1/settings/remote-mcp'
interface TokenInfo { id: string; label: string; device_id: string; created_at: string; last_used_at: string | null }
interface Call { time: string; tool: string; success: boolean; evidence_id: string }
interface Status { enabled: boolean; device_id: string | null; tokens: TokenInfo[]; calls: Call[] }
const options: RequestInit = { cache: 'no-store', redirect: 'error' }

const clients = ['Claude Code', 'Codex CLI', 'Cursor', 'VS Code'] as const
type McpClient = typeof clients[number]
const clientStorageKey = 'revit-bridge-web.mcpClient'
const bearer = 'Bearer ${REVIT_MCP_TOKEN}'
// Cursor and VS Code read an environment variable as ${env:NAME}; ${NAME} would be sent as it is.
const envBearer = 'Bearer ${env:REVIT_MCP_TOKEN}'

function ConnectionSnippets({ address, withToken }: { address: string; withToken: boolean }) {
  const [client, setClient] = useState<McpClient>(() => {
    try {
      const saved = localStorage.getItem(clientStorageKey)
      return clients.find(name => name === saved) ?? clients[0]
    } catch { return clients[0] }
  })
  const id = useId()
  const entry = (authorization: string) => ({ url: address, headers: withToken ? { Authorization: authorization } : undefined })

  function select(name: McpClient) {
    setClient(name)
    try { localStorage.setItem(clientStorageKey, name) } catch { /* Storage is optional. */ }
  }

  return <>
    <Snippet title={uiText('MCP JSON (mcpServers)')} text={JSON.stringify({ mcpServers: { revit: { type: 'http', ...entry(bearer) } } }, null, 2)} />
    {withToken && <p>{uiText('Clients differ in how they read an environment variable: ${VAR} in Claude Code, ${env:VAR} in Cursor and VS Code.')}</p>}
    <h3 id={id}>{uiText('Client setup')}</h3>
    <div className="subtab-bar" role="tablist" aria-labelledby={id}>
      {clients.map((name, index) => <button key={name} id={`${id}-${index}`} type="button" role="tab"
        className={name === client ? 'subtab is-active' : 'subtab'}
        aria-controls={`${id}-panel`} aria-selected={name === client} tabIndex={name === client ? 0 : -1}
        onClick={() => select(name)} onKeyDown={event => {
          if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
          event.preventDefault()
          const step = event.key === 'ArrowRight' ? 1 : -1
          const next = (index + step + clients.length) % clients.length
          select(clients[next])
          document.getElementById(`${id}-${next}`)?.focus()
        }}>{uiText(name)}</button>)}
    </div>
    <div role="tabpanel" id={`${id}-panel`} aria-labelledby={`${id}-${clients.indexOf(client)}`}>
      {client === 'Claude Code' && <Snippet text={`claude mcp add --transport http revit ${address}`
        + (withToken ? ` --header 'Authorization: ${bearer}'` : '')} />}
      {client === 'Codex CLI' && <Snippet text={'codex mcp add revit --url ' + address
        + (withToken ? ' --bearer-token-env-var REVIT_MCP_TOKEN' : '')} />}
      {client === 'Cursor' && <Snippet title={uiText('Add to .cursor/mcp.json')}
        text={JSON.stringify({ mcpServers: { revit: entry(envBearer) } }, null, 2)} />}
      {client === 'VS Code' && <Snippet title={uiText('Add to .vscode/mcp.json')}
        text={JSON.stringify({ servers: { revit: { type: 'http', ...entry(envBearer) } } }, null, 2)} />}
    </div>
  </>
}

function Panel() {
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
    }, error => {
      if (controller.signal.aborted) return
      if (error instanceof BridgeApiError && error.code === 'remote_mcp_disabled') {
        setStatus({ enabled: false, device_id: null, tokens: [], calls: [] })
        setIssued('')
        setFailed(false)
      } else {
        setStatus(undefined)
        setFailed(true)
      }
    })
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
  return <div className="page remote-mcp"><section className="card section">
    <h2>{uiText("Remote MCP")}</h2>
    <p><code>{address}</code> · {status ? (status.enabled ? uiText("Enabled") : uiText("Disabled"))
      : uiText("Status unavailable")}</p>
    <button className="btn-ghost" onClick={() => setReload(n => n + 1)}>{uiText("Refresh")}</button>
    {failed && <p role="alert">{uiText("Could not read or change Remote MCP settings. Check the pairing and the settings store.")}</p>}
    {!cloud && <label><input type="checkbox" checked={withToken} onChange={e => setWithToken(e.target.checked)} />
      {uiText("Use an access token in the configuration")}</label>}
    <p>{withToken ? uiText("Set REVIT_MCP_TOKEN in your MCP client environment to the token you create below.")
      : uiText("Direct loopback connections can use these snippets without a token.")}</p>
    <ConnectionSnippets address={address} withToken={withToken} />
  </section>
  <section className="card section"><h3>{uiText("Access tokens")}</h3>
    {status?.device_id ? <>
      <p>{uiText("Device")} · {status.device_id}</p>
      {status.tokens.length >= 5 && <p>{uiText("This device has five active tokens. Revoke one to create another.")}</p>}
      <form onSubmit={e => { e.preventDefault(); void change() }}>
        <label>{uiText("Token label")} <input className="input-field" required maxLength={64} value={label}
          onChange={e => setLabel(e.target.value)} /></label>{' '}
        <button className="btn-primary" disabled={busy || !label.trim() || status.tokens.length >= 5}>{uiText("Create token")}</button>
      </form>
      {issued && <div role="status"><p>{uiText("Copy this token now. It is shown once.")}</p>
        <code>{issued}</code>{' '}<button className="btn-ghost" onClick={() => setIssued('')}>{uiText("Dismiss")}</button></div>}
      {status.tokens.length > 0 && <div className="table-scroll"><table><thead><tr>
        <th>{uiText("Token label")}</th><th>{uiText("Created")}</th><th>{uiText("Last used")}</th><th /></tr></thead>
        <tbody>{status.tokens.map(t => <tr key={t.id}><td>{t.label}</td><td>{t.created_at}</td><td>{t.last_used_at || uiText("Never")}</td>
          <td><button className="btn-ghost danger" disabled={busy} onClick={() => void change(t.id)}>{uiText("Revoke")}</button></td>
        </tr>)}</tbody></table></div>}
    </> : cloud && status?.enabled && <p>{uiText("Select a paired device in Settings → Connection.")}</p>}
  </section>
  <section className="card section"><h3>{uiText("Recent calls")}</h3>
    {status && (status.calls.length ? <ul>{status.calls.map(c => <li key={c.evidence_id}><time>{c.time}</time> · {c.tool}
      {' · '}{c.success ? uiText("Succeeded") : uiText("Failed")} · <code>{c.evidence_id}</code></li>)}</ul>
      : <p>{uiText("No remote calls recorded.")}</p>)}
  </section></div>
}

export default function RemoteMcpSettings() {
  const { deviceId, browserKey, adminToken } = useSessionStore()
  // Drop the one-time display and pending reads when the selected identity changes.
  return <Panel key={JSON.stringify([deviceId, browserKey, adminToken])} />
}
