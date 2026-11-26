import type { ReactNode } from 'react'
import { uiText } from '../../utils/strings'
import { boolean, numeric, problemField, shown, types, valueFor, type Pack } from './authoringPack'

export function Field({ path, problems, label, children }: { path: string; problems: string[]; label: string; children: ReactNode }) {
  const issues = problems.filter(p => problemField(p) === path)
  return <label className="authoring-field" data-field={path}>
    <span>{label}</span>{children}
    {issues.map((p, i) => <small className="text-danger" key={i}>{p}</small>)}
  </label>
}

export function ValueInput({ type, value, onChange, label, allowEmptyText }: {
  type: unknown; value: unknown; onChange: (v: unknown) => void; label: string; allowEmptyText?: boolean
}) {
  if (boolean(type)) return <select aria-label={label} value={shown(value)} onChange={e => onChange(valueFor(type, e.target.value))}>
    <option value="">—</option><option value="true">{uiText('Yes')}</option><option value="false">{uiText('No')}</option>
  </select>
  return <input aria-label={label} type={numeric(type) ? 'number' : 'text'} step={['int','integer'].includes(shown(type)) ? 1 : 'any'}
    value={shown(value)} onChange={e => onChange(allowEmptyText && !numeric(type) ? e.target.value : valueFor(type, e.target.value))} />
}

const columns = ['Name', 'Type', 'Unit', 'Required', 'Default', 'Description', 'Source']
const sources = ['designer', 'answer', 'default', 'tool:levels', 'tool:floor_types', 'tool:elements']
const sourceLabels: Record<string, string> = { designer: 'Designer', answer: 'Answer', default: 'Default',
  'tool:levels': 'Levels', 'tool:floor_types': 'Floor types', 'tool:elements': 'Elements' }

export function ParameterTable({ rows, problems, onChange }: { rows: Pack[]; problems: string[]; onChange: (rows: Pack[]) => void }) {
  function edit(i: number, patch: Pack) {
    const row = Object.fromEntries(Object.entries({ ...rows[i], ...patch }).filter(([, v]) => v !== undefined))
    onChange(rows.map((p, j) => j === i ? row : p))
  }
  function move(i: number, target: number) {
    const next = [...rows]
    next.splice(target, 0, ...next.splice(i, 1))
    onChange(next)
  }
  return <section><h3>{uiText('Parameters')}</h3>
    <div className="table-scroll"><table className="authoring-parameters"><thead><tr>
      {columns.map(c => <th key={c}>{uiText(c)}</th>)}<th>{uiText('Actions')}</th>
    </tr></thead><tbody>{rows.map((p, i) => {
      const prefix = `parameters[${i}]`
      const name = shown(p.name)
      const field = (key: string, column: string, input: ReactNode) =>
        <td><Field path={prefix + '.' + key} problems={problems} label={uiText(column) + ' ' + (i + 1)}>{input}</Field></td>
      const source = shown(p.source)
      return <tr key={i}>
        {field('name', 'Name', <input value={name} onChange={e => edit(i, { name: e.target.value })} />)}
        {field('type', 'Type', <select value={shown(p.type ?? 'string')} onChange={e => edit(i, { type: e.target.value,
          unit: numeric(e.target.value) ? p.unit : undefined, default: undefined })}>
          {p.type != null && !types.includes(shown(p.type)) && <option disabled>{shown(p.type)}</option>}
          {types.map(t => <option key={t}>{t}</option>)}</select>)}
        {field('unit', 'Unit', <select value={shown(p.unit)} onChange={e => edit(i, { unit: e.target.value || undefined })}>
          <option value="">—</option>{p.unit && !['mm', 'm', 'feet'].includes(shown(p.unit)) ? <option disabled>{shown(p.unit)}</option> : null}
          {['mm', 'm', 'feet'].map(u => <option key={u} disabled={!numeric(p.type)}>{u}</option>)}</select>)}
        {field('required', 'Required', <input type="checkbox" checked={p.required === true}
          onChange={e => edit(i, { required: e.target.checked, ...(e.target.checked ? { default: undefined,
            source: source === 'default' ? 'designer' : source } : {}) })} />)}
        {field('default', 'Default', <ValueInput type={p.type} label={uiText('Default') + ' ' + (i + 1)} value={p.default}
          onChange={v => edit(i, { default: v ?? (source === 'default' && !numeric(p.type) && !boolean(p.type) ? '' : undefined),
            ...(v !== undefined ? { required: false } : {}) })} />)}
        {field('description', 'Description', <input value={shown(p.description)} onChange={e => edit(i, { description: e.target.value })} />)}
        <td><Field path={prefix + '.source'} problems={problems} label={uiText('Source') + ' ' + (i + 1)}>
          <select value={source} onChange={e => edit(i, { source: e.target.value, choices_from: undefined,
          ...(e.target.value === 'default' ? { required: false, default: p.default ?? (numeric(p.type) ? 0 : boolean(p.type) ? false : '') } : {}) })}>
          {!sources.includes(source) && <option value={source} disabled>{source || '—'}</option>}
          {sources.map(s => <option key={s} value={s}>{uiText(sourceLabels[s])}</option>)}</select></Field>
          {p.choices_from !== undefined && <Field path={prefix + '.choices_from'} problems={problems} label={uiText('Choices from')}>
            <input value={shown(p.choices_from)} onChange={e => edit(i, { choices_from: e.target.value || undefined })} /></Field>}</td>
        <td><div className="toolbar">
          <button type="button" aria-label={uiText('Move up')} disabled={i === 0} onClick={() => move(i, i - 1)}>↑</button>
          <button type="button" aria-label={uiText('Move down')} disabled={i === rows.length - 1} onClick={() => move(i, i + 1)}>↓</button>
          <button type="button" onClick={() => onChange(rows.filter((_, j) => j !== i))}>{uiText('Remove')}</button>
        </div>{problems.filter(p => problemField(p) === prefix).map(p => <small className="text-danger" key={p}>{p}</small>)}</td>
      </tr>
    })}</tbody></table></div>
    {problems.filter(p => problemField(p) === 'parameters').map(p => <small className="text-danger" key={p}>{p}</small>)}
    <button type="button" onClick={() => onChange([...rows, { name: '', type: 'string', source: 'designer', required: true, description: '' }])}>
      {uiText('Add parameter')}</button>
  </section>
}
