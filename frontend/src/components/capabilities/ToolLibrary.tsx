/* Capability packs: pick one, load its real choices from Revit, fill parameters,
   confirm and run under a token; edit or delete your own with the admin password
   (shared/packAdmin) - the built-in packs are read-only. */

import { uiText } from '../../utils/strings'
import { useCallback, useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import type { ExecutionResult, SpecError, ToolChoiceItem, ToolInfo, ToolParam } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import Accordion from '../shared/Accordion'
import { usePackGate } from '../shared/packAdmin'
import StepIndicator from '../shared/StepIndicator'
import { taskApi } from '../task/api'
import { ExecutionView } from '../task/ExecutionPanel'
import { tokenPrefix } from '../task/format'
import { SpecTable } from '../task/SpecCard'
import { confirmAndRun, describePreconditions, initialValues, specFromForm } from './directRun'
import ToolParamEditor from './ToolParamEditor'

const STEPS = ['Select pack', 'Load choices', 'Set parameters', 'Confirm and run']

type RunOutcome =
  | { kind: 'rejected'; errors: SpecError[] }
  | { kind: 'executed'; token: string; expires_at: string; result: ExecutionResult }
  | { kind: 'failed'; message: string }

export default function ToolLibrary() {
  const gate = usePackGate()
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [selected, setSelected] = useState('')
  const [step, setStep] = useState(1)
  const [params, setParams] = useState<ToolParam[]>([])
  const [choices, setChoices] = useState<Record<string, ToolChoiceItem[]>>({})
  const [values, setValues] = useState<Record<string, string>>({})
  const [result, setResult] = useState<RunOutcome | null>(null)
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [detail, setDetail] = useState<{ display_name: string; description: string; code_template: string; source_query: string; tags: string[]; version: string; preconditions: Array<Record<string, unknown>>; validator: Record<string, unknown> | null } | null>(null)
  const [showCode, setShowCode] = useState(false)
  const [editMode, setEditMode] = useState(false)
  const [edit, setEdit] = useState({ display_name: '', description: '', source_query: '', tags: '', params: '[]', code: '' })
  const [saveStatus, setSaveStatus] = useState('')

  const refresh = useCallback(async () => {
    try { setTools(await bridgeApi.listTools()) } catch (e: unknown) { setNotice(uiText("Cannot list packs: {0}", getErrorMessage(e))) }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const select = (name: string) => {
    setSelected(name); setStep(1); setShowCode(false); setEditMode(false); setDetail(null)
    setResult(null); setNotice(''); setSaveStatus(''); setChoices({}); setValues({}); setParams([])
  }

  const load = useCallback(async (name: string) => {
    if (!name) return
    setLoading(true); setStep(2); setNotice(''); setResult(null); setSaveStatus('')
    try {
      const d = await bridgeApi.getTool(name)
      const all = d.parameters || []
      setParams(all)
      setDetail({ display_name: d.display_name || d.name, description: d.description || '', code_template: d.code_template || '', source_query: d.source_query || '', tags: d.tags || [],
        version: d.version, preconditions: d.preconditions || [], validator: d.validator ?? null })
      setEdit({ display_name: d.display_name || d.name, description: d.description || '', source_query: d.source_query || '', tags: (d.tags || []).join(', '), params: JSON.stringify(all, null, 2), code: d.code_template || '' })

      let ch: Record<string, ToolChoiceItem[]> = {}
      if (all.some(p => p.choices_from)) {
        try { ch = await bridgeApi.getToolChoices(name) }
        catch (e: unknown) { setNotice(uiText("Choices from Revit unavailable: {0}. Fill the values by hand or connect Revit first.", getErrorMessage(e))) }
      }
      setChoices(ch)
      setValues(initialValues(all, ch))
      setStep(3)
    } catch (e: unknown) {
      setNotice(uiText("Cannot load pack: {0}", getErrorMessage(e)))
    } finally {
      setLoading(false)
    }
  }, [])

  /* The filled-in values become a TaskSpec; the run goes through the gate like every other. */
  const spec = selected && step >= 3 ? specFromForm({ name: selected, display_name: detail?.display_name }, params, values, choices) : null

  const run = async () => {
    if (!spec) return
    setLoading(true); setStep(4)
    try {
      const outcome = await confirmAndRun(taskApi, spec)
      setResult(outcome.status === 'rejected'
        ? { kind: 'rejected', errors: outcome.errors }
        : { kind: 'executed', token: outcome.token, expires_at: outcome.expires_at, result: outcome.result })
      refresh()
    } catch (e: unknown) {
      setResult({ kind: 'failed', message: getErrorMessage(e) })
    } finally {
      setLoading(false)
    }
  }

  const save = async () => {
    if (!selected) return
    let parsed: ToolParam[]
    try {
      const p = JSON.parse(edit.params)
      if (!Array.isArray(p)) throw new Error('parameters must be an array')
      parsed = p
    } catch (e: unknown) { setSaveStatus(uiText("Parameters JSON: {0}", getErrorMessage(e))); return }
    setLoading(true); setSaveStatus('Saving...')
    try {
      const u = await bridgeApi.updateTool(selected, {
        display_name: edit.display_name, description: edit.description, source_query: edit.source_query,
        tags: edit.tags.split(',').map(t => t.trim()).filter(Boolean), parameters: parsed, code_template: edit.code,
      })
      setDetail({ display_name: u.display_name, description: u.description, code_template: u.code_template, source_query: u.source_query, tags: u.tags,
        version: u.version, preconditions: u.preconditions || [], validator: u.validator ?? null })
      setParams(u.parameters || [])
      setEditMode(false)
      setSaveStatus(uiText("Saved{0}.", u.revit_synced ? uiText(" and registered in Revit") : ''))
      refresh()
    } catch (e: unknown) {
      setSaveStatus(uiText("Save failed: {0}", getErrorMessage(e)))
    } finally {
      setLoading(false)
    }
  }

  const remove = async (name: string) => {
    if (!window.confirm(uiText("Delete pack '{0}'?", name))) return
    try { await bridgeApi.deleteTool(name); if (selected === name) select(''); refresh() }
    catch (e: unknown) { setNotice(uiText("Delete failed: {0}", getErrorMessage(e))) }
  }

  return (
    <div className="space-y-4">
      <StepIndicator steps={STEPS} current={step} />

      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={refresh} className="btn-secondary">{uiText("Refresh")}</button>
        <span className="section-copy small">{tools.length} {uiText(tools.length === 1 ? 'pack' : 'packs')}{gate.note && `. ${uiText(gate.note)}`}</span>
      </div>
      {!selected && notice && <p className="tool-warning-status">{notice}</p>}

      {tools.length > 0 && (
        <div className="card table-scroll">
          <table className="pack-table">
            <thead>
              <tr style={{ background: 'var(--bg2)' }}>
                <th>{uiText("Name")}</th>
                <th>{uiText("Description")}</th>
                <th>{uiText("Version")}</th>
                <th>{uiText("Validator")}</th>
                <th>{uiText("Preconditions")}</th>
                <th>{uiText("Uses")}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {tools.map(t => (
                <tr key={t.name} onClick={() => select(t.name)}
                  className={selected === t.name ? 'is-open' : undefined}>
                  <td><button className="pack-name">{t.name}</button></td>
                  <td>{t.description}</td>
                  <td>{t.version}</td>
                  <td>{t.validator ?? <span style={{ color: 'var(--faint)' }}>{uiText("none")}</span>}</td>
                  <td style={{ color: 'var(--mid)' }}>{describePreconditions(t.preconditions)}</td>
                  <td>{t.used}</td>
                  <td>
                    {gate.canEdit && !t.builtin && <button className="btn-ghost danger" onClick={e => { e.stopPropagation(); remove(t.name) }}>{uiText("Delete")}</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span className="label-text" style={{ margin: 0 }}>{uiText("Selected:")}</span>
            <span className="chip info">{detail?.display_name || selected}</span>
            <button onClick={() => load(selected)} disabled={loading} className="btn-primary">{loading ? uiText("Loading...") : uiText("Load pack")}</button>
            {detail && <button onClick={() => setShowCode(!showCode)} className="btn-secondary">{showCode ? uiText("Hide code") : uiText("View code")}</button>}
            {detail && gate.canEdit && !tools.find(t => t.name === selected)?.builtin && <button onClick={() => { setShowCode(true); setEditMode(true); setSaveStatus('') }} disabled={loading} className="btn-secondary">{uiText("Edit pack")}</button>}
          </div>
          {detail?.description && step >= 3 && <p className="section-copy">{detail.description}</p>}
          {detail && step >= 3 && (
            <p className="section-copy small" style={{ margin: 0 }}>
              v{detail.version} {uiText("| validator:")} {detail.validator ? <code>{JSON.stringify(detail.validator)}</code> : uiText("none")}
              {' | '}{uiText("preconditions:")} {detail.preconditions.length ? describePreconditions(detail.preconditions) : uiText("none")}
            </p>
          )}
          {detail && detail.tags.length > 0 && step >= 3 && <div className="tool-tag-row">{detail.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
          {notice && <p className="tool-warning-status">{notice}</p>}
        </div>
      )}

      {showCode && detail && (
        <Accordion title={editMode ? uiText("Edit pack") : uiText("Pack code")} defaultOpen>
          {editMode && (
            <div className="form-grid">
              <label><span className="label-text">{uiText("Display name")}</span>
                <input className="input-field" value={edit.display_name} onChange={e => setEdit({ ...edit, display_name: e.target.value })} /></label>
              <label><span className="label-text">{uiText("Tags")}</span>
                <input className="input-field" value={edit.tags} onChange={e => setEdit({ ...edit, tags: e.target.value })} /></label>
              <label className="form-wide"><span className="label-text">{uiText("Description")}</span>
                <input className="input-field" value={edit.description} onChange={e => setEdit({ ...edit, description: e.target.value })} /></label>
              <label className="form-wide"><span className="label-text">{uiText("Source query")}</span>
                <input className="input-field" value={edit.source_query} onChange={e => setEdit({ ...edit, source_query: e.target.value })} /></label>
            </div>
          )}
          <textarea className="code-editor" readOnly={!editMode} value={editMode ? edit.code : detail.code_template}
            onChange={e => setEdit({ ...edit, code: e.target.value })} spellCheck={false} />
          {editMode && (
            <label style={{ display: 'block', marginTop: 10 }}>
              <span className="label-text">{uiText("Parameters JSON")}</span>
              <textarea className="code-editor small" value={edit.params} onChange={e => setEdit({ ...edit, params: e.target.value })} spellCheck={false} />
            </label>
          )}
          <div className="tool-edit-actions">
            {editMode ? (
              <>
                <button onClick={save} className="btn-primary" disabled={loading}>{uiText("Save pack")}</button>
                <button onClick={() => setEditMode(false)} className="btn-ghost" disabled={loading}>{uiText("Cancel")}</button>
              </>
            ) : <span className="tool-code-note">{uiText("Placeholders in braces are filled from the parameters below.")}</span>}
          </div>
          {saveStatus && <p className="tool-review-status">{saveStatus}</p>}
        </Accordion>
      )}

      {step >= 3 && (
        <ToolParamEditor params={params} choices={choices} values={values}
          onChange={(name, value) => setValues(prev => ({ ...prev, [name]: value }))} />
      )}

      {spec && (
        <Accordion title={uiText("Spec card (what the token binds to)")} defaultOpen>
          <SpecTable spec={spec} />
          <p className="section-copy small">{uiText("Confirm issues a one-time token for exactly these values and runs the pack under it; the result and the validator's checks are recorded in the evidence ledger.")}</p>
          <button onClick={run} disabled={loading || spec.parameters.length < params.filter(p => p.required).length} className="btn-primary">
            {loading ? uiText("Running...") : uiText("Confirm and run in Revit")}
          </button>
        </Accordion>
      )}

      {result?.kind === 'rejected' && (
        <ul className="spec-errors">
          {result.errors.map((e, i) => <li key={i}>{uiText("Confirm refused:")} <code>{e.code}</code>{e.param ? <> {uiText("on")} <code>{e.param}</code></> : null}: {e.message}</li>)}
        </ul>
      )}
      {result?.kind === 'failed' && <p className="tool-warning-status">{result.message}</p>}
      {result?.kind === 'executed' && (
        <div>
          <span className="token-chip">{uiText("token")} <code>{tokenPrefix(result.token)}</code> {uiText("| expires")} {result.expires_at}</span>
          <ExecutionView execution={result.result} />
        </div>
      )}
    </div>
  )
}
