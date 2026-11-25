export function browserLanguage(): 'en' | 'zh' {
  const tags = typeof navigator === 'undefined' ? [] : navigator.languages?.length
    ? navigator.languages : [navigator.language]
  return tags.map(tag => tag?.toLowerCase().split('-')[0]).find(tag => tag === 'en' || tag === 'zh') ?? 'en'
}
