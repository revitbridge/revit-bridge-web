import { uiText } from '../../utils/strings'
import SegmentedSelect from '../shared/SegmentedSelect'
import { useExecutionMode } from './executionPreference'

export function ExecutionMode({ disabled }: { disabled: boolean }) {
  const { auto, set } = useExecutionMode()
  return <div className="execution-mode"><SegmentedSelect label={uiText("Execution ")} value={auto ? 'auto' : 'confirm'}
    disabled={disabled} change={mode => set(mode === 'auto')} options={[
      { value: 'confirm', label: uiText("Confirm each step") }, { value: 'auto', label: uiText("Run automatically") },
    ]} />
    {auto && <p>{uiText("Ready cards run directly in Revit.")}</p>}
  </div>
}
