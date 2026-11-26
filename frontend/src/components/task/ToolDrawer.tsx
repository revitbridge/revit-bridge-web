import { uiText } from '../../utils/strings'
import { useEffect, useRef, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { workflowApi, type Workflow } from '../../api/workflows'
import { useSessionStore } from '../../store'
import type { HostFunction, RevitHealthResponse, ToolDetail, ToolInfo } from '../../types/api'
import { describePreconditions } from '../capabilities/directRun'
import './toolDrawer.css'

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
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{name} <small>{tool.name}</small></button>
    {expanded && <div>
      <p>{info.description}</p>
      <dl><dt>{uiText("Source")}</dt><dd>{tool.builtin ? uiText("Built-in") : uiText("My tools")}</dd>
        <dt>{uiText("Parameters")}</dt><dd>{info.parameters.length ? <ul>{info.parameters.map(p => <li key={p.name}>
          <code>{p.name}</code> {p.unit} · {(p.required ?? !('default' in p)) ? uiText("Required") : uiText("Optional")}
{p.default !== undefined && <> · {uiText("Default")} {JSON.stringify(p.default)}</>}
        </li>)}</ul> : '-'}</dd>
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
  const grouped = tools?.length && tools.every(t => typeof t.read_only === 'boolean' && t.builtin)
  const visibleFunctions = functions.filter(f => (f.name + ' ' + f.description).toLowerCase().includes(query.trim().toLowerCase()))
  const visibleWorkflows = workflows.filter(w => [w.name, w.display_name, w.summary, w.description].join(' ')
    .toLowerCase().includes(query.trim().toLowerCase()))
  const total = (tools?.length ?? 0) + functions.length + workflows.length
  const shownCount = shown.length + visibleFunctions.length + visibleWorkflows.length
  const groups = grouped ? ['read', 'ui', 'write'].map(group => ({
    label: group === 'read' ? uiText("Read only") : group === 'ui' ? uiText("View and selection") : uiText("Changes the model"),
    items: shown.filter(t => (t.ui_only ? 'ui' : t.read_only ? 'read' : 'write') === group),
  })) : [{ label: '', items: shown }]
  return <>
    <button type="button" className="btn-ghost" onClick={() => setOpen(true)}>{uiText("Tools")} ({tools ? total : '…'})</button>
    {open && <dialog ref={dialog} className="tool-drawer" aria-label={uiText("Tool catalog")} onCancel={close}>
      <header><h2>{uiText("Tool catalog")}</h2><button type="button" onClick={close}>{uiText("Close")}</button></header>
      <p>{uiText("Chat can read the project snapshot, levels, types and selection. It says when a tool is missing and uses saved tools only.")}
        {' '}<a href="/capabilities/authoring">{uiText("Create a tool")}</a></p>
      <p>Revit · {health ? (health.revit_connected ? uiText("Connected") : uiText("Disconnected")) : uiText("Unknown")}
        {' · '}{uiText("Allow my tools")} · {typeof health?.user_tools_allowed === 'boolean'
          ? (health.user_tools_allowed ? uiText("On") : uiText("Off")) : uiText("Unknown")}</p>
      <label>{uiText("Search tools")} <input type="search" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <p role="status">{error ? uiText("Could not refresh the catalog") : tools === null ? uiText("Loading")
        : total === 0 ? uiText("No tools available") : shownCount === 0 ? uiText("No matching tools") : shownCount + ' / ' + total}</p>
      {functionError && <p role="status">{uiText("Host functions could not be loaded.")}</p>}
      {workflowError && <p role="status">{uiText("Workflows could not be loaded.")}</p>}
      {visibleWorkflows.length > 0 && <section><h3>{uiText("Workflows")}</h3>
        {visibleWorkflows.map(w => <details key={w.name}><summary>{w.display_name} <small>{w.name}</small></summary>
          <p>{w.summary}</p><p>{w.description}</p>
          <button onClick={() => choose(uiText('Run {0}', w.name))}>{uiText("Use in chat")}</button>
        </details>)}</section>}
      {visibleFunctions.length > 0 && <section><h3>{uiText("Host functions")}</h3>
        {visibleFunctions.map(f => <details key={f.name}><summary>{f.name}</summary><p>{f.description}</p>
          <p>{f.class === 'ui_only' ? uiText("View only") : uiText("Read only")}</p>
          {f.parameters.map(p => <div key={p.name}><code>{p.name}</code> · {p.type} · {p.required
            ? uiText("Required") : uiText("Optional")}</div>)}</details>)}</section>}
      {groups.filter(g => g.items.length).map(g => <section key={g.label}>{g.label && <h3>{g.label}</h3>}
        {g.items.map(t => <ToolRow key={t.name} tool={t} choose={choose} />)}</section>)}
    </dialog>}
  </>
}
