import { uiText } from '../../utils/strings'
import { useEffect, useRef, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { workflowApi, type Workflow } from '../../api/workflows'
import { useSessionStore } from '../../store'
import type { HostFunction, RevitHealthResponse, ToolDetail, ToolInfo } from '../../types/api'
import { describePreconditions } from '../capabilities/directRun'
import Icon from '../shared/Icon'
import './toolDrawer.css'

function ParameterTable({ parameters }: { parameters: ToolInfo['parameters'] }) {
  return <div className="table-scroll"><table><thead><tr>
    {['Parameter', 'Type', 'Unit', 'Default', 'Required'].map(k => <th key={k}>{uiText(k)}</th>)}
  </tr></thead><tbody>{parameters.map(p => <tr key={p.name}>
    <td><code>{p.name}</code></td><td>{p.type || '—'}</td><td>{p.unit || '—'}</td>
    <td>{p.default === undefined ? '—' : JSON.stringify(p.default)}</td>
    <td>{(p.required ?? !('default' in p)) ? uiText("Required") : uiText("Optional")}</td>
  </tr>)}</tbody></table></div>
}

function ToolRow({ tool, choose }: { tool: ToolInfo; choose: (example: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const [detail, setDetail] = useState<ToolDetail>()
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!expanded) return
    let current = true
    bridgeApi.getTool(tool.name).then(data => { if (current) setDetail(data) })
      .catch(() => { if (current) setFailed(true) })
    return () => { current = false }
  }, [expanded, tool.name])
  const name = tool.display_name || tool.name
  const info = detail ?? tool
  const example = detail?.source_query?.trim() || uiText('Help me use {0}.', name)
  return <article className="tool-drawer-row">
    <button className="catalog-row" type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}><span>{name} <small>{tool.name}</small></span>
      <span className="catalog-badges">{(tool.ui_only || tool.read_only) &&
        <span className="badge">{tool.ui_only ? uiText("View only") : uiText("Read only")}</span>}
        {!tool.builtin && <span className="badge info">{uiText("My tools")}</span>}
        <Icon name="down" /></span></button>
    {expanded && <div className="catalog-detail">
      <p>{info.description}</p>
      <dl><dt>{uiText("Source")}</dt><dd>{tool.builtin ? uiText("Built-in") : uiText("My tools")}</dd>
        <dt>{uiText("Parameters")}</dt><dd className="param-list">{info.parameters.length ?
          <ParameterTable parameters={info.parameters} /> : '-'}</dd>
        <dt>{uiText("Preconditions")}</dt><dd>{describePreconditions(info.preconditions)}</dd>
        <dt>{uiText("Applies when")}</dt><dd>{info.applies_when?.join(' · ') || '-'}</dd>
        <dt>{uiText("Not for")}</dt><dd>{info.not_for?.join(' · ') || '-'}</dd>
        <dt>Revit</dt><dd>{tool.revit_versions?.join(', ') || uiText("Unspecified")}</dd>
      </dl>
      {failed && !detail && <p role="status">{uiText("Tool detail unavailable. Showing catalog data.")}</p>}
      <button type="button" onClick={() => choose(example)}>{example}</button>
    </div>}
  </article>
}

