import { useId, useRef } from 'react'

interface Option { value: string; label: string; disabled?: boolean }

/** The native select owns the focus, keyboard interaction and accessible name.
 * The segments are a pointer surface for the same options and value. */
export default function SegmentedSelect({ label, options, value, change, disabled }: {
  label: string; options: Option[]; value: string; change: (value: string) => void; disabled?: boolean
}) {
  const id = useId()
  const select = useRef<HTMLSelectElement>(null)
  return <div className="segmented-field">
    <label htmlFor={id}>{label}</label>
    <div className="segments">
      <select ref={select} className="sr-only" id={id} value={value} onChange={e => change(e.target.value)} disabled={disabled}>
        {options.map(o => <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>)}
      </select>
      {options.map(o => <button key={o.value} type="button" aria-hidden="true" tabIndex={-1}
        className={o.value === value ? 'is-active' : undefined} disabled={disabled || o.disabled}
        onClick={() => {
          select.current?.focus({ preventScroll: true })
          if (o.value !== value) change(o.value)
        }}>{o.label}</button>)}
    </div>
  </div>
}
