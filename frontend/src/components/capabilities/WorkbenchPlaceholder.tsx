/* The workbench page until the workbench component (write a snippet, check, dry-run, save
   as a tool) is wired in: a note, and the designer's own tools - export, delete, import - so
   the routes behind it can be used now. Every call goes through api/workbench.ts. */

import { useCallback, useEffect, useState } from 'react'
import { workbenchApi, type WorkbenchTool } from '../../api/workbench'
import { getErrorMessage } from '../../utils/errors'
import { usePackGate } from '../shared/packAdmin'
import { importPack, ownTools } from './ownTools'

type Api = Pick<typeof workbenchApi, 'list' | 'remove' | 'exportYaml' | 'importYaml'>

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/yaml' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${name}.yaml`
  link.click()
  // Revoked once the browser has taken the download, not before (some start it asynchronously).
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

export default function WorkbenchPlaceholder({ api = workbenchApi }: { api?: Api }) {
  const gate = usePackGate()
  const [tools, setTools] = useState<WorkbenchTool[] | null>(null)
  const [status, setStatus] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try { setTools(ownTools(await api.list())) }
    catch (e: unknown) { setStatus({ tone: 'error', text: `Cannot list tools: ${getErrorMessage(e)}` }) }
  }, [api])

  useEffect(() => { refresh() }, [refresh])

  const act = async (work: () => Promise<string | null>) => {
    setBusy(true)
    try {
      const done = await work()
      if (done) setStatus({ tone: 'ok', text: done })
      await refresh()
    } catch (e: unknown) {
      setStatus({ tone: 'error', text: getErrorMessage(e) })
    } finally {
      setBusy(false)
    }
  }

  const exportTool = (name: string) => act(async () => { download(name, await api.exportYaml(name)); return `Exported ${name}.yaml.` })
  const removeTool = (name: string) => act(async () => {
    if (!window.confirm(`Delete the tool '${name}'?`)) return null
    await api.remove(name)
    return `Deleted ${name}.`
  })
  const importFile = (file: File) => act(async () => {
    const result = await importPack(api, await file.text(),
      name => window.confirm(`A tool named '${name}' exists. Replace it with ${file.name}?`))
    return result ? `Imported ${result.name}${result.replaced ? ' (replaced)' : ''}.` : null
  })

  const directories = [...new Set((tools ?? []).map(t => t.directory).filter(Boolean))]

  return (
    <div className="space-y-4">
      <div className="card section">
        <h3 className="heading-display section-title">Workbench</h3>
        <p className="section-copy small" style={{ marginTop: 0 }}>
          The workbench UI is being wired in: here you will paste or write a C# snippet, check it, dry-run it
          against the open model (rolled back, nothing kept) and save it as a tool. The model in the chat
          writes no code; it runs saved tools only and sends you here when none fits.
        </p>
        <p className="section-copy small" style={{ margin: 0 }}>
          Your tools are shared with every host that uses the same tool directory - on this machine the
          revit-bridge MCP server of Claude Code and the other coding frameworks.
          {directories.length > 0 && <> Directory: {directories.map(d => <code key={d}>{d}</code>)}</>}
          {gate.note && ` ${gate.note}`}
        </p>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <button onClick={() => { setStatus(null); refresh() }} className="btn-secondary" disabled={busy}>Refresh</button>
        {gate.canEdit && (
          <label className="btn-secondary" style={{ cursor: busy ? 'default' : 'pointer' }}>
            Import a tool (.yaml)
            <input type="file" accept=".yaml,.yml,text/yaml" style={{ display: 'none' }} disabled={busy}
              onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) importFile(file) }} />
          </label>
        )}
        <span className="section-copy small">
          {tools === null ? 'Loading your tools...' : `${tools.length} tool${tools.length === 1 ? '' : 's'} of your own`}
        </span>
      </div>
      {status && <p className={status.tone === 'ok' ? 'tool-review-status' : 'tool-warning-status'}>{status.text}</p>}

      {tools !== null && tools.length > 0 && (
        <div className="card table-scroll">
          <table className="w-full" style={{ fontFamily: 'var(--mono)', fontSize: 12 }}>
            <thead>
              <tr style={{ background: 'var(--bg2)' }}>
                <th className="px-3 py-2 text-left label-text">Name</th>
                <th className="px-3 py-2 text-left label-text">Description</th>
                <th className="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {tools.map(t => (
                <tr key={t.name} style={{ borderBottom: '1px solid var(--line)' }}>
                  <td className="px-3 py-1.5" style={{ fontWeight: 500 }}>{t.name}</td>
                  <td className="px-3 py-1.5" style={{ fontFamily: 'var(--serif)', fontSize: 13, color: 'var(--mid)' }}>{t.description}</td>
                  <td className="px-3 py-1.5" style={{ whiteSpace: 'nowrap' }}>
                    <button className="btn-ghost" onClick={() => exportTool(t.name)} disabled={busy}>Export</button>
                    {gate.canEdit && <button className="btn-ghost danger" onClick={() => removeTool(t.name)} disabled={busy}>Delete</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {tools !== null && tools.length === 0 && (
        <p className="section-copy small">No tools of your own yet. Built-in tools are on the Packs page (read-only).</p>
      )}
    </div>
  )
}
