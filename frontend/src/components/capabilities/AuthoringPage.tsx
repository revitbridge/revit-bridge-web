import { useEffect, useRef, useState } from 'react'
import { flushSync } from 'react-dom'
import { uiText } from '../../utils/strings'
import { bridgeApi } from '../../api/bridge'
import { llmHeaders, useSessionStore } from '../../store'
import { BridgeApiError, get, post } from '../shared/http'
import { diagnosticRange, readCheck, readDryRun, type Diagnostic, type DryRunReply } from '../hostv2/workbench/results'
import { DiagnosticList, DryRunResult } from '../hostv2/workbench/Workbench'
import { getErrorMessage, serverReasons } from '../../utils/errors'
import type { ToolInfo } from '../../types/api'
import { Field, ParameterTable, ValueInput } from './AuthoringFields'
import { blankPack, paramRows, readPackJson, removeUnsupported, shown, trialInputs, unsupportedFields, type Pack } from './authoringPack'
import './authoring.css'

const base = '/api/v1/bridge'
interface Draft { pack: Pack; problems: string[]; reference: string; repair_rounds: number; diagnostics: Diagnostic[]; compiled: boolean | null; check_reason?: string }
interface Trial { evidence_id: string; time: string; code_sha256: string }
type Stage = 'edit' | 'checked' | 'tried' | 'saved'
type Action = 'draft' | 'check' | 'trial' | 'save'
interface Props {
  brief?: string
  connected?: boolean | null
  modelAvailable?: boolean
  onConnectionLost?: () => void
  onSavedLink?: (name: string, chat: boolean) => void
}

