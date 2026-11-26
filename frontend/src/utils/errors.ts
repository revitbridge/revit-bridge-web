function reasonText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value == null) return ''
  if (typeof value !== 'object') return String(value)
  if ('msg' in value && typeof value.msg === 'string') {
    const location = 'loc' in value && Array.isArray(value.loc) ? value.loc.join('.') : ''
    return location ? location + ': ' + value.msg : value.msg
  }
  if ('message' in value && typeof value.message === 'string') {
    const code = 'code' in value && typeof value.code === 'string' ? value.code : ''
    const param = 'param' in value && typeof value.param === 'string' && value.param ? ' (' + value.param + ')' : ''
    return code ? code + param + ': ' + value.message : value.message
  }
  return JSON.stringify(value)
}

/** Messages in display order, including a FastAPI validation list and package render errors. */
export function serverReasons(body: unknown): string[] {
  if (!body || typeof body !== 'object') return []
  const lines = ['message', 'problems', 'detail', 'errors', 'warnings', 'hint'].flatMap(key => {
    const value: unknown = Reflect.get(body, key)
    return (Array.isArray(value) ? value : [value]).map(reasonText).filter(Boolean)
  })
  return [...new Set(lines)]
}

export function getErrorMessage(error: unknown): string {
  if (error && typeof error === 'object') {
    const reasons = serverReasons('body' in error ? error.body : error)
    if (reasons.length) return reasons.join('\n')
  }
  return error instanceof Error ? error.message : String(error)
}

export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}
