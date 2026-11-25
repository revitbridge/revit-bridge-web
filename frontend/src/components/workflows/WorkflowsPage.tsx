import { useEffect, useRef, useState, type DragEvent } from 'react'
import { bridgeApi } from '../../api/bridge'
import { workflowApi, type Binding, type Workflow, type WorkflowStep } from '../../api/workflows'
import { useSessionStore } from '../../store'
import type { ToolInfo } from '../../types/api'
import { browserLanguage } from '../../utils/language'
import { getErrorMessage } from '../../utils/errors'
import useHostFeatures from '../shared/useHostFeatures'
import { ParameterEditor } from './StepEditor'
import './workflows.css'

function blank(): Workflow {
  return { schema_version: 1, kind: 'workflow', name: '', display_name: '', summary: '', description: '',
    version: '1.0.0', inputs: [], steps: [] }
}

function inputNames(steps: WorkflowStep[]) {
  return new Set(steps.flatMap(step => Object.values(step.params).flatMap(source =>
    typeof source === 'object' && 'input' in source ? [source.input] : [])))
}

function withSteps(draft: Workflow, steps: WorkflowStep[], tools: ToolInfo[]): Workflow {
  const names = inputNames(steps)
  const previouslyUsed = inputNames(draft.steps)
  const inputs = draft.inputs.filter(i => names.has(i.name) || !previouslyUsed.has(i.name))
  for (const step of steps) {
    const params = tools.find(t => t.name === step.tool)?.parameters
    for (const [key, source] of Object.entries(step.params)) {
      if (typeof source !== 'object' || !('input' in source) || inputs.some(i => i.name === source.input)) continue
      const param = params?.find(p => p.name === key)
      const description = param?.description || (param?.unit ? key + ' (' + param.unit + ')' : key)
      inputs.push({ name: source.input, type: param?.type ?? 'string', description, ask: true })
    }
  }
  return { ...draft, steps, inputs }
}

