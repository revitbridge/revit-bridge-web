import { useEffect, useId, useState } from 'react'
import { workflowApi, type Binding, type WorkflowInput, type WorkflowStep } from '../../api/workflows'
import type { ToolParam } from '../../types/api'

interface ParamProps {
  param: ToolParam
  binding?: Binding
  inputs: WorkflowInput[]
  earlier: WorkflowStep[]
  change: (value?: Binding) => void
  describe: (name: string, description: string) => void
  zh: boolean
}

function ResultPath({ tool, path, change, zh }: {
  tool?: string; path: string; change: (path: string) => void; zh: boolean
}) {
  const id = useId()
  const [paths, setPaths] = useState<string[]>([])
  useEffect(() => {
    let active = true
    if (tool) workflowApi.resultPaths(tool).then(p => { if (active) setPaths(p) })
      .catch(() => { if (active) setPaths([]) })
    return () => { active = false }
  }, [tool])
  return <label>{zh ? '结果路径' : 'Result path'}
    <input list={id} value={path} onChange={e => change(e.target.value)} placeholder="ElementId" />
    <datalist id={id}>{paths.map(p => <option key={p} value={p} />)}</datalist>
  </label>
}

export function ParameterEditor({ param, binding, inputs, earlier, change, describe, zh }: ParamProps) {
  const id = useId()
  const mode = binding === undefined ? 'omit' : typeof binding !== 'object' ? 'fixed' : 'input' in binding ? 'input' : 'from'
  const bool = ['bool', 'boolean'].includes(param.type ?? '')
  const numeric = !bool && !!param.type && param.type !== 'string'
  function setMode(next: string) {
    if (next === 'omit') change(undefined)
    if (next === 'input') change({ input: param.name })
    if (next === 'from') change({ from: earlier[0]?.id ?? '', path: '' })
    if (next === 'fixed') {
      const v = param.default
      change(typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean' ? v : bool ? false : '')
    }
  }
  return <fieldset className="workflow-param"><legend><code>{param.name}</code> {param.unit}</legend>
    <label>{zh ? '参数来源' : 'Value source'}
      <select value={mode} onChange={e => setMode(e.target.value)}>
        {(!param.required || mode === 'omit') && <option value="omit">{zh ? '未指定' : 'Unspecified'}</option>}
        <option value="fixed">{zh ? '固定值' : 'Fixed value'}</option>
        <option value="input">{zh ? '运行时询问' : 'Ask at run time'}</option>
        <option value="from" disabled={!earlier.length}>{zh ? '前一步的结果' : 'Earlier result'}</option>
      </select></label>
    {mode === 'fixed' && <label>{zh ? '固定值' : 'Fixed value'}
      {bool ? <select value={String(binding)} onChange={e => change(e.target.value === 'true')}>
        <option value="true">true</option><option value="false">false</option></select> :
        <input type={numeric ? 'number' : 'text'} step="any" value={String(binding ?? '')} onChange={e =>
          change(numeric && e.target.value !== '' ? e.target.valueAsNumber : e.target.value)} />}
    </label>}
    {typeof binding === 'object' && 'input' in binding && <><label>{zh ? '输入名称' : 'Input name'}
      <input list={id} value={binding.input} onChange={e => change({ input: e.target.value })} />
      <datalist id={id}>{inputs.filter(i => i.type === param.type).map(i => <option key={i.name} value={i.name} />)}</datalist>
    </label><label>{zh ? '运行时问题' : 'Run-time question'}
      <input value={inputs.find(i => i.name === binding.input)?.description ?? ''}
        onChange={e => describe(binding.input, e.target.value)} />
    </label></>}
    {typeof binding === 'object' && 'from' in binding && <>
      <label>{zh ? '来源步骤' : 'Source step'}<select value={binding.from} onChange={e => change({ from: e.target.value, path: '' })}>
        {!earlier.some(s => s.id === binding.from) && <option value={binding.from}>{binding.from || '—'}</option>}
        {earlier.map(s => <option key={s.id} value={s.id}>{s.title} · {s.id}</option>)}
      </select></label>
      <ResultPath key={binding.from} tool={earlier.find(s => s.id === binding.from)?.tool} path={binding.path}
        change={path => change({ ...binding, path })} zh={zh} />
    </>}
  </fieldset>
}
