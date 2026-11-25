import { useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { llmHeaders, useSessionStore } from '../../store'
import { post } from '../shared/http'
import { readCheck, readDryRun } from '../hostv2/workbench/results'
import { getErrorMessage } from '../../utils/errors'
import { browserLanguage } from '../../utils/language'
import type { ToolInfo } from '../../types/api'
import './authoring.css'

const base = '/api/v1/bridge'
type Pack = Record<string, unknown>
interface Draft { pack: Pack; problems: string[] }
type Step = 'draft' | 'check' | 'trial' | 'saved'
const usableFields = new Set(['schema_version', 'version', 'name', 'code_template', 'parameters', 'description',
  'source_query', 'validator'])

function object(text: string): Pack {
  const parsed = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Expected a JSON object.')
  return parsed
}

function Editor() {
  const zh = browserLanguage() === 'zh'
  const [brief, setBrief] = useState('')
  const [based, setBased] = useState('')
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [meta, setMeta] = useState('')
  const [code, setCode] = useState('')
  const [params, setParams] = useState('[]')
  const [values, setValues] = useState('{}')
  const [stage, setStage] = useState<Step>('draft')
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState('')
  const [catalogError, setCatalogError] = useState('')
  useEffect(() => {
    let current = true
    bridgeApi.listTools().then(items => { if (current) setTools(items) })
      .catch(e => { if (current) setCatalogError(getErrorMessage(e)) })
    return () => { current = false }
  }, [])
  const edited = (set: (text: string) => void, text: string) => { set(text); setStage('draft'); setResult(''); setReplace(false) }
  async function action(kind: Step) {
    if (busy || (kind === 'trial' && stage !== 'check') || (kind === 'saved' && stage !== 'trial')) return
    setBusy(true)
    setResult('')
    try {
      if (kind === 'draft') {
        const draft = await post<Draft>(base + '/tools/draft', { description: brief,
          ...(based ? { based_on: based } : {}) }, llmHeaders())
        const { code_template, parameters, ...rest } = draft.pack
        setMeta(JSON.stringify(rest, null, 2)); setCode(typeof code_template === 'string' ? code_template : '')
        setParams(JSON.stringify(parameters ?? [], null, 2)); setValues('{}'); setStage('draft'); setReplace(false)
        setResult(draft.problems.join('\n'))
        return
      }
      const pack = { ...object(meta), code_template: code, parameters: JSON.parse(params) } as Pack
      const inputs = object(values)
      if (!Array.isArray(pack.parameters)) throw new Error('Parameters must be a JSON array.')
      if (kind === 'check') {
        setStage('draft')
        const unsupported = Object.keys(pack).filter(k => !usableFields.has(k))
        if (unsupported.length || pack.version !== '1.0.0') throw new Error(zh
          ? '请在能力包字段中设置 version 为 1.0.0，并移除当前保存接口无法保留的字段：' + unsupported.join(', ')
          : 'In Pack fields set version to 1.0.0 and remove fields the save endpoint cannot retain: ' + unsupported.join(', '))
        if (tools.some(t => t.builtin && t.name.toLowerCase() === String(pack.name).toLowerCase()))
          throw new Error(zh ? '内置工具只读，请修改名称。' : 'Built-in tools are read-only. Choose a new name.')
        const sample = Object.fromEntries(pack.parameters.map(p => [p.name, inputs[p.name] ?? p.default ??
          (['int', 'integer', 'float', 'double', 'number'].includes(p.type) ? 0 : ['bool', 'boolean'].includes(p.type) ? false : '')]))
        const checked = readCheck(await post(base + '/check', { pack, values: sample }))
        setResult([checked.reason, ...checked.diagnostics.map(d => [d.id, d.message].join(' ')), ...checked.warnings]
          .filter(Boolean).join('\n') || (checked.compiled ? (zh ? '编译通过' : 'Compiled') : (zh ? '编译失败' : 'Compile failed')))
        if (checked.checked && checked.compiled === true && !checked.blocked) setStage('check')
      } else if (kind === 'trial') {
        const trial = readDryRun(await post(base + '/dry-run', { pack, values: inputs }))
        setResult(JSON.stringify({ rolled_back: trial.rolled_back, changes: trial.changes, error: trial.error,
          diagnostics: trial.diagnostics, failures: trial.failures, result: trial.result }, null, 2))
        if (trial.success && trial.rolled_back === true) setStage('trial')
      } else {
        const existing = (await bridgeApi.listTools()).find(t => t.name.toLowerCase() === String(pack.name).toLowerCase())
        if (existing?.builtin) throw new Error(zh ? '内置工具只读。' : 'Built-in tools are read-only.')
        if (existing && !replace) {
          setReplace(true)
          setResult((zh ? '已存在同名的我的工具：' : 'A saved user tool has this name: ') + existing.name)
          return
        }
        const saved = await post<{ name: string }>(base + '/solidify', { name: pack.name, code,
          description: pack.description, parameters: pack.parameters, source_query: pack.source_query, validator: pack.validator })
        setStage('saved'); setResult((zh ? '已收入工具清单 · ' : 'Saved to tools · ') + saved.name)
      }
    } catch (e) { setResult(getErrorMessage(e)) }
    finally { setBusy(false) }
  }
  return <section className="card authoring-page"><h2>{zh ? '工具编写' : 'Tool authoring'}</h2>
    <fieldset disabled={busy}><div className="authoring-columns">
      <div><label>{zh ? '描述工具' : 'Describe the tool'}<textarea value={brief} onChange={e => setBrief(e.target.value)} /></label>
        <label>{zh ? '参考工具' : 'Reference pack'}<select value={based} onChange={e => setBased(e.target.value)}>
          <option value="">{zh ? '无' : 'None'}</option>{tools.map(t => <option key={t.name} value={t.name}>{t.display_name || t.name}</option>)}
        </select></label>
        {catalogError && <p role="alert">{catalogError}</p>}
        <button type="button" disabled={!brief.trim()} onClick={() => action('draft')}>{zh ? '起草' : 'Generate draft'}</button>
        {meta && <><label>{zh ? '能力包字段 (JSON)' : 'Pack fields (JSON)'}<textarea value={meta} onChange={e => edited(setMeta, e.target.value)} /></label>
          <label>{zh ? '参数 (JSON)' : 'Parameters (JSON)'}<textarea value={params} onChange={e => edited(setParams, e.target.value)} /></label>
          <label>{zh ? '试运行参数值 (JSON)' : 'Trial values (JSON)'}<textarea value={values} onChange={e => edited(setValues, e.target.value)} /></label></>}
      </div>
      <div>{meta && <><label>{zh ? '代码模板' : 'C# template'}<textarea className="authoring-code" spellCheck={false}
          value={code} onChange={e => edited(setCode, e.target.value)} /></label>
        <div className="authoring-actions"><button type="button" onClick={() => action('check')}>{zh ? '编译检查' : 'Compile check'}</button>
        <button type="button" disabled={stage !== 'check'} onClick={() => action('trial')}>{zh ? '试运行并回滚' : 'Dry run and roll back'}</button>
        <button type="button" disabled={stage !== 'trial'} onClick={() => action('saved')}>
          {replace ? (zh ? '确认替换我的工具' : 'Confirm replacing my tool') : (zh ? '收入工具清单' : 'Save to tools')}</button></div></>}
        {result && <pre role="status">{result}</pre>}
      </div>
    </div></fieldset></section>
}

export default function AuthoringPage() {
  const scope = useSessionStore(s => s.deviceId)
  return <Editor key={scope} />
}
