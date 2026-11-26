/* Panel 1: what exists in the model right now - GET /snapshot, taken when the page opens. */

import { uiText } from '../../utils/strings'
import { useCallback, useEffect, useState } from 'react'
import { bridgeApi } from '../../api/bridge'
import type { ProjectSnapshot } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'

const MAX_NAMES = 6

export default function SnapshotPanel() {
  const [snapshot, setSnapshot] = useState<ProjectSnapshot | null>(null)
  const [status, setStatus] = useState('Taking the snapshot...')
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      setSnapshot(await bridgeApi.snapshot())
      setStatus('')
    } catch (e: unknown) {
      setSnapshot(null)
      setStatus(`${uiText("No snapshot")}: ${getErrorMessage(e)}`)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { refresh() }, [refresh])

  return (
    <section className="card section">
      <div className="panel-head">
        <h3 className="heading-display section-title">{uiText("1. Snapshot")}</h3>
        <div className="flex gap-2">
          <button className="btn-secondary" onClick={refresh} disabled={loading}>{loading ? uiText("Taking...") : uiText("Refresh")}</button>
          {snapshot && <button className="btn-ghost" onClick={() => setOpen(!open)}>{open ? uiText("Collapse") : uiText("Expand")}</button>}
        </div>
      </div>
      {status && <div className="status-line">{uiText(status)}</div>}
      {snapshot && open && <SnapshotSummary snapshot={snapshot} />}
      {snapshot && !open && (
        <p className="section-copy small">
          {snapshot.document.title} | {snapshot.units.raw} | {snapshot.levels.length} {uiText("levels | fingerprint")} <code>{snapshot.fingerprint}</code>
        </p>
      )}
    </section>
  )
}

export function SnapshotSummary({ snapshot }: { snapshot: ProjectSnapshot }) {
  const names = (list: string[]) => list.length > MAX_NAMES ? `${list.slice(0, MAX_NAMES).join(', ')}, ...` : list.join(', ')
  return (
    <dl className="snapshot-grid">
      <dt>{uiText("Document")}</dt>
      <dd>{snapshot.document.title} <span className="muted">| Revit {snapshot.document.revit_version}{snapshot.document.is_workshared ? uiText(" | workshared") : ''}</span></dd>
      <dt>{uiText("Units")}</dt>
      <dd>{snapshot.units.raw} <span className="muted">({snapshot.units.length}{uiText("; packs take millimetres)")}</span></dd>
      <dt>{uiText("Levels")}</dt>
      <dd>{snapshot.levels.length === 0 ? <span className="muted">{uiText("none")}</span>
        : snapshot.levels.map(l => <span key={l.id} className="chip">{l.name} <span className="muted">{l.elevation_mm} mm</span></span>)}</dd>
      <dt>{uiText("Grids")}</dt>
      <dd>{snapshot.grids.count}{snapshot.grids.names.length > 0 && <span className="muted"> | {names(snapshot.grids.names)}</span>}</dd>
      <dt>{uiText("Types")}</dt>
      <dd>{snapshot.family_types.length === 0 ? <span className="muted">{uiText("none in the default categories")}</span>
        : snapshot.family_types.map(t => (
          <div key={t.category}><code>{t.category}</code> {t.count} <span className="muted">{names(t.names)}</span></div>
        ))}</dd>
      <dt>{uiText("Selection")}</dt>
      <dd>{snapshot.selection_count === 0 ? <span className="muted">{uiText("nothing selected")}</span>
        : <>{snapshot.selection_count} {uiText("element")}{snapshot.selection_count === 1 ? '' : 's'}: {snapshot.selection.map(s => `${s.category} ${s.name} (#${s.id})`).join(', ')}</>}</dd>
      {snapshot.active_view && <><dt>{uiText("View")}</dt><dd>{snapshot.active_view.name} <span className="muted">{snapshot.active_view.view_type}{snapshot.active_view.level ? ` | ${snapshot.active_view.level}` : ''}</span></dd></>}
      {snapshot.warnings.length > 0 && <><dt>{uiText("Warnings")}</dt><dd className="text-danger">{snapshot.warnings.join('; ')}</dd></>}
      <dt>{uiText("Fingerprint")}</dt>
      <dd><code>{snapshot.fingerprint}</code> <span className="muted">{uiText("| taken")} {snapshot.taken_at} {uiText("in")} {snapshot.duration_ms} ms</span></dd>
    </dl>
  )
}
