import type { Role, UsageEvent } from '../../../types/hostv2'
import { sumUsage, type UsageTotal } from './events'
import './trace.css'

const roles: Role[] = ['analyst', 'planner', 'supervisor']
const format = (value: number | null) => value === null ? 'unknown' : value.toLocaleString('en-US')
const rate = (value: number | null) => value === null ? 'unknown' : `${(value * 100).toFixed(1)}%`

function UsageRow({ total, role, showRole }: { total: UsageTotal; role: string; showRole: boolean }) {
  return (
    <tr>
      {showRole && <th scope="row">{role}</th>}
      <td data-label="Input">{format(total.input)}</td>
      <td data-label="Cache read">{format(total.cache_read)}</td>
      <td data-label="Hit rate">{rate(total.cache_hit_rate)}</td>
      <td data-label="Cache write">{format(total.cache_write)}</td>
      <td data-label="Output">{format(total.output)}</td>
      <td data-label="Calls">{total.calls}</td>
    </tr>
  )
}

function UsageColumn({ title, events }: { title: string; events: UsageEvent[] }) {
  const total = sumUsage(events)
  const present = roles.filter(role => events.some(event => event.role === role))
  const showRole = present.length > 1
  return (
    <section className="hostv2-token-column" aria-label={title}>
      <h3>{title}</h3>
      <table className="hostv2-token-table">
        <caption className="hostv2-trace-sr-only">{title} token usage</caption>
        <thead><tr>
          {showRole && <th scope="col">Role</th>}
          <th scope="col">Input</th>
          <th scope="col">Cache read</th>
          <th scope="col">Hit rate</th>
          <th scope="col">Cache write</th>
          <th scope="col">Output</th>
          <th scope="col">Calls</th>
        </tr></thead>
        <tbody>
          {showRole && present.map(role => (
            <UsageRow key={role} role={role} showRole total={sumUsage(events.filter(event => event.role === role))} />
          ))}
          <UsageRow total={total} role="Total" showRole={showRole} />
        </tbody>
      </table>
    </section>
  )
}

export function TokenPanel({ turn, session }: { turn: UsageEvent[]; session: UsageEvent[] }) {
  const turnTotal = sumUsage(turn)
  const sessionTotal = sumUsage(session)
  return (
    <details className="hostv2-tokens">
      <summary>
        <strong>Tokens</strong>
        <span className="hostv2-token-summary">
          This turn: {format(turnTotal.input)} in / {format(turnTotal.output)} out ({turnTotal.calls} calls)
          {' | '}Session: {format(sessionTotal.input)} in / {format(sessionTotal.output)} out ({sessionTotal.calls} calls)
        </span>
      </summary>
      <div className="hostv2-token-columns">
        <UsageColumn title="This turn" events={turn} />
        <UsageColumn title="This session" events={session} />
      </div>
    </details>
  )
}

export default TokenPanel
