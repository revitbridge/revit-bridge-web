/* Execution outcome from Revit: flat objects as a table, anything else as JSON. */

import { uiText } from '../../utils/strings'
import type { ReactNode } from 'react'

export interface ExecOutcome {
  ok: boolean
  data: unknown
  error?: string | null
}

export default function ExecResult({ result }: { result: ExecOutcome }) {
  if (!result.ok) {
    return (
      <div className="notice execution-banner failed">
        <span>{uiText("Failed:")} {result.error || 'unknown error'}
        </span>
      </div>
    )
  }

  const data = result.data
  if (data === null || data === undefined) {
    return <p className="helper-text">{uiText("Success (no return data)")}</p>
  }

  if (typeof data === 'object' && !Array.isArray(data)) {
    const entries = Object.entries(data as Record<string, unknown>)
    return (
      <div className="card table-scroll">
        <table>
          <tbody>
            {entries.map(([key, val]) => (
              <tr key={key}>
                <td className="mono">{key}</td>
                <td>{renderValue(val)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <pre>
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}

function renderValue(val: unknown): ReactNode {
  if (val === null || val === undefined) return <span>null</span>
  if (typeof val === 'boolean') return <span>{String(val)}</span>
  if (typeof val === 'number') return <span>{val}</span>
  if (typeof val === 'string') return <span>{val}</span>
  return <pre>{JSON.stringify(val, null, 2)}</pre>
}
