import { uiText } from '../../utils/strings'
import { useEffect, useId, useState } from 'react'
import SegmentedSelect from '../shared/SegmentedSelect'
import { workflowApi, type Binding, type WorkflowInput, type WorkflowStep } from '../../api/workflows'
import type { ToolParam } from '../../types/api'

interface ParamProps {
  param: ToolParam
  binding?: Binding
  inputs: WorkflowInput[]
  earlier: WorkflowStep[]
  change: (value?: Binding) => void
  describe: (name: string, description: string) => void
}

function ResultPath({ tool, path, change }: {
  tool?: string; path: string; change: (path: string) => void
}) {
  const id = useId()
  const [paths, setPaths] = useState<string[]>([])
  useEffect(() => {
    let active = true
    if (tool) workflowApi.resultPaths(tool).then(p => { if (active) setPaths(p) })
      .catch(() => { if (active) setPaths([]) })
    return () => { active = false }
  }, [tool])
  return <label>{uiText("Result path")}
    <input list={id} value={path} onChange={e => change(e.target.value)} placeholder="ElementId" />
    <datalist id={id}>{paths.map(p => <option key={p} value={p} />)}</datalist>
  </label>
}

export function ParameterEditor({ param, binding, inputs, earlier, change, describe }: ParamProps) {
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
    <SegmentedSelect label={uiText("Value source")} value={mode} change={setMode} options={[
      ...(!param.required || mode === 'omit' ? [{ value: 'omit', label: uiText("Unspecified") }] : []),
      { value: 'fixed', label: uiText("Fixed value") }, { value: 'input', label: uiText("Ask at run time") },
      { value: 'from', label: uiText("Earlier result"), disabled: !earlier.length },
    ]} />
    {mode === 'fixed' && <label>{uiText("Fixed value")}
      {bool ? <select value={String(binding)} onChange={e => change(e.target.value === 'true')}>
        <option value="true">true</option><option value="false">false</option></select> :
        <input type={numeric ? 'number' : 'text'} step="any" value={String(binding ?? '')} onChange={e =>
          change(numeric && e.target.value !== '' ? e.target.valueAsNumber : e.target.value)} />}
    </label>}
    {typeof binding === 'object' && 'input' in binding && <><label>{uiText("Input name")}
      <input list={id} value={binding.input} onChange={e => change({ input: e.target.value })} />
      <datalist id={id}>{inputs.filter(i => i.type === param.type).map(i => <option key={i.name} value={i.name} />)}</datalist>
    </label><label>{uiText("Run-time question")}
      <input value={inputs.find(i => i.name === binding.input)?.description ?? ''}
        onChange={e => describe(binding.input, e.target.value)} />
    </label></>}
    {typeof binding === 'object' && 'from' in binding && <>
      <label>{uiText("Source step")}<select value={binding.from} onChange={e => change({ from: e.target.value, path: '' })}>
        {!earlier.some(s => s.id === binding.from) && <option value={binding.from}>{binding.from || '—'}</option>}
        {earlier.map(s => <option key={s.id} value={s.id}>{s.title} · {s.id}</option>)}
      </select></label>
      <ResultPath key={binding.from} tool={earlier.find(s => s.id === binding.from)?.tool} path={binding.path}
        change={path => change({ ...binding, path })} />
    </>}
  </fieldset>
}
