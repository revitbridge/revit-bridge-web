/* Capability packs: pick one, load its real choices from Revit, fill parameters, run, edit. */

import { useCallback, useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import type { ToolChoiceItem, ToolInfo, ToolParam } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import Accordion from '../shared/Accordion'
import ExecResult, { type ExecOutcome } from '../shared/ExecResult'
import StepIndicator from '../shared/StepIndicator'
import ToolParamEditor from './ToolParamEditor'

const STEPS = ['Select pack', 'Load choices', 'Set parameters', 'Run']

export default function ToolLibrary() {
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [selected, setSelected] = useState('')
  const [step, setStep] = useState(1)
  const [params, setParams] = useState<ToolParam[]>([])
  const [choices, setChoices] = useState<Record<string, ToolChoiceItem[]>>({})
  const [values, setValues] = useState<Record<string, string>>({})
  const [result, setResult] = useState<ExecOutcome | null>(null)
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const [detail, setDetail] = useState<{ display_name: string; description: string; code_template: string; source_query: string; tags: string[] } | null>(null)
  const [showCode, setShowCode] = useState(false)
  const [editMode, setEditMode] = useState(false)
  const [edit, setEdit] = useState({ display_name: '', description: '', source_query: '', tags: '', params: '[]', code: '' })
  const [saveStatus, setSaveStatus] = useState('')

  const refresh = useCallback(async () => {
    try { setTools(await bridgeApi.listTools()) } catch (e: unknown) { setNotice(`Cannot list packs: ${getErrorMessage(e)}`) }
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
      setDetail({ display_name: d.display_name || d.name, description: d.description || '', code_template: d.code_template || '', source_query: d.source_query || '', tags: d.tags || [] })
      setEdit({ display_name: d.display_name || d.name, description: d.description || '', source_query: d.source_query || '', tags: (d.tags || []).join(', '), params: JSON.stringify(all, null, 2), code: d.code_template || '' })

      let ch: Record<string, ToolChoiceItem[]> = {}
      if (all.some(p => p.choices_from)) {
        try { ch = await bridgeApi.getToolChoices(name) }
        catch (e: unknown) { setNotice(`Choices from Revit unavailable: ${getErrorMessage(e)}. Fill the values by hand or connect Revit first.`) }
      }
      setChoices(ch)
      const defaults: Record<string, string> = {}
      for (const p of all) {
        if (ch[p.name]?.length) defaults[p.name] = String(ch[p.name][0].value)
        else if (p.default != null) defaults[p.name] = String(p.default)
      }
      setValues(defaults)
      setStep(3)
    } catch (e: unknown) {
      setNotice(`Cannot load pack: ${getErrorMessage(e)}`)
    } finally {
      setLoading(false)
    }
  }, [])

  const run = async () => {
    if (!selected) return
    setLoading(true); setStep(4)
    try {
      const res = await bridgeApi.runTool(selected, values)
      setResult(res.success ? { ok: true, data: res.result } : { ok: false, data: null, error: res.error })
      refresh()
    } catch (e: unknown) {
      setResult({ ok: false, data: null, error: getErrorMessage(e) })
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
    } catch (e: unknown) { setSaveStatus(`Parameters JSON: ${getErrorMessage(e)}`); return }
    setLoading(true); setSaveStatus('Saving...')
    try {
      const u = await bridgeApi.updateTool(selected, {
        display_name: edit.display_name, description: edit.description, source_query: edit.source_query,
        tags: edit.tags.split(',').map(t => t.trim()).filter(Boolean), parameters: parsed, code_template: edit.code,
      })
      setDetail({ display_name: u.display_name, description: u.description, code_template: u.code_template, source_query: u.source_query, tags: u.tags })
      setParams(u.parameters || [])
      setEditMode(false)
      setSaveStatus(`Saved${u.revit_synced ? ' and registered in Revit' : ''}.`)
      refresh()
    } catch (e: unknown) {
      setSaveStatus(`Save failed: ${getErrorMessage(e)}`)
    } finally {
      setLoading(false)
    }
  }

  const remove = async (name: string) => {
    if (!window.confirm(`Delete pack '${name}'?`)) return
    try { await bridgeApi.deleteTool(name); if (selected === name) select(''); refresh() }
    catch (e: unknown) { setNotice(`Delete failed: ${getErrorMessage(e)}`) }
  }

  return (
    <div className="space-y-4">
      <StepIndicator steps={STEPS} current={step} />

      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={refresh} className="btn-secondary">Refresh</button>
        <span className="section-copy small">{tools.length} pack{tools.length === 1 ? '' : 's'}</span>
      </div>

      {tools.length > 0 && (
        <div className="card table-scroll">
          <table className="w-full" style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>
            <thead>
              <tr style={{ background: 'var(--bg2)' }}>
                <th className="px-3 py-2 text-left label-text">Name</th>
                <th className="px-3 py-2 text-left label-text">Description</th>
                <th className="px-3 py-2 text-left label-text w-16">Uses</th>
                <th className="px-3 py-2 w-16"></th>
              </tr>
            </thead>
            <tbody>
              {tools.map(t => (
                <tr key={t.name} onClick={() => select(t.name)}
                  style={{ cursor: 'pointer', background: selected === t.name ? 'rgba(217,119,87,0.08)' : 'transparent', borderBottom: '1px solid var(--line)' }}>
                  <td className="px-3 py-1.5" style={{ fontWeight: 500 }}>{t.name}</td>
                  <td className="px-3 py-1.5" style={{ fontFamily: 'var(--serif)', fontSize: 13, color: 'var(--mid)' }}>{t.description}</td>
                  <td className="px-3 py-1.5">{t.used}</td>
                  <td className="px-3 py-1.5">
                    <button className="btn-ghost danger" onClick={e => { e.stopPropagation(); remove(t.name) }}>Delete</button>
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
            <span className="label-text" style={{ margin: 0 }}>Selected:</span>
            <span style={{ fontFamily: 'var(--mono)', fontSize: 13, color: 'var(--accent)' }}>{detail?.display_name || selected}</span>
            <button onClick={() => load(selected)} disabled={loading} className="btn-primary">{loading ? 'Loading...' : 'Load pack'}</button>
            {detail && <button onClick={() => setShowCode(!showCode)} className="btn-secondary">{showCode ? 'Hide code' : 'View code'}</button>}
            {detail && <button onClick={() => { setShowCode(true); setEditMode(true); setSaveStatus('') }} disabled={loading} className="btn-secondary">Edit pack</button>}
          </div>
          {detail?.description && step >= 3 && <p className="section-copy">{detail.description}</p>}
          {detail && detail.tags.length > 0 && step >= 3 && <div className="tool-tag-row">{detail.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
          {notice && <p className="tool-warning-status">{notice}</p>}
        </div>
      )}

      {showCode && detail && (
        <Accordion title={editMode ? 'Edit pack' : 'Pack code'} defaultOpen>
          {editMode && (
            <div className="form-grid">
              <label><span className="label-text">Display name</span>
                <input className="input-field" value={edit.display_name} onChange={e => setEdit({ ...edit, display_name: e.target.value })} /></label>
              <label><span className="label-text">Tags</span>
                <input className="input-field" value={edit.tags} onChange={e => setEdit({ ...edit, tags: e.target.value })} /></label>
              <label className="form-wide"><span className="label-text">Description</span>
                <input className="input-field" value={edit.description} onChange={e => setEdit({ ...edit, description: e.target.value })} /></label>
              <label className="form-wide"><span className="label-text">Source query</span>
                <input className="input-field" value={edit.source_query} onChange={e => setEdit({ ...edit, source_query: e.target.value })} /></label>
            </div>
          )}
          <textarea className="code-editor" readOnly={!editMode} value={editMode ? edit.code : detail.code_template}
            onChange={e => setEdit({ ...edit, code: e.target.value })} spellCheck={false} />
          {editMode && (
            <label style={{ display: 'block', marginTop: 10 }}>
              <span className="label-text">Parameters JSON</span>
              <textarea className="code-editor small" value={edit.params} onChange={e => setEdit({ ...edit, params: e.target.value })} spellCheck={false} />
            </label>
          )}
          <div className="tool-edit-actions">
            {editMode ? (
              <>
                <button onClick={save} className="btn-primary" disabled={loading}>Save pack</button>
                <button onClick={() => setEditMode(false)} className="btn-ghost" disabled={loading}>Cancel</button>
              </>
            ) : <span className="tool-code-note">Placeholders in braces are filled from the parameters below.</span>}
          </div>
          {saveStatus && <p className="tool-review-status">{saveStatus}</p>}
        </Accordion>
      )}

      {step >= 3 && (
        <ToolParamEditor params={params} choices={choices} values={values}
          onChange={(name, value) => setValues(prev => ({ ...prev, [name]: value }))} />
      )}

      {step >= 3 && (
        <button onClick={run} disabled={loading} className="btn-primary">{loading ? 'Running...' : 'Run in Revit'}</button>
      )}

      {result && <ExecResult result={result} />}
    </div>
  )
}
