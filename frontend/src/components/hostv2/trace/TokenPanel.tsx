import { uiText } from '../../../utils/strings'
import type { Role, UsageEvent } from '../../../types/hostv2'
import { sumUsage, type UsageTotal } from './events'
import './trace.css'

const roles: Role[] = ['analyst', 'planner', 'supervisor']
// A count the provider did not report is '-', as on the Token page - never 0.
const format = (value: number | null) => value === null ? '-' : value.toLocaleString('en-US')
const rate = (value: number | null) => value === null ? '-' : `${(value * 100).toFixed(1)}%`

function UsageRow({ total, role, showRole }: { total: UsageTotal; role: string; showRole: boolean }) {
  return (
    <tr>
      {showRole && <th scope="row">{uiText(role)}</th>}
      <td data-label={uiText("Input")}>{format(total.input)}</td>
      <td data-label={uiText("Cache read")}>{format(total.cache_read)}</td>
      <td data-label={uiText("Hit rate")}>{rate(total.cache_hit_rate)}</td>
      <td data-label={uiText("Cache write")}>{format(total.cache_write)}</td>
      <td data-label={uiText("Output")}>{format(total.output)}</td>
      <td data-label={uiText("Calls")}>{total.calls}</td>
    </tr>
  )
}

function UsageColumn({ title, events }: { title: string; events: UsageEvent[] }) {
  const total = sumUsage(events)
  const present = roles.filter(role => events.some(event => event.role === role))
  // One row per role whenever the roles ran (one loop: the planner alone, the total only).
  const showRole = present.some(role => role !== 'planner')
  return (
    <section className="hostv2-token-column" aria-label={title}>
      <h3>{title}</h3>
      <table className="hostv2-token-table">
        <caption className="hostv2-trace-sr-only">{title} {uiText("token usage")}</caption>
        <thead><tr>
          {showRole && <th scope="col">{uiText("Role")}</th>}
          <th scope="col">{uiText("Input")}</th>
          <th scope="col">{uiText("Cache read")}</th>
          <th scope="col">{uiText("Hit rate")}</th>
          <th scope="col">{uiText("Cache write")}</th>
          <th scope="col">{uiText("Output")}</th>
          <th scope="col">{uiText("Calls")}</th>
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

export function TokenPanel({ turn, session, title = 'Tokens' }: { turn: UsageEvent[]; session: UsageEvent[]; title?: string }) {
  const turnTotal = sumUsage(turn)
  const sessionTotal = sumUsage(session)
  return (
    <details className="hostv2-tokens">
      <summary>
        <strong>{uiText(title)}</strong>
        <span className="hostv2-token-summary">{uiText("This turn:")} {format(turnTotal.input)} {uiText("in /")} {format(turnTotal.output)} {uiText("out (")}{turnTotal.calls} {uiText("calls)")}{' | '}{uiText("Session:")} {format(sessionTotal.input)} {uiText("in /")} {format(sessionTotal.output)} {uiText("out (")}{sessionTotal.calls} {uiText("calls)")}</span>
      </summary>
      <div className="hostv2-token-columns">
        <UsageColumn title={uiText("This turn")} events={turn} />
        <UsageColumn title={uiText("This session")} events={session} />
      </div>
    </details>
  )
}

export default TokenPanel
