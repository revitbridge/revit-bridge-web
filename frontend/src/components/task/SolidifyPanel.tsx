/* Panel 6: code that worked becomes a v1 pack - POST /solidify with the parameter draft;
   the server's problems (schema, sandbox) are listed one per line. */

import { useEffect, useState } from 'react'
import type { SolidifyRequest, SolidifyResponse, TaskSpec } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import { TaskApiError } from './api'
import { draftPack } from './solidifyDraft'

interface Props {
  spec: TaskSpec                                   // the confirmed execute_code spec that succeeded
  evidenceId?: string
  solidify: (payload: SolidifyRequest) => Promise<SolidifyResponse>
}

export default function SolidifyPanel({ spec, evidenceId, solidify }: Props) {
  const [form, setForm] = useState(() => toForm(draftPack(spec)))
  const [busy, setBusy] = useState(false)
  const [problems, setProblems] = useState<string[]>([])
  const [saved, setSaved] = useState<SolidifyResponse | null>(null)

  useEffect(() => {
    setForm(toForm(draftPack(spec)))
    setProblems([])
    setSaved(null)
  }, [spec])

  const submit = async () => {
    let parameters: SolidifyRequest['parameters']
    let validator: SolidifyRequest['validator'] = null
    try {
      const parsed: unknown = JSON.parse(form.parameters)
      if (!Array.isArray(parsed)) throw new Error('parameters must be a JSON array')
      parameters = parsed
    } catch (e: unknown) { setProblems([`parameters: ${getErrorMessage(e)}`]); return }
    if (form.validator.trim()) {
      try {
        const parsed: unknown = JSON.parse(form.validator)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('validator must be a JSON object')
        validator = parsed as Record<string, unknown>
      } catch (e: unknown) { setProblems([`validator: ${getErrorMessage(e)}`]); return }
    }
    setBusy(true); setProblems([]); setSaved(null)
    try {
      setSaved(await solidify({
        name: form.name.trim(), code: spec.action.code ?? '', description: form.description.trim(),
        parameters, source_query: form.source_query.trim(), validator,
      }))
    } catch (e: unknown) {
      const body = e instanceof TaskApiError ? e.body : null
      const listed = body?.problems?.length ? body.problems : body?.warnings?.length ? body.warnings : null
      setProblems(listed ? listed.map(p => `${body?.error}: ${p}`) : [getErrorMessage(e)])
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="card section">
      <div className="panel-head">
        <h3 className="heading-display section-title">6. Solidify</h3>
        {evidenceId && <span className="muted small-mono">from evidence {evidenceId}</span>}
      </div>
      <p className="section-copy small" style={{ marginTop: 0 }}>
        The code that just worked, saved as a capability pack (v1: every parameter with a source, required and unit).
        Edit the draft; the server validates it and lists every problem.
      </p>
      <div className="form-grid">
        <label><span className="label-text">Pack name</span>
          <input className="input-field" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></label>
        <label><span className="label-text">Source query</span>
          <input className="input-field" value={form.source_query} onChange={e => setForm({ ...form, source_query: e.target.value })} /></label>
        <label className="form-wide"><span className="label-text">Description</span>
          <input className="input-field" value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} /></label>
        <label className="form-wide"><span className="label-text">Parameters (v1 JSON)</span>
          <textarea className="code-editor small" value={form.parameters} onChange={e => setForm({ ...form, parameters: e.target.value })} spellCheck={false} /></label>
        <label className="form-wide"><span className="label-text">Validator (optional JSON: created_ids | count_delta | param_equals)</span>
          <textarea className="code-editor small" style={{ minHeight: 64 }} value={form.validator} onChange={e => setForm({ ...form, validator: e.target.value })}
            placeholder='{"kind": "created_ids", "category": "OST_Walls"}' spellCheck={false} /></label>
      </div>
      <details className="spec-details">
        <summary className="label-text">Code (as executed)</summary>
        <pre className="command-block">{spec.action.code}</pre>
      </details>
      <div className="flex gap-2 items-center" style={{ marginTop: 10 }}>
        <button className="btn-primary" onClick={submit} disabled={busy || !form.name.trim()}>{busy ? 'Saving...' : 'Save as pack'}</button>
        {saved && <span className="tool-review-status" style={{ margin: 0 }}>Saved <code>{saved.name}</code> v{saved.version ?? '1.0.0'}{saved.revit_synced ? ', registered in Revit' : ''}. It is on the Capabilities page.</span>}
      </div>
      {problems.length > 0 && (
        <ul className="spec-errors" data-testid="solidify-problems">
          {problems.map((p, i) => <li key={i}>{p}</li>)}
        </ul>
      )}
    </section>
  )
}

function toForm(draft: SolidifyRequest) {
  return {
    name: draft.name,
    description: draft.description,
    source_query: draft.source_query,
    parameters: JSON.stringify(draft.parameters, null, 2),
    validator: '',
  }
}
