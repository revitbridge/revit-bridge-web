/* Step progress bar */

import { uiText } from '../../utils/strings'
interface Props {
  steps: string[]
  current: number // 1-based
}

export default function StepIndicator({ steps, current }: Props) {
  return (
    <div className="steps-indicator">
      {steps.map((label, i) => {
        const step = i + 1
        const isDone = step < current
        const isActive = step === current
        return (
          <span key={i} className="flex items-center gap-1">
            {i > 0 && <span style={{ color: 'var(--subtle)', margin: '0 4px' }}>&rarr;</span>}
            <span className={isActive ? 'is-active' : undefined}>
              {isDone ? '\u2713' : isActive ? '\u25B6' : ''} {step}.{uiText(label)}
            </span>
          </span>
        )
      })}
    </div>
  )
}
