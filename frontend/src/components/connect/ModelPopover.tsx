import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch } from '../../api/client'
import { defaultModel, getConfig, SELECTOR_ROLES, subscribeConfig, type SelectionRole } from '../../config'
import { useSessionStore } from '../../store'
import { uiText } from '../../utils/strings'
import Icon from '../shared/Icon'
import { applyModelDetails, endpoint, type ModelDetails } from './modelLibrary'

const roleLabels = { planner: 'Planner', supervisor: 'Supervisor', analyst: 'Analyst', optimizer: 'Optimizer' }

export function ModelSelectors() {
  const { features } = useSyncExternalStore(subscribeConfig, getConfig, getConfig)
  const token = useSessionStore(s => s.adminToken)
  const locked = features.mode === 'cloud' && !token
  const [pending, setPending] = useState<{ role: SelectionRole; entry: string } | null>(null)
  const [failed, setFailed] = useState(false)
  const { modelLibrary, modelSelection } = features

  async function change(role: SelectionRole, entry: string) {
    setPending({ role, entry })
    setFailed(false)
    try {
      const details = await apiFetch<ModelDetails>(endpoint + '/selection', {
        method: 'PUT', redirect: 'error', cache: 'no-store', body: JSON.stringify({ role, entry: entry || null }),
      })
      applyModelDetails(details)
    } catch { setFailed(true) }
    finally { setPending(null) }
  }

  return <div className="model-selectors">
    {modelLibrary.length === 0 && <p>{uiText('The model library is empty.')}</p>}
    {SELECTOR_ROLES.map(role => {
      const selected = (pending?.role === role ? pending.entry : modelSelection[role]) || ''
      const entry = modelLibrary.find(item => item.name === selected)
      const fallback = defaultModel(role)
      return <div key={role}>
        <label><span>{uiText(roleLabels[role])}</span>
          <select value={selected} disabled={locked || !!pending || (!modelLibrary.length && !selected)}
            onChange={e => void change(role, e.target.value)}>
            <option value="">{fallback === undefined ? uiText('Default')
              : uiText('Default ({0})', fallback || uiText('Not set'))}</option>
            {selected && !entry && <option disabled value={selected}>{selected}</option>}
            {modelLibrary.map(item => <option value={item.name} key={item.name}>{item.name} - {item.model}</option>)}
          </select>
        </label>
        {role === 'planner' && entry && !entry.vision &&
          <p className="section-copy small">{uiText('This model cannot see sketches or captures.')}</p>}
      </div>
    })}
    {locked && <p className="section-copy">{uiText('An administrator chooses the models.')}</p>}
    {failed && <p role="alert">{uiText('Could not change the model.')}</p>}
  </div>
}

export default function ModelPopover({ model, onManage }: { model: string; onManage: () => void }) {
  const [open, setOpen] = useState(false)
  const wrapper = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const heading = useId()
  useEffect(() => {
    if (!open) return
    const outside = (e: MouseEvent) => {
      if (e.target instanceof Node && !wrapper.current?.contains(e.target)) setOpen(false)
    }
    const escape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setOpen(false); button.current?.focus() }
    }
    document.addEventListener('click', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('click', outside); document.removeEventListener('keydown', escape) }
  }, [open])

  return <div className="model-picker" ref={wrapper}>
    <button ref={button} type="button" className="runtime-chip model-chip" aria-haspopup="dialog"
      aria-expanded={open} onClick={() => setOpen(!open)}>
      <Icon name="model" /><span>{uiText('Model')}</span><strong>{model}</strong>
    </button>
    {open && <section role="dialog" aria-labelledby={heading} className="model-popover">
      <h3 id={heading}>{uiText('Models')}</h3>
      <ModelSelectors />
      <a href="/settings" onClick={e => { e.preventDefault(); setOpen(false); onManage() }}>{uiText('Manage models')}</a>
    </section>}
  </div>
}