function Editor() {
  const zh = browserLanguage() === 'zh'
  const features = useHostFeatures()
  const writable = features.mode === 'local' || features.authoring
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [saved, setSaved] = useState<Workflow[]>([])
  const [selected, setSelected] = useState('')
  const [draft, setDraft] = useState<Workflow>(blank)
  const [query, setQuery] = useState('')
  const [check, setCheck] = useState<{ draft: Workflow; errors: string[] }>()
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [overwrite, setOverwrite] = useState(false)
  const file = useRef<HTMLInputElement>(null)
  const nextId = useRef(1)
  useEffect(() => {
    let active = true
    Promise.all([bridgeApi.listTools(), workflowApi.list()]).then(([packs, flows]) => {
      if (active) { setTools(packs); setSaved(flows) }
    }).catch(e => { if (active) setNotice(getErrorMessage(e)) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    let active = true
    const timer = setTimeout(() => {
      workflowApi.validate(draft).then(({ errors }) => { if (active) setCheck({ draft, errors }) })
        .catch(e => { if (active) setCheck({ draft, errors: [getErrorMessage(e)] }) })
    }, 200)
    return () => { active = false; clearTimeout(timer) }
  }, [draft])
  function select(w?: Workflow) {
    setDraft(w ?? blank()); setSelected(w?.name ?? ''); setNotice(''); setOverwrite(false)
  }
  async function action(work: () => Promise<void>) {
    setBusy(true); setNotice('')
    try { await work() } catch (e) { setNotice(getErrorMessage(e)) } finally { setBusy(false) }
  }
  function loaded(w: Workflow) {
    select(w)
    setSaved(all => [...all.filter(old => old.name !== w.name), w].sort((a, b) => a.name.localeCompare(b.name)))
    setNotice(zh ? '已保存' : 'Saved')
  }
  const editSteps = (steps: WorkflowStep[]) => setDraft(withSteps(draft, steps, tools))
  function add(tool: ToolInfo, at = draft.steps.length) {
    if (draft.steps.length >= 8) return
    let id: string
    do { id = 'step_' + nextId.current++ } while (draft.steps.some(s => s.id === id))
    const params = Object.fromEntries(tool.parameters.filter(p => p.required ?? !('default' in p)).map(p => [p.name, { input: p.name }]))
    const steps = [...draft.steps]
    steps.splice(at, 0, { id, tool: tool.name, title: tool.display_name || tool.name, params })
    editSteps(steps)
  }
  function move(from: number, to: number) {
    const steps = [...draft.steps]
    if (from < 0 || from >= steps.length || to < 0 || from === to) return
    const [step] = steps.splice(from, 1)
    steps.splice(to, 0, step)
    editSteps(steps)
  }
  function drop(event: DragEvent, to: number) {
    event.preventDefault(); event.stopPropagation()
    const pack = tools.find(t => t.name === event.dataTransfer.getData('application/x-workflow-tool'))
    if (pack) add(pack, to)
    else move(draft.steps.findIndex(s => s.id === event.dataTransfer.getData('application/x-workflow-step')), to)
  }
  function updateStep(id: string, update: Partial<WorkflowStep>) {
    editSteps(draft.steps.map(s => s.id === id ? { ...s, ...update } : s))
  }
  function bind(step: WorkflowStep, key: string, value?: Binding) {
    const params = { ...step.params, [key]: value }
    if (value === undefined) delete params[key]
    updateStep(step.id, { params: params as Record<string, Binding> })
  }
  const replacing = saved.some(w => w.name === draft.name)
  const errors = check?.draft === draft ? check.errors : null
  return <div className="page workflows-page">
    <h2>{zh ? '工作流' : 'Workflows'}</h2>
    <fieldset disabled={busy} className="workflow-editor">
      <div className="workflow-actions">
        <label>{zh ? '已保存的工作流' : 'Saved workflows'}<select value={selected} onChange={e => select(saved.find(w => w.name === e.target.value))}>
          <option value="">{zh ? '新工作流' : 'New workflow'}</option>
          {saved.map(w => <option key={w.name} value={w.name}>{w.display_name} · {w.name}</option>)}</select></label>
        <button disabled={!selected} onClick={() => void action(async () => {
          const yaml = await workflowApi.exportYaml(selected)
          const url = URL.createObjectURL(new Blob([yaml], { type: 'text/yaml' }))
          const a = document.createElement('a'); a.href = url; a.download = selected + '.yaml'; a.click()
          setTimeout(() => URL.revokeObjectURL(url), 1000)
        })}>{zh ? '导出 YAML' : 'Export YAML'}</button>
        {writable && <>
          <button onClick={() => file.current?.click()}>{zh ? '导入 YAML' : 'Import YAML'}</button>
          <input hidden ref={file} type="file" accept=".yaml,.yml,text/yaml" onChange={e => {
            const source = e.target.files?.[0]; e.target.value = ''
            if (source) void action(async () => loaded(await workflowApi.importYaml(await source.text(), overwrite)))
          }} />
          <button disabled={!selected} onClick={() => {
            if (window.confirm((zh ? '删除工作流 ' : 'Delete workflow ') + selected + '?')) void action(async () => {
              await workflowApi.remove(selected); setSaved(all => all.filter(w => w.name !== selected)); select()
            })
          }}>{zh ? '删除' : 'Delete'}</button>
        </>}
      </div>
      <div className="workflow-meta">
        {([['name', zh ? '名称' : 'Name'], ['display_name', zh ? '显示名称' : 'Display name'],
          ['version', zh ? '版本' : 'Version'], ['summary', zh ? '摘要' : 'Summary']] as const).map(([key, label]) =>
          <label key={key}>{label}<input value={draft[key]} maxLength={key === 'summary' ? 120 : undefined}
            onChange={e => setDraft({ ...draft, [key]: e.target.value })} /></label>)}
        <label>{zh ? '说明' : 'Description'}<textarea value={draft.description}
          onChange={e => setDraft({ ...draft, description: e.target.value })} /></label>
      </div>
      <div className="workflow-columns"><section>
        <h3>{zh ? '工具' : 'Tools'}</h3>
        <label>{zh ? '搜索工具' : 'Search packs'}<input type="search" value={query} onChange={e => setQuery(e.target.value)} /></label>
        <ul className="workflow-packs">{tools.filter(t => [t.name, t.display_name, t.description].join(' ').toLowerCase()
          .includes(query.trim().toLowerCase())).map(t => <li key={t.name} draggable
            onDragStart={e => e.dataTransfer.setData('application/x-workflow-tool', t.name)}>
            <span>{t.display_name || t.name}<small>{t.display_name && t.name}</small></span>
            <button disabled={draft.steps.length >= 8} onClick={() => add(t)}>{zh ? '加入' : 'Add'}</button>
          </li>)}</ul>
      </section><section className="workflow-steps" onDragOver={e => e.preventDefault()} onDrop={e => drop(e, draft.steps.length)}>
        <h3>{zh ? '步骤' : 'Steps'}</h3>
        {!draft.steps.length && <p>{zh ? '拖入工具，或使用“加入”按钮。' : 'Drag a pack here or choose Add.'}</p>}
        <ol>{draft.steps.map((s, index) => <li key={s.id} onDrop={e => drop(e, index)}>
          <div className="workflow-step-heading"><strong draggable tabIndex={0} aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown"
            onDragStart={e => e.dataTransfer.setData('application/x-workflow-step', s.id)} onKeyDown={e => {
              if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
                e.preventDefault(); move(index, index + (e.key === 'ArrowUp' ? -1 : 1))
              }
            }} title={zh ? '拖动或按 Alt + 上下方向键排序' : 'Drag or press Alt + Up or Down to reorder'}>{index + 1}. {s.tool}</strong>
            <button disabled={!index} onClick={() => move(index, index - 1)}>{zh ? '上移' : 'Move up'}</button>
            <button disabled={index === draft.steps.length - 1} onClick={() => move(index, index + 1)}>{zh ? '下移' : 'Move down'}</button>
            <button onClick={() => editSteps(draft.steps.filter(step => step !== s))}>{zh ? '移除' : 'Remove'}</button></div>
          <label>{zh ? '步骤标题' : 'Step title'}<input value={s.title} onChange={e => updateStep(s.id, { title: e.target.value })} /></label>
          {tools.find(t => t.name === s.tool)?.parameters.map(p => <ParameterEditor key={p.name} param={p}
            binding={Object.hasOwn(s.params, p.name) ? s.params[p.name] : undefined} inputs={draft.inputs}
            describe={(name, description) => setDraft({ ...draft, inputs: draft.inputs.map(i => i.name === name ? { ...i, description } : i) })}
            earlier={draft.steps.slice(0, index)} change={v => bind(s, p.name, v)} zh={zh} />)}
        </li>)}</ol>
      </section></div>
      <div aria-live="polite">{errors?.length ? <ul>{errors.map((error, index) => <li key={index}>{error}</li>)}</ul>
        : <p>{errors ? (zh ? '校验通过' : 'Validation passed') : (zh ? '校验中…' : 'Validating…')}</p>}</div>
      {writable && <div className="workflow-actions"><label><input type="checkbox" checked={overwrite}
        onChange={e => setOverwrite(e.target.checked)} />{zh ? '允许替换同名工作流' : 'Allow replacing a saved workflow'}</label>
        <button className="btn-primary" disabled={!errors || !!errors.length || (replacing && !overwrite)}
          onClick={() => void action(async () => loaded(await workflowApi.save(draft, overwrite)))}>{zh ? '保存工作流' : 'Save workflow'}</button></div>}
      {notice && <p role="status">{notice}</p>}
    </fieldset>
  </div>
}

export default function WorkflowsPage() {
  const device = useSessionStore(s => s.deviceId)
  return <Editor key={device} />
}
