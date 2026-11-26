import { uiText } from '../../utils/strings'
import { useExecutionMode } from './executionPreference'

export function ExecutionMode({ disabled }: { disabled: boolean }) {
  const { auto, set } = useExecutionMode()
  return <div><label>{uiText("Execution ")}
    <select value={auto ? 'auto' : 'confirm'} disabled={disabled} onChange={e => set(e.target.value === 'auto')}>
      <option value="confirm">{uiText("Confirm each step")}</option>
      <option value="auto">{uiText("Run automatically")}</option>
    </select></label>
    {auto && <p>{uiText("Ready cards run directly in Revit.")}</p>}
  </div>
}
