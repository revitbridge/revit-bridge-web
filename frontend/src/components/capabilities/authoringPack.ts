import { uiText } from '../../utils/strings'

export type Pack = Record<string, unknown>
export const isObject = (item: unknown): item is Pack => !!item && typeof item === 'object' && !Array.isArray(item)
export const shown = (v: unknown) => v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v)
export const paramRows = (pack: Pack) => Array.isArray(pack.parameters) ? pack.parameters.map(p => isObject(p) ? p : {}) : []
export const types = ['string', 'double', 'int', 'bool', 'float', 'number', 'integer', 'boolean']
export const numeric = (type: unknown) => ['double', 'int', 'float', 'number', 'integer'].includes(shown(type).toLowerCase())
export const boolean = (type: unknown) => ['bool', 'boolean'].includes(shown(type).toLowerCase())
export const blankPack = () => ({ schema_version: 1, version: '1.0.0', name: '', description: '', parameters: [], code_template: '' })
const supported = new Set(Object.keys(blankPack()).concat('source_query', 'validator'))
const parameterFields = new Set(['name', 'type', 'description', 'default', 'unit', 'source', 'required', 'choices_from'])

export function unsupportedFields(pack: Pack) {
  const fields = Object.keys(pack).filter(k => !supported.has(k))
  if (pack.version !== '1.0.0') fields.push('version')
  if (pack.schema_version !== 1) fields.push('schema_version')
  paramRows(pack).forEach((row, i) => {
    for (const k of Object.keys(row)) if (!parameterFields.has(k)) fields.push(`parameters[${i}].${k}`)
  })
  return fields
}

export function removeUnsupported(pack: Pack): Pack {
  const cleaned = Object.fromEntries(Object.entries(pack).filter(([k]) => supported.has(k)))
  if (Array.isArray(cleaned.parameters)) cleaned.parameters = cleaned.parameters.map(p => isObject(p)
    ? Object.fromEntries(Object.entries(p).filter(([k]) => parameterFields.has(k))) : p)
  return { ...cleaned, version: '1.0.0', schema_version: 1 }
}

// The validator uses both "parameters[i].field" and "parameters[i] (name): unknown type ...".
export function problemField(problem: string): string | undefined {
  const row = /^parameters\[(\d+)\](?:\.(\w+))?(?:\s*\([^)]*\))?:?\s*(.*)/i.exec(problem)
  if (row) {
    const prefix = `parameters[${row[1]}]`
    const detail = row[3]
    const field = row[2] || (/declared twice|^name\b/.test(detail) ? 'name'
      : /source default needs a default/.test(detail) ? 'default'
      : /with a default is not required/.test(detail) ? 'required'
      : detail.match(/\b(source|type|unit|required|default|description|choices_from)\b/)?.[1])
    return field ? prefix + '.' + field : prefix
  }
  return /^(name|description|code_template|parameters)\b/.exec(problem)?.[1]
}

export function valueFor(type: unknown, text: string): unknown {
  if (text === '') return undefined
  if (boolean(type)) return text === 'true'
  if (numeric(type) && text.trim() && Number.isFinite(Number(text))) return Number(text)
  return text
}

export function trialInputs(rows: Pack[], overrides: Pack, sample: boolean): Pack {
  return Object.fromEntries(rows.flatMap(p => {
    const name = shown(p.name)
    const value = Object.hasOwn(overrides, name) ? overrides[name] ?? p.default : p.default
    if (value !== undefined) return [[name, value]]
    if (sample) return [[name, numeric(p.type) ? 1 : boolean(p.type) ? false : 'Sample']]
    return []
  }))
}

export function readPackJson(text: string): Pack {
  const value: unknown = JSON.parse(text)
  if (!isObject(value)) throw new Error(uiText('Expected a JSON object.'))
  return value
}
