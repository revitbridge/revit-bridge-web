import { useEffect, useRef, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { useSessionStore } from '../../store'
import type { HostFunction, RevitHealthResponse, ToolDetail, ToolInfo } from '../../types/api'
import { browserLanguage } from '../../utils/language'
import { describePreconditions } from '../capabilities/directRun'
import './toolDrawer.css'

function ToolRow({ tool, choose, zh }: { tool: ToolInfo; choose: (example: string) => void; zh: boolean }) {
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
  const example = detail?.source_query?.trim() || (zh ? '请帮我使用' + name + '。' : 'Help me use ' + name + '.')
  return <article className="tool-drawer-row">
    <button type="button" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{name} <small>{tool.name}</small></button>
    {expanded && <div>
      <p>{info.description}</p>
      <dl><dt>{zh ? '来源' : 'Source'}</dt><dd>{tool.builtin ? (zh ? '内置' : 'Built-in') : (zh ? '我的工具' : 'My tools')}</dd>
        <dt>{zh ? '参数' : 'Parameters'}</dt><dd>{info.parameters.length ? <ul>{info.parameters.map(p => <li key={p.name}>
          <code>{p.name}</code> {p.unit} · {(p.required ?? !('default' in p)) ? (zh ? '必填' : 'Required') : (zh ? '可选' : 'Optional')}
{p.default !== undefined && <> · {zh ? '默认' : 'Default'} {JSON.stringify(p.default)}</>}
        </li>)}</ul> : '-'}</dd>
        <dt>{zh ? '前置条件' : 'Preconditions'}</dt><dd>{describePreconditions(info.preconditions)}</dd>
        <dt>{zh ? '适用于' : 'Applies when'}</dt><dd>{info.applies_when?.join(' · ') || '-'}</dd>
        <dt>{zh ? '不适用于' : 'Not for'}</dt><dd>{info.not_for?.join(' · ') || '-'}</dd>
        <dt>Revit</dt><dd>{tool.revit_versions?.join(', ') || (zh ? '未声明' : 'Unspecified')}</dd>
      </dl>
      {failed && !detail && <p role="status">{zh ? '详细资料暂不可用，已显示清单数据。' : 'Tool detail unavailable. Showing catalog data.'}</p>}
      <button type="button" onClick={() => choose(example)}>{example}</button>
    </div>}
  </article>
}

export default function ToolDrawer({ onExample }: { onExample: (example: string) => void }) {
  const zh = browserLanguage() === 'zh'
  const device = useSessionStore(s => s.deviceId)
  const [open, setOpen] = useState(false)
  const [tools, setTools] = useState<ToolInfo[] | null>(null)
  const [error, setError] = useState(false)
  const [functions, setFunctions] = useState<HostFunction[]>([])
  const [functionError, setFunctionError] = useState(false)
  const [query, setQuery] = useState('')
  const [health, setHealth] = useState<RevitHealthResponse | null>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    let active = true
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
  const total = (tools?.length ?? 0) + functions.length
  const shownCount = shown.length + visibleFunctions.length
  const groups = grouped ? ['read', 'ui', 'write'].map(group => ({
    label: group === 'read' ? (zh ? '只读' : 'Read only') : group === 'ui' ? (zh ? '仅界面' : 'View and selection') : (zh ? '会修改模型' : 'Changes the model'),
    items: shown.filter(t => (t.ui_only ? 'ui' : t.read_only ? 'read' : 'write') === group),
  })) : [{ label: '', items: shown }]
  return <>
    <button type="button" className="btn-ghost" onClick={() => setOpen(true)}>{zh ? '工具' : 'Tools'} ({tools ? total : '…'})</button>
    {open && <dialog ref={dialog} className="tool-drawer" aria-label={zh ? '工具清单' : 'Tool catalog'} onCancel={close}>
      <header><h2>{zh ? '工具清单' : 'Tool catalog'}</h2><button type="button" onClick={close}>{zh ? '关闭' : 'Close'}</button></header>
      <p>{zh ? '助手可读取项目快照、标高、类型和选择集，并使用下列工具。超出范围时会说明缺少工具。聊天里不编写代码。'
        : 'Chat can read the project snapshot, levels, types and selection. It says when a tool is missing and uses saved tools only.'}
        {' '}<a href="/capabilities/authoring">{zh ? '编写工具' : 'Create a tool'}</a></p>
      <p>Revit · {health ? (health.revit_connected ? (zh ? '已连接' : 'Connected') : (zh ? '未连接' : 'Disconnected')) : (zh ? '状态未知' : 'Unknown')}
        {' · '}{zh ? '允许我的工具' : 'Allow my tools'} · {typeof health?.user_tools_allowed === 'boolean'
          ? (health.user_tools_allowed ? (zh ? '开' : 'On') : (zh ? '关' : 'Off')) : (zh ? '未知' : 'Unknown')}</p>
      <label>{zh ? '搜索工具' : 'Search tools'} <input type="search" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <p role="status">{error ? (zh ? '无法刷新清单' : 'Could not refresh the catalog') : tools === null ? (zh ? '加载中' : 'Loading')
        : total === 0 ? (zh ? '暂无工具' : 'No tools available') : shownCount === 0 ? (zh ? '无匹配工具' : 'No matching tools') : shownCount + ' / ' + total}</p>
      {functionError && <p role="status">{zh ? '无法加载宿主函数。' : 'Host functions could not be loaded.'}</p>}
      {visibleFunctions.length > 0 && <section><h3>{zh ? '宿主函数' : 'Host functions'}</h3>
        {visibleFunctions.map(f => <details key={f.name}><summary>{f.name}</summary><p>{f.description}</p>
          <p>{f.class === 'ui_only' ? (zh ? '仅界面' : 'View only') : (zh ? '只读' : 'Read only')}</p>
          {f.parameters.map(p => <div key={p.name}><code>{p.name}</code> · {p.type} · {p.required
            ? (zh ? '必填' : 'Required') : (zh ? '可选' : 'Optional')}</div>)}</details>)}</section>}
      {groups.filter(g => g.items.length).map(g => <section key={g.label}>{g.label && <h3>{g.label}</h3>}
        {g.items.map(t => <ToolRow key={t.name} tool={t} choose={choose} zh={zh} />)}</section>)}
    </dialog>}
  </>
}
