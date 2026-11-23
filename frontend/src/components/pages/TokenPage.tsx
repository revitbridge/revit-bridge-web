/* Token: the measured runs (GET /api/usage/runs - the chats sent with an X-Run-Label, as
   the token bench does), read-only. Each run's calls and token counts, per role when the
   host gives them; pick two to see them side by side with the differences. Unknown counts
   show as '-'; a total with an unknown in it is unknown. Admin only on a cloud host. */

import { useEffect, useState } from 'react'
import { useStore } from 'zustand'
import { usageApi } from '../../api/usage'
import { getConfig } from '../../config'
import { useSessionStore } from '../../store'
import type { UsageRun } from '../../types/api'
import {
  compareRoles, compareRows, countsOf, formatCount, roleTotals, runTime, usageGate, type CompareRow, type Counts,
  type UsageGate,
} from '../token/runs'
import { createTokenFlow, type TokenFlow, type TokenState } from '../token/tokenFlow'

let sharedFlow: TokenFlow | null = null
function flowFor(flow?: TokenFlow): TokenFlow {
  if (flow) return flow
  sharedFlow ??= createTokenFlow(usageApi)
  return sharedFlow
}

function CountCells({ counts }: { counts: Counts }) {
  return (
    <>
      <td>{formatCount(counts.calls)}</td>
      <td>{formatCount(counts.input)}</td>
      <td>{formatCount(counts.cache_read)}</td>
      <td>{formatCount(counts.cache_write)}</td>
      <td>{formatCount(counts.output)}</td>
      <td>{formatCount(counts.ms)}</td>
    </>
  )
}

function RunRows({ run, picked, onToggle }: { run: UsageRun; picked: number; onToggle: () => void }) {
  return (
    <>
      <tr className={`token-run${picked >= 0 ? ' is-open' : ''}`} data-run={run.run_label}>
        <td>
          <label className="token-pick">
            <input type="checkbox" checked={picked >= 0} onChange={onToggle} aria-label={`compare ${run.run_label}`} />
            {picked >= 0 ? (picked === 0 ? ' A' : ' B') : ''}
          </label>
        </td>
        <td className="mono">{run.run_label}</td>
        <td>{runTime(run)}</td>
        <CountCells counts={countsOf(run)} />
      </tr>
      {roleTotals(run).map(part => (
        <tr key={part.role} className="token-role-row" data-role={part.role}>
          <td />
          <td colSpan={2}>{part.role} <span className="mono">({part.models.join(', ')})</span></td>
          <CountCells counts={part.counts} />
        </tr>
      ))}
    </>
  )
}

function CompareTable({ rows, a, b }: { rows: CompareRow[]; a: string; b: string }) {
  return (
    <div className="table-scroll">
      <table className="evidence-table token-compare">
        <thead><tr><th />{[a, b].map((label, i) => <th key={i}>{label}</th>)}<th>B - A</th></tr></thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.key} data-metric={row.key}>
              <td>{row.label}</td><td>{row.a}</td><td>{row.b}</td>
              <td className={`token-diff ${row.direction}`}>{row.diff}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Comparison({ a, b }: { a: UsageRun; b: UsageRun }) {
  const roles = compareRoles(a, b)
  return (
    <section className="card section token-comparison">
      <h3 className="heading-display section-title">A: {a.run_label} - B: {b.run_label}</h3>
      <CompareTable rows={compareRows(countsOf(a), countsOf(b))} a={`A: ${a.run_label}`} b={`B: ${b.run_label}`} />
      {roles.map(role => (
        <div key={role.role} className="token-role-compare" data-role={role.role}>
          <h4 className="label-text">{role.role}</h4>
          <CompareTable rows={role.rows} a={`A (${role.modelsA})`} b={`B (${role.modelsB})`} />
        </div>
      ))}
    </section>
  )
}

export interface TokenViewProps {
  state: TokenState
  pair: [UsageRun, UsageRun] | null
  gate: UsageGate
  adminToken: string
  onRefresh: () => void
  onToggle: (label: string) => void
  onAdminToken: (token: string) => void
}

/* The page as its state draws it (TokenPage feeds it from the flow). */
export function TokenView({ state, pair, gate, adminToken, onRefresh, onToggle, onAdminToken }: TokenViewProps) {
  return (
    <div className="page">
      <section className="card section">
        <div className="panel-head">
          <h3 className="heading-display section-title">Token</h3>
          <button className="btn-secondary" onClick={onRefresh} disabled={state.loading || !gate.canRead}>
            {state.loading ? 'Loading...' : 'Refresh'}
          </button>
        </div>
        <p className="section-copy small" style={{ marginTop: 0 }}>
          The runs measured with a run label (the <code>X-Run-Label</code> header the token bench sends): calls and
          tokens per run, and per role and model. Read-only. A count the provider did not report shows as -, and so
          does any total that includes one. Pick two runs to compare them.
        </p>
        {gate.askToken && (
          <input type="password" className="input-field" value={adminToken} onChange={e => onAdminToken(e.target.value)}
            placeholder="Admin password" autoComplete="off" style={{ maxWidth: 320 }} />
        )}
        {gate.note && <div className="status-line">{gate.note}</div>}
        {state.notice && <div className="status-line">{state.notice}</div>}
        {state.runs && state.runs.length === 0 && (
          <p className="section-copy small">No measured runs yet: chats sent without a run label are not listed here.</p>
        )}
        {state.runs && state.runs.length > 0 && (
          <div className="table-scroll">
            <table className="evidence-table token-runs">
              <thead>
                <tr>
                  <th>Compare</th><th>Run</th><th>Time</th><th>Calls</th><th>Input</th><th>Cache read</th>
                  <th>Cache write</th><th>Output</th><th>Time (ms)</th>
                </tr>
              </thead>
              <tbody>
                {state.runs.map(run => (
                  <RunRows key={run.run_label} run={run} picked={state.selected.indexOf(run.run_label)}
                    onToggle={() => onToggle(run.run_label)} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        {state.selected.length === 1 && <p className="section-copy small">Pick one more run to compare.</p>}
      </section>
      {pair && <Comparison a={pair[0]} b={pair[1]} />}
    </div>
  )
}

export default function TokenPage({ flow: given }: { flow?: TokenFlow }) {
  const [flow] = useState(() => flowFor(given))
  const state = useStore(flow.store)
  const adminToken = useSessionStore(s => s.adminToken)
  const setAdminToken = useSessionStore(s => s.setAdminToken)
  const { mode, admin } = getConfig().features
  const gate = usageGate(mode, admin, adminToken)

  // Once on opening (when the host lets this tab read); after that, Refresh.
  useEffect(() => { if (gate.canRead) flow.load() }, [flow]) // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <TokenView state={state} pair={flow.pair(state)} gate={gate} adminToken={adminToken}
      onRefresh={() => flow.load()} onToggle={label => flow.toggle(label)} onAdminToken={setAdminToken} />
  )
}