function Editor({ brief: initialBrief, connected, modelAvailable = true, onConnectionLost, onSavedLink }: Props) {
  const [brief, setBrief] = useState(initialBrief ?? '')
  const [based, setBased] = useState('')
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [pack, setPack] = useState<Pack | null>(null)
  const [values, setValues] = useState<Pack>({})
  const [json, setJson] = useState<string | null>(null)
  const [jsonError, setJsonError] = useState('')
  const [stage, setStage] = useState<Stage>('edit')
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [origin, setOrigin] = useState('')
  const [problems, setProblems] = useState<string[]>([])
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [trial, setTrial] = useState<DryRunReply | null>(null)
  const [catalogError, setCatalogError] = useState('')
  const [trials, setTrials] = useState<Trial[]>([])
  const [trialsError, setTrialsError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const editor = useRef<HTMLTextAreaElement>(null)
  const gutter = useRef<HTMLDivElement>(null)
  const connection = useRef<AbortController | null>(null)
  const [lastConnected, setLastConnected] = useState(connected)

  function invalidate() {
    setStage('edit'); setReplace(false); setNotice(''); setProblems([]); setDiagnostics([]); setTrial(null)
  }
  // A reconnect requires a fresh check even if the text is identical.
  if (lastConnected !== connected) {
    setLastConnected(connected)
    if (!connected && stage !== 'saved') {
      setStage('edit'); setReplace(false); setTrial(null); setNotice('')
    }
  }
  useEffect(() => {
    const controller = new AbortController()
    connection.current = controller
    return () => controller.abort()
  }, [connected])
  useEffect(() => {
    let active = true
    get<{ trials: Trial[] }>(base + '/remote-trials').then(data => {
      if (active) { setTrials(data.trials); setTrialsError('') }
    }, err => { if (active) setTrialsError(getErrorMessage(err)) })
    return () => { active = false }
  }, [refresh])
  useEffect(() => {
    let active = true
    bridgeApi.listTools().then(items => { if (active) setTools(items) })
      .catch(e => { if (active) setCatalogError(getErrorMessage(e)) })
    return () => { active = false }
  }, [])
  const rows = pack ? paramRows(pack) : []
  const code = shown(pack?.code_template)
  const name = shown(pack?.name)
  const unsupported = pack ? unsupportedFields(pack) : []
  const draftReason = !modelAvailable ? uiText('Configure a model in Settings to generate a draft.')
    : !brief.trim() ? uiText('Describe the tool first.') : ''
  const checkReason = !pack ? uiText('Create a draft first.') : jsonError ? uiText('Correct the JSON first.')
    : unsupported.length ? uiText('Remove the listed fields first.')
    : !name.trim() || !code.trim() ? uiText('Enter a tool name and C# template.')
    : tools.some(t => t.builtin && t.name.toLowerCase() === name.toLowerCase()) ? uiText('Built-in tools are read-only. Choose a new name.')
    : !connected ? uiText('Revit is not connected.') : ''
  const inputs = trialInputs(rows, values, false)
  const inputFor = (p: Pack) => Object.hasOwn(inputs, shown(p.name)) ? inputs[shown(p.name)] : undefined
  const missing = rows.filter(p => inputFor(p) == null)
  const trialReason = checkReason || (stage === 'edit' ? uiText('Compile check has not passed for this text.')
    : missing.length ? uiText('Enter trial values for {0}.', missing.map(p => shown(p.name)).join(', ')) : '')
  const saveReason = trialReason || (stage === 'saved' ? uiText('Saved to tools') : stage !== 'tried' ? uiText('Complete a successful rollback trial first.') : '')

  function edit(next: Pack) {
    invalidate(); setPack(next)
    if (next.parameters !== pack?.parameters) {
      const nextRows = paramRows(next)
      setValues(previous => Object.fromEntries(Object.entries(previous).filter(([key]) => {
        const old = rows.find(p => p.name === key)
        return old && nextRows.some(p => p.name === key && p.type === old.type)
      })))
    }
  }
  function load(next: Pack) {
    edit(next); setValues({}); setJson(null); setJsonError(''); setOrigin('')
  }
  function editJson(text: string) {
    invalidate(); setJson(text)
    try { edit(readPackJson(text)); setJsonError('') }
    catch (err) { setJsonError(getErrorMessage(err)) }
  }
  function locate(diagnostic: Diagnostic) {
    const range = diagnosticRange(code, diagnostic)
    if (!range) return
    if (json !== null) flushSync(() => setJson(null))
    const el = editor.current
    if (el) {
      el.focus(); el.setSelectionRange(...range)
      el.scrollTop = Math.max(0, ((diagnostic.line ?? 1) - 2) * parseFloat(getComputedStyle(el).lineHeight))
      if (gutter.current) gutter.current.scrollTop = el.scrollTop
    }
  }
  async function loadTrial(t: Trial) {
    setBusy(true)
    try {
      const draft = await get<{ code: string }>(base + '/remote-trials/' + encodeURIComponent(t.evidence_id))
      load({ ...blankPack(), name: 'trial_' + t.code_sha256.slice(0, 12), code_template: draft.code })
      setTrialsError(''); setNotice(uiText('Enter a tool name and description, then run Compile check.'))
    } catch (err) { setTrialsError(getErrorMessage(err)) } finally { setBusy(false) }
  }
  async function action(kind: Action) {
    if (busy || { draft: draftReason, check: checkReason, trial: trialReason, save: saveReason }[kind]) return
    const signal = connection.current?.signal
    setBusy(true); setNotice(''); setProblems([]); setDiagnostics([]); setTrial(null)
    try {
      if (kind === 'draft') {
        invalidate(); setOrigin('')
        const result = await post<Draft>(base + '/tools/draft', { description: brief, ...(based ? { based_on: based } : {}) }, llmHeaders())
        load(result.pack); setProblems(result.problems); setDiagnostics(result.diagnostics ?? [])
        setOrigin([result.reference && uiText('reference: {0}', result.reference),
          typeof result.repair_rounds === 'number' && uiText('Repair rounds: {0}', result.repair_rounds)].filter(Boolean).join(' · '))
        setNotice(result.check_reason || (result.compiled === true ? uiText('Sample draft compiled.') : ''))
      } else if (kind === 'check') {
        setStage('edit'); setReplace(false)
        const result = readCheck(await post(base + '/check', { pack, values: trialInputs(rows, values, true) }))
        if (signal?.aborted) return
        const passed = result.checked && result.compiled === true && !result.blocked
        setDiagnostics(result.diagnostics)
        setProblems([result.reason, ...result.warnings, ...(!passed && !result.reason && !result.diagnostics.length && !result.warnings.length ? [uiText('Compile failed')] : [])].filter((s): s is string => !!s))
        if (passed) { setStage('checked'); setNotice(uiText('Compiled')) }
      } else if (kind === 'trial') {
        setStage('checked'); setReplace(false)
        const raw = await post<Pack>(base + '/dry-run', { pack, values: inputs })
        const result = readDryRun(raw)
        if (signal?.aborted) return
        setTrial(result); setDiagnostics(result.diagnostics)
        if (result.success && result.rolled_back === true) setStage('tried')
        else setProblems([...new Set([result.error, ...serverReasons(raw),
          result.rolled_back !== true ? uiText('Rollback is not confirmed.') : uiText('Dry run failed.')].filter((s): s is string => !!s))])
      } else if (pack) {
        const existing = (await bridgeApi.listTools()).find(t => t.name.toLowerCase() === name.toLowerCase())
        if (signal?.aborted) return
        if (existing?.builtin) throw new Error(uiText('Built-in tools are read-only.'))
        if (existing && !replace) {
          setReplace(true); setNotice(uiText('A saved user tool has this name: ') + existing.name); return
        }
        const saved = await post<{ name: string }>(base + '/solidify', { name, code, description: pack.description,
          parameters: pack.parameters, source_query: pack.source_query, validator: pack.validator })
        setStage('saved'); setNotice(uiText('Saved to tools · ') + saved.name)
      }
    } catch (err) {
      const reasons = err instanceof BridgeApiError ? serverReasons(err.body) : []
      if (err instanceof BridgeApiError && err.code === 'revit_unreachable') onConnectionLost?.()
      if (kind === 'draft' && err instanceof BridgeApiError) {
        if (!pack && err.code === 'invalid_pack') setPack(blankPack())
        if (typeof err.body?.repair_rounds === 'number') setOrigin(uiText('Repair rounds: {0}', err.body.repair_rounds))
      }
      setProblems(reasons.length ? reasons : [getErrorMessage(err)])
    } finally { setBusy(false) }
  }

  const editing = !pack || !name || !code || jsonError || unsupported.length > 0 || (problems.length > 0 && !diagnostics.length)
  const current = stage === 'edit' ? (editing ? 0 : 1) : stage === 'checked' ? 2 : 3
  function actionButton(kind: Action, label: string, reason: string) {
    return <div><button type="button" data-action={kind} className={!reason ? 'btn-primary' : 'btn-secondary'}
      disabled={busy || !!reason} aria-describedby={reason ? kind + '-reason' : undefined} onClick={() => void action(kind)}>{uiText(label)}</button>
      {reason && <small id={kind + '-reason'} className="helper-text">{reason}</small>}</div>
  }
  return <section className="card authoring-page"><h2>{uiText('Tool authoring')}</h2>
    <ol className="steps-indicator" aria-label={uiText('Tool authoring')}>
      {['Draft / edit', 'Compile check', 'Trial run with rollback'].map((label, i) =>
        <li key={label} data-state={current > i ? 'done' : current === i ? 'current' : 'pending'}
          aria-current={current === i ? 'step' : undefined} className={current === i ? 'is-active' : undefined}>
          {current > i ? '✓' : i + 1} {uiText(label)}</li>)}
    </ol>
    <fieldset disabled={busy}><div className="form-grid">
      <label>{uiText('Describe the tool')}<textarea value={brief} onChange={e => { setBrief(e.target.value); invalidate() }} /></label>
      <label>{uiText('Reference pack')}<select value={based} onChange={e => { setBased(e.target.value); invalidate() }}>
        <option value="">{uiText('Automatic reference')}</option>
        {tools.map(t => <option key={t.name} value={t.name}>{t.display_name || t.name}</option>)}</select></label>
    </div>
    {catalogError && <p role="alert">{catalogError}</p>}
    {actionButton('draft', 'Generate draft', draftReason)}
    {origin && <p className="helper-text">{origin}</p>}
    <button type="button" aria-pressed={json !== null} disabled={!!jsonError}
      onClick={() => { if (json === null) { const next = pack ?? blankPack(); setPack(next); setJson(JSON.stringify(next, null, 2)) } else setJson(null) }}>
      {uiText('Edit as JSON')}</button>
    {jsonError && <button type="button" onClick={() => { setJson(null); setJsonError('') }}>{uiText('Discard invalid JSON')}</button>}
    {json !== null && <label>{uiText('Pack JSON')}<textarea className="code-editor authoring-json" spellCheck={false} value={json}
      aria-invalid={!!jsonError} onChange={e => editJson(e.target.value)} />{jsonError && <small className="text-danger" role="alert">{jsonError}</small>}</label>}
    {pack && <>
      {unsupported.length > 0 && !jsonError && <div className="notice" role="note">{uiText('Will not be saved')}{' · '}
        {unsupported.join(', ')} <button type="button" onClick={() => { const next = removeUnsupported(pack); edit(next)
          if (json !== null) setJson(JSON.stringify(next, null, 2)) }}>{uiText('Remove listed fields')}</button></div>}
      {json === null && <>
        <div className="form-grid">
          <Field path="name" label={uiText('Name')} problems={problems}><input value={name} onChange={e => edit({ ...pack, name: e.target.value })} /></Field>
          <label>{uiText('Display name (from name)')}<input readOnly value={name.replaceAll('_', ' ').toLowerCase().replace(/[a-z]+/g, word => word[0].toUpperCase() + word.slice(1))} /></label>
          <Field path="description" label={uiText('Description')} problems={problems}><input value={shown(pack.description)}
            onChange={e => edit({ ...pack, description: e.target.value })} /></Field>
        </div>
        <ParameterTable rows={rows} problems={problems} onChange={parameters => edit({ ...pack, parameters })} />
      </>}
      {json === null && <Field path="code_template" label={uiText('C# template')} problems={problems}>
        <span className="hostv2-workbench-editor"><span className="hostv2-workbench-lines" ref={gutter} aria-hidden="true">
          {code.split(/\r\n?|\n/).map((_, i) => i + 1).join('\n')}</span>
          <textarea className="authoring-code" ref={editor} spellCheck={false} wrap="off" value={code}
            onScroll={e => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop }}
            onChange={e => edit({ ...pack, code_template: e.target.value })} /></span></Field>}
      {rows.length > 0 && <section><h3>{uiText('Trial values')}</h3><div className="form-grid">{rows.map((p, i) =>
        <label key={i}>{shown(p.name)}{p.unit ? ' (' + shown(p.unit) + ')' : ''}{p.required ? uiText(' (required)') : ''}
          <ValueInput label={uiText('Trial value for {0}', shown(p.name))} type={p.type} value={inputFor(p)} allowEmptyText
            onChange={v => { invalidate(); setValues({ ...values, [shown(p.name)]: v }) }} /></label>)}</div></section>}
    </>}
    <div className="authoring-actions">
      {actionButton('check', 'Compile check', checkReason)}
      {actionButton('trial', 'Dry run and roll back', trialReason)}
      {actionButton('save', replace && stage !== 'saved' ? 'Confirm replacing my tool' : 'Add to tools', saveReason)}
    </div></fieldset>
    {busy && <p role="status">{uiText('Working...')}</p>}
    {!!(problems.length || diagnostics.length) && <div className="spec-errors" role="alert">
      {!!problems.length && <ul>{problems.map((p, i) => <li key={i}>{p}</li>)}</ul>}
      <DiagnosticList diagnostics={diagnostics} code={code} locate={locate} />
    </div>}
    {notice && <p className="status-line" role="status">{notice}</p>}
    {trial && <DryRunResult result={trial} />}
    {stage === 'saved' && <nav className="toolbar">{[false, true].map(chat =>
      <a key={String(chat)} href={(chat ? '/' : '/tools') + '?tool=' + encodeURIComponent(name)} onClick={e => {
        if (onSavedLink && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) { e.preventDefault(); onSavedLink(name, chat) }
      }}>{chat ? uiText('Use it in chat') : uiText('View {0} in tools', name)}</a>)}</nav>}
    <section className="recent-trials"><div className="panel-head"><h3>{uiText('Recent trials')}</h3>
      <button type="button" onClick={() => setRefresh(n => n + 1)}>{uiText('Refresh')}</button></div>
      {trialsError && <p role="alert">{trialsError}</p>}
      {trials.length ? <ul>{trials.map(t => <li key={t.evidence_id} className="trial-row"><time>{t.time}</time> · {uiText('Rolled back')}
        {' · '}<code>{t.evidence_id}</code> · SHA-256 <code>{t.code_sha256}</code>{' '}
        <button type="button" disabled={busy} onClick={() => void loadTrial(t)}>{uiText('Turn into a tool')}</button></li>)}</ul>
        : <p>{uiText('No remote rollback trials available.')}</p>}
    </section>
  </section>
}

export default function AuthoringPage(props: Props) {
  const scope = useSessionStore(s => s.deviceId)
  return <Editor key={scope} {...props} />
}
