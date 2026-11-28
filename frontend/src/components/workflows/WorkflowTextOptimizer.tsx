import { useEffect, useId, useRef, useState } from 'react'
import { BridgeApiError } from '../../api/client'
import { workflowApi, type TextSuggestion, type Workflow } from '../../api/workflows'
import { getErrorMessage } from '../../utils/errors'
import { browserLanguage } from '../../utils/language'
import { uiText } from '../../utils/strings'

export default function WorkflowTextOptimizer({ draft, modelAvailable, apply }: {
  draft: Workflow
  modelAvailable: boolean
  apply: (text: Partial<Pick<Workflow, 'summary' | 'description'>>) => void
}) {
  const id = useId()
  const request = useRef<AbortController | null>(null)
  const [pending, setPending] = useState(false)
  const [suggestion, setSuggestion] = useState<TextSuggestion>()
  const [error, setError] = useState('')
  useEffect(() => () => request.current?.abort(), [])

  async function optimize() {
    if (request.current) return
    const controller = new AbortController()
    request.current = controller
    setPending(true)
    setError('')
    try {
      const reply = await workflowApi.optimizeText(draft, browserLanguage(), controller.signal)
      if (!controller.signal.aborted) setSuggestion(reply)
    } catch (e) {
      if (!controller.signal.aborted) {
        const message = e instanceof BridgeApiError && e.body?.message || getErrorMessage(e)
        setError(message.replace(/\s+/g, ' ').trim())
      }
    } finally {
      if (!controller.signal.aborted) {
        request.current = null
        setPending(false)
      }
    }
  }

  return <div className="workflow-text-optimizer">
    <button type="button" disabled={pending || !modelAvailable} onClick={() => void optimize()}>{uiText("Optimize text")}</button>
    <span role="status">{suggestion && !pending && !error ? uiText("Text suggestion received.") : ''}</span>
    {error && <p role="alert">{error}</p>}
    {suggestion && <div className="workflow-text-review">
      {(['summary', 'description'] as const).map(field => <section key={field} aria-labelledby={id + field}>
        <h3 id={id + field}>{uiText(field === 'summary' ? "Summary" : "Description")}</h3>
        <dl className="workflow-text-comparison">
          <div><dt>{uiText("Current")}</dt><dd>{draft[field]}</dd></div>
          <div><dt>{uiText("Suggested")}</dt><dd>{suggestion[field]}</dd></div>
        </dl>
        {suggestion[field] === draft[field] ? <small>{uiText("No change suggested")}</small>
          : <button type="button" onClick={() => apply({ [field]: suggestion[field] })}>{uiText("Use")}</button>}
      </section>)}
      <div className="workflow-actions">
        <button type="button" disabled={draft.summary === suggestion.summary && draft.description === suggestion.description}
          onClick={() => apply({ summary: suggestion.summary, description: suggestion.description })}>{uiText("Use both")}</button>
        <button type="button" onClick={() => setSuggestion(undefined)}>{uiText("Dismiss")}</button>
        <small>{uiText("Model")} {suggestion.model}</small>
      </div>
    </div>}
  </div>
}
