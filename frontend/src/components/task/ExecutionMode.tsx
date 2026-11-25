import { browserLanguage } from '../../utils/language'
import { useExecutionMode } from './executionPreference'

export function ExecutionMode({ disabled }: { disabled: boolean }) {
  const { auto, set } = useExecutionMode()
  const zh = browserLanguage() === 'zh'
  return <div><label>{zh ? '执行方式 ' : 'Execution '}
    <select value={auto ? 'auto' : 'confirm'} disabled={disabled} onChange={e => set(e.target.value === 'auto')}>
      <option value="confirm">{zh ? '逐步确认' : 'Confirm each step'}</option>
      <option value="auto">{zh ? '自动执行' : 'Run automatically'}</option>
    </select></label>
    {auto && <p>{zh ? '卡片就绪后会直接在 Revit 里执行' : 'Ready cards run directly in Revit.'}</p>}
  </div>
}
