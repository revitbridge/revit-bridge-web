import type { StageEvent, ToolEvent, TraceEvent } from '../../../types/hostv2'
import { groupTraceEvents, truncatedArgs } from './events'
import './trace.css'

const STAGE_STATE: Record<StageEvent['status'], string> = {
  start: 'in progress',
  end: 'complete',
  interrupted: 'interrupted',
}

function ToolRow({ tool }: { tool: ToolEvent }) {
  const truncated = truncatedArgs(tool.args)
  const args = truncated ? `${truncated.head}...` : JSON.stringify(tool.args)
  const argsTitle = truncated ? `${args} (${truncated.chars.toLocaleString('en-US')} characters)` : args
  return (
    <li className={`hostv2-trace-tool is-${tool.status}`}>
      <details>
        <summary className="hostv2-trace-summary">
          <span className="hostv2-trace-name">{tool.name}</span>
          <span className="hostv2-trace-args" title={argsTitle}>{args}</span>
          <span className="hostv2-trace-status">
            {tool.status === 'running' && <span className="hostv2-trace-running" aria-hidden="true" />}
            {tool.status}
          </span>
          <span className="hostv2-trace-metric">{tool.ms === undefined ? 'time unknown' : `${tool.ms} ms`}</span>
          <span className="hostv2-trace-metric">{tool.size === undefined ? 'size unknown' : `${tool.size.toLocaleString('en-US')} characters`}</span>
        </summary>
        <div className="hostv2-trace-detail">
          {truncated
            ? <>
                <h4>Parameters (truncated: the first {truncated.head.length.toLocaleString('en-US')} of {truncated.chars.toLocaleString('en-US')} characters)</h4>
                <pre tabIndex={0} aria-label={`${tool.name} parameters, truncated`}>{truncated.head}</pre>
              </>
            : <>
                <h4>Parameters</h4>
                <pre tabIndex={0} aria-label={`${tool.name} parameters`}>{JSON.stringify(tool.args, null, 2)}</pre>
              </>}
          {tool.preview !== undefined && <>
            <h4>Result preview</h4>
            <pre tabIndex={0} aria-label={`${tool.name} result preview`}>{tool.preview}</pre>
          </>}
        </div>
      </details>
      {tool.status === 'error' && <p className="hostv2-trace-error">{tool.error || 'Tool failed without an error message.'}</p>}
      {tool.status === 'interrupted' && <p className="hostv2-trace-interrupted">The turn ended before this call reported back.</p>}
    </li>
  )
}

export function TraceTimeline({ events }: { events: TraceEvent[] }) {
  const groups = groupTraceEvents(events)
  if (groups.length === 0) return null
  return (
    <section className="hostv2-trace" aria-label="Tool activity">
      {groups.map((group, index) => (
        <div className="hostv2-trace-group" key={index}>
          {group.stage && (
            <h3 className="hostv2-trace-stage">
              <span>{group.stage.role}</span>
              <span className="hostv2-trace-model">{group.stage.model}</span>
              <span className={`hostv2-trace-stage-state is-${group.stage.status}`}>{STAGE_STATE[group.stage.status]}</span>
            </h3>
          )}
          {group.tools.length > 0 && <ol className="hostv2-trace-tools">
            {group.tools.map(tool => <ToolRow key={tool.id} tool={tool} />)}
          </ol>}
        </div>
      ))}
    </section>
  )
}

export default TraceTimeline
