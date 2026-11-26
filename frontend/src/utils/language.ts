export type Language = 'en' | 'zh'
const storageKey = 'rb_language'
let memory: Language | undefined

export function browserLanguage(): 'en' | 'zh' {
  try {
    const saved = localStorage.getItem(storageKey)
    if (saved === 'en' || saved === 'zh') return saved
  } catch { /* Storage is optional. */ }
  if (memory) return memory
  const tags = typeof navigator === 'undefined' ? [] : navigator.languages?.length
    ? navigator.languages : [navigator.language]
  return tags.map(tag => tag?.toLowerCase().split('-')[0]).find(tag => tag === 'en' || tag === 'zh') ?? 'en'
}

export function setLanguage(value: Language) {
  memory = value
  try { localStorage.setItem(storageKey, value) } catch { /* This tab keeps the choice. */ }
  window.dispatchEvent(new Event('rb-language'))
}

export function subscribeLanguage(fn: () => void) {
  window.addEventListener('rb-language', fn)
  window.addEventListener('storage', fn)
  return () => { window.removeEventListener('rb-language', fn); window.removeEventListener('storage', fn) }
}
