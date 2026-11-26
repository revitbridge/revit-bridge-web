import { uiText } from '../../utils/strings'
import { useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import { llmHeaders, useSessionStore } from '../../store'
import { get, post } from '../shared/http'
import { readCheck, readDryRun } from '../hostv2/workbench/results'
import { getErrorMessage } from '../../utils/errors'
import { browserLanguage } from '../../utils/language'
import type { ToolInfo } from '../../types/api'
import './authoring.css'

const base = '/api/v1/bridge'
type Pack = Record<string, unknown>
interface Draft { pack: Pack; problems: string[] }
interface Trial { evidence_id: string; time: string; code_sha256: string; rolled_back: boolean }
type Step = 'draft' | 'check' | 'trial' | 'saved'
const usableFields = new Set(['schema_version', 'version', 'name', 'code_template', 'parameters', 'description',
  'source_query', 'validator'])

function object(text: string): Pack {
  const parsed = JSON.parse(text)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
    throw new Error(uiText("Expected a JSON object."))
  return parsed
}

function Editor({ initialBrief }: { initialBrief?: string }) {
  const zh = browserLanguage() === 'zh'
  const [brief, setBrief] = useState(initialBrief ?? '')
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
  const [trials, setTrials] = useState<Trial[]>([])
  const [trialsError, setTrialsError] = useState('')
  const [refresh, setRefresh] = useState(0)
  useEffect(() => {
    let active = true
    get<{ trials: Trial[] }>(base + '/remote-trials').then(data => {
      if (active) { setTrials(data.trials); setTrialsError('') }
    }, err => { if (active) setTrialsError(getErrorMessage(err)) })
    return () => { active = false }
  }, [refresh])
  useEffect(() => {
    let current = true
    bridgeApi.listTools().then(items => { if (current) setTools(items) })
      .catch(e => { if (current) setCatalogError(getErrorMessage(e)) })
    return () => { current = false }
  }, [])
  const edited = (set: (text: string) => void, text: string) => { set(text); setStage('draft'); setResult(''); setReplace(false) }
  async function loadTrial(trial: Trial) {
    setBusy(true)
    try {
      const draft = await get<{ code: string }>(base + '/remote-trials/' + encodeURIComponent(trial.evidence_id))
      setMeta(JSON.stringify({ schema_version: 1, version: '1.0.0', name: 'trial_' + trial.code_sha256.slice(0, 12),
        description: '' }, null, 2))
      setCode(draft.code); setParams('[]'); setValues('{}'); setStage('draft'); setReplace(false)
      setResult(uiText("Enter a tool name and description, then run Compile check."))
    } catch (e) { setTrialsError(getErrorMessage(e)) } finally { setBusy(false) }
  }
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
      if (!Array.isArray(pack.parameters)) throw new Error(uiText("Parameters must be a JSON array."))
      if (kind === 'check') {
        setStage('draft')
        const unsupported = Object.keys(pack).filter(k => !usableFields.has(k))
        if (unsupported.length || pack.version !== '1.0.0') throw new Error(zh
          ? '请在能力包字段中设置 version 为 1.0.0，并移除当前保存接口无法保留的字段：' + unsupported.join(', ')
          : 'In Pack fields set version to 1.0.0 and remove fields the save endpoint cannot retain: ' + unsupported.join(', '))
        if (tools.some(t => t.builtin && t.name.toLowerCase() === String(pack.name).toLowerCase()))
          throw new Error(uiText("Built-in tools are read-only. Choose a new name."))
        const sample = Object.fromEntries(pack.parameters.map(p => [p.name, inputs[p.name] ?? p.default ??
          (['int', 'integer', 'float', 'double', 'number'].includes(p.type) ? 0 : ['bool', 'boolean'].includes(p.type) ? false : '')]))
        const checked = readCheck(await post(base + '/check', { pack, values: sample }))
        setResult([checked.reason, ...checked.diagnostics.map(d => [d.id, d.message].join(' ')), ...checked.warnings]
          .filter(Boolean).join('\n') || (checked.compiled ? uiText("Compiled") : uiText("Compile failed")))
        if (checked.checked && checked.compiled === true && !checked.blocked) setStage('check')
      } else if (kind === 'trial') {
        const trial = readDryRun(await post(base + '/dry-run', { pack, values: inputs }))
        setResult(JSON.stringify({ rolled_back: trial.rolled_back, changes: trial.changes, error: trial.error,
          diagnostics: trial.diagnostics, failures: trial.failures, result: trial.result }, null, 2))
        if (trial.success && trial.rolled_back === true) setStage('trial')
      } else {
        const existing = (await bridgeApi.listTools()).find(t => t.name.toLowerCase() === String(pack.name).toLowerCase())
        if (existing?.builtin) throw new Error(uiText("Built-in tools are read-only."))
        if (existing && !replace) {
          setReplace(true)
          setResult(uiText("A saved user tool has this name: ") + existing.name)
          return
        }
        const saved = await post<{ name: string }>(base + '/solidify', { name: pack.name, code,
          description: pack.description, parameters: pack.parameters, source_query: pack.source_query, validator: pack.validator })
        setStage('saved'); setResult(uiText("Saved to tools · ") + saved.name)
      }
    } catch (e) { setResult(getErrorMessage(e)) }
    finally { setBusy(false) }
  }
  return <section className="card authoring-page"><h2>{uiText("Tool authoring")}</h2>
    <section><h3>{uiText("Recent trials")}</h3>
      <button type="button" onClick={() => setRefresh(n => n + 1)}>{uiText("Refresh")}</button>
      {trialsError && <p role="alert">{trialsError}</p>}
      {trials.length ? <ul>{trials.map(t => <li key={t.evidence_id}><time>{t.time}</time> · {uiText("Rolled back")}
        {' · '}<code>{t.evidence_id}</code> · SHA-256 <code>{t.code_sha256}</code>{' '}
        <button type="button" disabled={busy} onClick={() => void loadTrial(t)}>{uiText("Turn into a tool")}</button></li>)}</ul>
        : <p>{uiText("No remote rollback trials available.")}</p>}
    </section>
    <fieldset disabled={busy}><div className="authoring-columns">
      <div><label>{uiText("Describe the tool")}<textarea value={brief} onChange={e => setBrief(e.target.value)} /></label>
        <label>{uiText("Reference pack")}<select value={based} onChange={e => setBased(e.target.value)}>
          <option value="">{uiText("None")}</option>{tools.map(t => <option key={t.name} value={t.name}>{t.display_name || t.name}</option>)}
        </select></label>
        {catalogError && <p role="alert">{catalogError}</p>}
        <button type="button" disabled={!brief.trim()} onClick={() => action('draft')}>{uiText("Generate draft")}</button>
        {meta && <><label>{uiText("Pack fields (JSON)")}<textarea value={meta} onChange={e => edited(setMeta, e.target.value)} /></label>
          <label>{uiText("Parameters (JSON)")}<textarea value={params} onChange={e => edited(setParams, e.target.value)} /></label>
          <label>{uiText("Trial values (JSON)")}<textarea value={values} onChange={e => edited(setValues, e.target.value)} /></label></>}
      </div>
      <div>{meta && <><label>{uiText("C# template")}<textarea className="authoring-code" spellCheck={false}
          value={code} onChange={e => edited(setCode, e.target.value)} /></label>
        <div className="authoring-actions"><button type="button" onClick={() => action('check')}>{uiText("Compile check")}</button>
        <button type="button" disabled={stage !== 'check'} onClick={() => action('trial')}>{uiText("Dry run and roll back")}</button>
        <button type="button" disabled={stage !== 'trial'} onClick={() => action('saved')}>
          {replace ? uiText("Confirm replacing my tool") : uiText("Save to tools")}</button></div></>}
        {result && <pre role="status">{result}</pre>}
      </div>
    </div></fieldset></section>
}

export default function AuthoringPage({ brief }: { brief?: string }) {
  const scope = useSessionStore(s => s.deviceId)
  return <Editor key={scope} initialBrief={brief} />
}