export default function ToolDrawer({ onExample }: { onExample: (example: string) => void }) {
  const device = useSessionStore(s => s.deviceId)
  const [open, setOpen] = useState(false)
  const [tools, setTools] = useState<ToolInfo[] | null>(null)
  const [error, setError] = useState(false)
  const [functions, setFunctions] = useState<HostFunction[]>([])
  const [workflows, setWorkflows] = useState<Workflow[]>([])
  const [workflowError, setWorkflowError] = useState(false)
  const [functionError, setFunctionError] = useState(false)
  const [query, setQuery] = useState('')
  const [health, setHealth] = useState<RevitHealthResponse | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    let active = true
    workflowApi.list().then(list => { if (active) { setWorkflows(list); setWorkflowError(false) } })
      .catch(() => { if (active) { setWorkflows([]); setWorkflowError(true) } })
    bridgeApi.hostFunctions().then(list => { if (active) { setFunctions(list); setFunctionError(false) } })
      .catch(() => { if (active) setFunctionError(true) })
    bridgeApi.listTools().then(list => { if (active) { setTools(list); setError(false) } })
      .catch(() => { if (active) setError(true) })
    return () => { active = false }
  }, [open, device])
  useEffect(() => {
    if (!open) return
    dialog.current?.showModal()
    let active = true
    bridgeApi.revitHealth().then(result => { if (active) setHealth(result) })
      .catch(() => { if (active) setHealth(null) })
    return () => { active = false }
  }, [open, device])
  const close = () => { dialog.current?.close(); setOpen(false); setQuery(''); setHealth(null) }
  const choose = (text: string) => { onExample(text); close() }
  const shown = (tools ?? []).filter(t => [t.name, t.display_name, t.description, ...(t.applies_when ?? [])]
    .join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const visibleFunctions = functions.filter(f => (f.name + ' ' + f.description).toLowerCase().includes(query.trim().toLowerCase()))
  const visibleWorkflows = workflows.filter(w => [w.name, w.display_name, w.summary, w.description].join(' ')
    .toLowerCase().includes(query.trim().toLowerCase()))
  const total = (tools?.length ?? 0) + functions.length + workflows.length
  const shownCount = shown.length + visibleFunctions.length + visibleWorkflows.length
  const groupOf = (t: ToolInfo) => !t.builtin ? 'My tools' : t.ui_only ? 'View and selection'
    : t.read_only ? 'Read only' : t.read_only === false ? 'Changes the model' : 'Built-in'
  const groups = ['Read only', 'View and selection', 'Changes the model', 'Built-in', 'My tools'].map(key => ({
    label: uiText(key), items: shown.filter(t => groupOf(t) === key),
  }))
  return <>
    <button type="button" className="btn-secondary" onClick={() => setOpen(true)}><Icon name="Tools" />{uiText("Tools")} ({tools ? total : '…'})</button>
    {open && <dialog ref={dialog} className="tool-drawer drawer-shell" aria-label={uiText("Tool catalog")} onCancel={close}>
      <header className="panel-head"><h2><Icon name="Tools" />{uiText("Tool catalog")} <span className="badge">{total}</span></h2>
        <button className="icon-button btn-ghost" type="button" onClick={close}><Icon name="close" /><span className="sr-only">{uiText("Close")}</span></button></header>
      <p>{uiText("Chat can read the project snapshot, levels, types and selection. It says when a tool is missing and uses saved tools only.")}
        {' '}<a href="/capabilities/authoring">{uiText("Create a tool")}</a></p>
      <div className="toolbar"><span className={"badge " + (health?.revit_connected ? "success" : "neutral")}>Revit · {health ? (health.revit_connected ? uiText("Connected") : uiText("Disconnected")) : uiText("Unknown")}
        </span><span className="badge">{uiText("Allow my tools")} · {typeof health?.user_tools_allowed === 'boolean'
          ? (health.user_tools_allowed ? uiText("On") : uiText("Off")) : uiText("Unknown")}</span></div>
      <label>{uiText("Search tools")} <input type="search" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <p role="status">{error ? uiText("Could not refresh the catalog") : tools === null ? uiText("Loading")
        : total === 0 ? uiText("No tools available") : shownCount === 0 ? uiText("No matching tools") : shownCount + ' / ' + total}</p>
      {functionError && <p role="status">{uiText("Host functions could not be loaded.")}</p>}
      {workflowError && <p role="status">{uiText("Workflows could not be loaded.")}</p>}
      <div className="catalog-groups">
      {visibleWorkflows.length > 0 && <section><h3>{uiText("Workflows")}</h3>
        {visibleWorkflows.map(w => <details key={w.name}><summary>{w.display_name} <small>{w.name}</small></summary>
          <p>{w.summary}</p><p>{w.description}</p>
          <button onClick={() => choose(uiText('Run {0}', w.name))}>{uiText("Use in chat")}</button>
        </details>)}</section>}
      {visibleFunctions.length > 0 && <section><h3>{uiText("Host functions")}</h3>
        {visibleFunctions.map(f => <details key={f.name}><summary><code>{f.name}</code> <span className="badge">{f.class === 'ui_only' ? uiText("View only") : uiText("Read only")}</span></summary><p>{f.description}</p>
          {f.parameters.length > 0 && <ParameterTable parameters={f.parameters} />}</details>)}</section>}
      {groups.filter(g => g.items.length).map(g => <section key={g.label}>{g.label && <h3>{g.label}</h3>}
        {g.items.map(t => <ToolRow key={t.name} tool={t} choose={choose} />)}</section>)}
      </div>
    </dialog>}
  </>
}
