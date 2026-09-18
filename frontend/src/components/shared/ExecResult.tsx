/* Execution outcome from Revit: flat objects as a table, anything else as JSON. */

import type { ReactNode } from 'react'

export interface ExecOutcome {
  ok: boolean
  data: unknown
  error?: string | null
}

export default function ExecResult({ result }: { result: ExecOutcome }) {
  if (!result.ok) {
    return (
      <div style={{ marginTop: 8, padding: 12, background: 'rgba(179, 59, 46, 0.1)', border: '1px solid var(--danger)', borderRadius: 'var(--radius-sm)' }}>
        <span style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--danger)', whiteSpace: 'pre-wrap' }}>
          Failed: {result.error || 'unknown error'}
        </span>
      </div>
    )
  }

  const data = result.data
  if (data === null || data === undefined) {
    return <p style={{ fontFamily: 'var(--mono)', fontSize: 12, color: 'var(--mid)', marginTop: 8 }}>Success (no return data)</p>
  }

  if (typeof data === 'object' && !Array.isArray(data)) {
    const entries = Object.entries(data as Record<string, unknown>)
    return (
      <div style={{ marginTop: 8, border: '1px solid var(--line)', borderRadius: 'var(--radius-sm)', overflow: 'hidden' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: 'var(--mono)', fontSize: 12 }}>
          <tbody>
            {entries.map(([key, val]) => (
              <tr key={key} style={{ borderBottom: '1px solid var(--line)' }}>
                <td style={{ padding: '6px 12px', background: 'var(--bg2)', color: 'var(--mid)', fontWeight: 500, whiteSpace: 'nowrap', width: 1 }}>{key}</td>
                <td style={{ padding: '6px 12px', color: 'var(--dark)' }}>{renderValue(val)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <pre style={{ marginTop: 8, background: 'var(--bg2)', padding: 12, borderRadius: 'var(--radius-sm)', fontFamily: 'var(--mono)', fontSize: 12, whiteSpace: 'pre-wrap', border: '1px solid var(--line)', maxHeight: 360, overflow: 'auto' }}>
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}

function renderValue(val: unknown): ReactNode {
  if (val === null || val === undefined) return <span style={{ color: 'var(--faint)', fontStyle: 'italic' }}>null</span>
  if (typeof val === 'boolean') return <span style={{ color: val ? 'var(--mid)' : 'var(--accent)' }}>{String(val)}</span>
  if (typeof val === 'number') return <span style={{ fontWeight: 500 }}>{val}</span>
  if (typeof val === 'string') return <span>{val}</span>
  return <pre style={{ margin: 0, whiteSpace: 'pre-wrap' }}>{JSON.stringify(val, null, 2)}</pre>
}
