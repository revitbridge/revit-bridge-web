import type { ExecutionResult, TaskPlan } from '../../types/api'
import { browserLanguage } from '../../utils/language'
import { planStatus } from './plan'

export default function PlanChecklist({ plan, results }: { plan: TaskPlan; results?: Record<string, ExecutionResult> }) {
  const zh = browserLanguage() === 'zh'
  return <ol className="task-plan" aria-label={zh ? '任务计划' : 'Task plan'}>
    {plan.steps.map(step => {
      const { icon, label } = planStatus(step.status, zh)
      const result = results && Object.prototype.hasOwnProperty.call(results, step.id) ? results[step.id] : undefined
      const title = <><span className="plan-step-icon" aria-hidden="true">{icon}</span>
        <span>{step.title}</span><small>{label}</small></>
      return <li key={step.id} data-status={step.status}>
        {result || step.evidence_id ? <details>
          <summary>{title}</summary>
          {step.evidence_id && <code>{step.evidence_id}</code>}
          {result && <pre>{JSON.stringify(result.result ?? result.message ?? result.error ?? { success: result.success }, null, 2)}</pre>}
        </details> : <div>{title}</div>}
      </li>
    })}
  </ol>
}
