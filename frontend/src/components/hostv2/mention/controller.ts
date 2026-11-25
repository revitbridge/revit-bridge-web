import { MAX_PICKS } from '../../../types/hostv2'
import type { FetchCandidates, Mention, MentionCandidates, MentionItem, MentionKind } from '../../../types/hostv2'
import {
  applyKnownNativeEdit, hasLineBreak, indexSpans, matchingKinds, mentionLabel, mentionSpans, mentionStartAt, menuAt,
  normalizeKnownSelection, normalizeNewlines, normalizeInputSelection, replaceKnownRange,
  type MentionMenu, type MentionSpan, type TextSelection,
} from './model'

export interface MentionCallbacks {
  fetchCandidates: FetchCandidates
  onChange: (text: string, mentions: Mention[]) => void
  onSubmit: (text: string, mentions: Mention[]) => void
}
export interface MentionState {
  text: string
  mentions: Mention[]
  spans: MentionSpan[]
  selection: TextSelection
  menu: MentionMenu | null
  candidates: MentionCandidates | null
  nextOffset: number | null
  loadingMore: boolean
  checked: Mention[]
  elementCategory: string | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: string
  active: number
  composing: boolean
  disabled: boolean
  // Mentions the parent passed that are not in its text (or whose names cannot
  // be a tag). They were reported back through onChange; kept for the notice
  // until the user edits or the parent's text changes.
  dropped: Mention[]
}

const at = (position: number): TextSelection => ({ start: position, end: position, direction: 'none' })
const sameMentions = (a: Mention[], b: Mention[]) => a.length === b.length &&
  a.every((mention, index) => mention.kind === b[index].kind && mention.id === b[index].id && mention.name === b[index].name)
const sameMenu = (a: MentionMenu | null, b: MentionMenu | null) => JSON.stringify(a) === JSON.stringify(b)
const copies = (mentions: Mention[]) => mentions.map(mention => ({ ...mention }))

// Binds external (offset-free) state. bound is in text order; dropped keeps the
// given order.
function bindExternal(text: string, mentions: Mention[]) {
  const spans = mentionSpans(text, mentions)
  const claimed = new Set(spans.map(span => span.index))
  const indexed = indexSpans(spans)
  return { spans: indexed, bound: indexed.map(span => span.mention), dropped: mentions.filter((_, index) => !claimed.has(index)) }
}

export function elementId(item: MentionItem): number | null {
  if (typeof item.id === 'string' && !/^\d+$/.test(item.id)) return null
  const id = Number(item.id)
  return Number.isSafeInteger(id) && id > 0 ? id : null
}

// The same controller powers the DOM adapter and the no-browser interaction
// tests. It never fetches by itself: all candidates use the supplied function.
export class MentionController {
  private state: MentionState
  private callbacks: MentionCallbacks
  private listeners = new Set<() => void>()
  private timer: ReturnType<typeof setTimeout> | undefined
  private request = 0
  private beforeComposition: { text: string; spans: MentionSpan[]; selection: TextSelection } | null = null
  // Start of the '@' whose picker was closed with Escape (or with Enter when it
  // had nothing to choose): it stays closed until the caret leaves that '@'.
  private dismissed: number | null = null
  // The last external input whose difference was reported, so a parent that
  // keeps passing it does not get onChange again on every render.
  private reported = ''

  constructor(text: string, mentions: Mention[], callbacks: MentionCallbacks, disabled = false) {
    this.callbacks = callbacks
    text = normalizeNewlines(text)
    const { spans, bound, dropped } = bindExternal(text, mentions)
    this.state = {
      text, spans, mentions: bound, selection: at(text.length), menu: null, candidates: null, nextOffset: null,
      loadingMore: false, checked: [], elementCategory: null, status: 'idle', error: '', active: 0, composing: false, disabled, dropped,
    }
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  // Callbacks may change identity on every parent render (inline arrows); the
  // latest ones are used from the next call on, nothing is refetched for it.
  configure(callbacks: MentionCallbacks) {
    this.callbacks = callbacks
  }
  syncExternal(value: string, mentions: Mention[], disabled = false) {
    if (disabled !== this.state.disabled) {
      this.publish({ disabled })
      if (disabled) this.close()
    }
    if (this.state.composing) return
    const text = normalizeNewlines(value)
    const { spans, bound, dropped } = bindExternal(text, mentions)
    // A controlled echo carries no offsets. Keep authoritative spans when text
    // and metadata agree instead of binding them to the first equal plain text.
    if (text !== this.state.text || !sameMentions(bound, this.state.mentions)) {
      this.cancel()
      this.dismissed = null
      this.publish({ text, spans, mentions: bound,
        selection: normalizeKnownSelection(text, spans, at(Math.min(this.state.selection.end, text.length))),
        menu: null, candidates: null, checked: [], elementCategory: null, loadingMore: false, nextOffset: null, status: 'idle', error: '',
        dropped: dropped.length || text !== this.state.text ? dropped : this.state.dropped })
    } else if (dropped.length && !sameMentions(dropped, this.state.dropped)) this.publish({ dropped })
    this.reportDifference(value, mentions)
  }
  // The parent's mentions disagree with its text (missing from it, or out of
  // text order), or its text was normalized: tell it once what the component
  // holds, so the parent and the component cannot drift apart silently.
  private reportDifference(value: string, mentions: Mention[]) {
    const { text, mentions: held } = this.state
    if (value === text && sameMentions(mentions, held)) {
      this.reported = ''
      return
    }
    const key = JSON.stringify([value, mentions.map(mention => [mention.kind, mention.id, mention.name])])
    if (key === this.reported) return
    this.reported = key
    this.callbacks.onChange(text, copies(held))
  }
  destroy() { this.cancel() }

  private publish(change: Partial<MentionState>) {
    this.state = { ...this.state, ...change }
    this.listeners.forEach(listener => listener())
  }
  private cancel() {
    this.request++
    clearTimeout(this.timer)
    this.timer = undefined
  }
  close() {
    this.cancel()
    if (this.state.menu || this.state.status !== 'idle')
      this.publish({ menu: null, candidates: null, checked: [], elementCategory: null, loadingMore: false, nextOffset: null, status: 'idle', error: '', active: 0 })
  }
  // Escape closes the picker for this '@' only; it does not come back while the
  // caret stays after it, so the next key and Enter behave as plain text.
  private dismiss() {
    this.dismissed = this.state.menu?.start ?? null
    this.close()
  }
  private refreshMenu(previous = this.state.menu) {
    const { text, spans, selection, disabled, composing } = this.state
    let menu: MentionMenu | null = null
    if (!disabled && !composing) {
      if (this.dismissed !== null && mentionStartAt(text, spans, selection.end) !== this.dismissed) this.dismissed = null
      if (selection.start === selection.end) menu = menuAt(text, spans, selection.end, previous)
      if (menu && menu.start === this.dismissed) menu = null
    }
    if (sameMenu(menu, this.state.menu)) return
    this.cancel()
    const keepChecks = menu?.mode === 'items' && menu.kind === 'element' &&
      previous?.mode === 'items' && previous.kind === 'element' && previous.start === menu.start
    this.publish({ menu, active: 0, candidates: null, status: 'idle', error: '', nextOffset: null, loadingMore: false,
      checked: keepChecks ? this.state.checked : [], elementCategory: keepChecks ? this.state.elementCategory : null })
    if (menu?.mode === 'items') this.schedule()
  }
  private schedule() {
    this.cancel()
    const menu = this.state.menu
    if (menu?.mode !== 'items' || this.state.disabled || this.state.composing) return
    const generation = this.request
    this.publish({ status: 'loading', candidates: null, error: '', active: 0 })
    this.timer = setTimeout(() => void this.fetchPage(menu, generation, 0), 200)
  }
  private async fetchPage(menu: Extract<MentionMenu, { mode: 'items' }>, generation: number, offset: number) {
    try {
      const page = await this.callbacks.fetchCandidates(menu.kind, {
        ...(menu.category ? { category: menu.category } : {}), q: menu.query,
        ...(menu.kind === 'element' ? { source: 'selection' as const } : {}), ...(offset ? { offset } : {}),
      })
      if (generation !== this.request) return
      const prior = offset ? this.state.candidates?.items ?? [] : []
      const known = new Set(prior.map(item => item.kind + '#' + item.id))
      const items = [...prior, ...page.items.filter(item => !known.has(item.kind + '#' + item.id))]
      // A partial selection page can omit unreadable rows. Its slot in the selection is still 50 IDs wide.
      const end = offset + (page.facets_partial ? 50 : page.items.length)
      const nextOffset = page.truncated && page.items.length > 0 && end > offset && end < page.total ? end : null
      const active = offset ? this.state.active : Math.max(0, items.findIndex(item =>
        (!this.state.elementCategory || item.category === this.state.elementCategory) && this.selectable(item)))
      this.publish({ status: 'ready', candidates: { ...page, items }, nextOffset, loadingMore: false, error: '', active })
    } catch (error) {
      if (generation !== this.request) return
      this.publish({ status: offset ? 'ready' : 'error', loadingMore: false,
        error: error instanceof Error ? error.message : String(error) })
    }
  }
  loadMore() {
    const { menu, nextOffset, loadingMore } = this.state
    if (menu?.mode !== 'items' || nextOffset === null || loadingMore) return
    this.publish({ loadingMore: true, error: '' })
    void this.fetchPage(menu, this.request, nextOffset)
  }
  select(selection: TextSelection) {
    if (this.state.disabled || this.state.composing) return
    const next = normalizeKnownSelection(this.state.text, this.state.spans, selection)
    if (JSON.stringify(next) !== JSON.stringify(this.state.selection)) {
      this.publish({ selection: next })
      this.refreshMenu()
    }
  }
  private commit(text: string, spans: MentionSpan[], caret: number, previous = this.state.menu) {
    spans = indexSpans(spans)
    const mentions = spans.map(span => span.mention)
    this.publish({ text, spans, mentions, selection: at(caret), dropped: [] })
    this.refreshMenu(previous)
    this.callbacks.onChange(text, copies(mentions))
  }
  editNative(next: string, selection: TextSelection) {
    if (this.state.disabled) return
    selection = normalizeInputSelection(next, selection)
    next = normalizeNewlines(next)
    if (this.state.composing) {
      // Let the native IME own the value and caret until compositionend. The
      // parent gets its controlled text updates, but no tags are rewritten yet.
      const before = this.beforeComposition
      const spans = before ? applyKnownNativeEdit(before.text, before.spans, next, before.selection).spans : this.state.spans
      this.publish({ text: next, spans, selection })
      this.callbacks.onChange(next, this.state.mentions)
      return
    }
    const { text, spans, selection: oldSelection } = this.state
    // A selected duplicate token cannot be identified from longest-prefix diff
    // alone; use the actual old selection whenever it explains the native edit.
    const edited = applyKnownNativeEdit(text, spans, next, oldSelection)
    this.commit(edited.text, edited.spans, edited.text === next ? selection.end : edited.caret)
  }
  // paste: an '@' that arrives inside pasted text does not open the picker (an
  // e-mail address or a copied "@level:..." is text); typing after an '@' that
  // was already there keeps working. Pasted labels are never promoted to ids.
  replaceSelection(inserted: string, options: { paste?: boolean } = {}) {
    if (this.state.disabled || this.state.composing) return false
    const { text, spans, selection } = this.state
    const edited = replaceKnownRange(text, spans, selection.start, selection.end, inserted)
    if (options.paste) {
      const from = edited.caret - normalizeNewlines(inserted).length
      const start = mentionStartAt(edited.text, edited.spans, edited.caret)
      if (start !== null && start >= from) this.dismissed = start
    }
    this.commit(edited.text, edited.spans, edited.caret)
    return true
  }
  compositionStart() {
    if (this.state.disabled) return
    this.beforeComposition = { text: this.state.text, spans: this.state.spans, selection: this.state.selection }
    this.close()
    this.publish({ composing: true })
  }
  compositionEnd(text: string, selection: TextSelection) {
    selection = normalizeInputSelection(text, selection)
    text = normalizeNewlines(text)
    const previous = this.beforeComposition
    this.beforeComposition = null
    this.publish({ composing: false })
    if (this.state.disabled) return
    if (previous) {
      const edited = applyKnownNativeEdit(previous.text, previous.spans, text, previous.selection)
      this.commit(edited.text, edited.spans, edited.text === text ? selection.end : edited.caret)
    } else this.editNative(text, selection)
  }
  selectable(item: MentionItem): boolean {
    if (item.disabled || (item.kind !== 'category' && hasLineBreak(item.name ?? ''))) return false
    const menu = this.state.menu
    if (menu?.mode !== 'items') return false
    if (menu.kind === 'type' && !menu.category && !menu.query.trim())
      return item.kind === 'category' && typeof item.id === 'string' && item.id.length > 0
    if (item.kind !== menu.kind || elementId(item) === null) return false
    if (menu.kind !== 'element') return true
    const id = elementId(item)!
    if (this.state.mentions.some(m => m.kind === 'element' && m.id === id)) return false
    return this.state.checked.some(m => m.id === id) || this.availableSlots() > this.state.checked.length
  }
  choose(index: number) {
    if (this.state.disabled || this.state.composing) return
    const menu = this.state.menu
    if (!menu) return
    if (menu.mode === 'kinds') {
      const kind = matchingKinds(menu.query)[index]
      if (kind) this.pickKind(kind)
      return
    }
    const item = this.state.candidates?.items[index]
    if (!item || !this.selectable(item)) return
    if (item.kind === 'category') {
      const category = String(item.id)
      const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, '@type:')
      this.commit(edited.text, edited.spans, edited.caret,
        { ...menu, category, categoryLabel: item.label || item.name || category })
      return
    }
    const mention: Mention = { kind: menu.kind, id: elementId(item)!, name: item.name ?? '',
      ...(menu.kind === 'element' && item.document_id ? { document_id: item.document_id } : {}) }
    if (menu.kind === 'element') {
      const checked = this.state.checked.some(m => m.id === mention.id)
        ? this.state.checked.filter(m => m.id !== mention.id) : [...this.state.checked, mention]
      this.publish({ checked, active: index })
      return
    }
    const label = mentionLabel(mention)
    const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, label)
    this.commit(edited.text, [...edited.spans, { start: edited.caret - label.length, end: edited.caret, index: -1, mention }], edited.caret)
    this.close()
  }
  availableSlots() {
    return MAX_PICKS - new Set(this.state.mentions.map(m => m.kind + '#' + m.id)).size
  }
  insertChecked() {
    const { menu, checked, disabled } = this.state
    if (disabled || menu?.mode !== 'items' || menu.kind !== 'element' || !checked.length || checked.length > this.availableSlots()) return
    const labels = checked.map(mentionLabel)
    const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, labels.join(' '))
    let start = menu.start
    const spans = checked.map((mention, i) => {
      const span = { start, end: start + labels[i].length, index: -1, mention }
      start = span.end + 1
      return span
    })
    this.commit(edited.text, [...edited.spans, ...spans], edited.caret)
    this.close()
  }
  filterCategory(category: string, label: string) {
    const menu = this.state.menu
    if (menu?.mode !== 'items') return
    if (menu.kind === 'element') {
      this.publish({ elementCategory: category || null })
      this.publish({ active: this.choices()[0] ?? 0 })
    } else if (menu.kind === 'type') {
      this.refreshMenu({ ...menu, category: category || undefined, categoryLabel: label || undefined })
    }
  }
  readSelection() {
    const menu = this.state.menu
    if (menu?.mode === 'items' && menu.kind === 'element' && !menu.query) {
      this.publish({ checked: [], elementCategory: null, nextOffset: null, loadingMore: false })
      this.schedule()
    } else this.pickKind('element')
  }
  pickKind(kind: MentionKind) {
    const menu = this.state.menu
    if (!menu) return
    const prefix = `@${kind}:`
    const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, prefix)
    this.commit(edited.text, edited.spans, edited.caret, null)
  }
  async clearMatched() {
    const menu = this.state.menu
    if (menu?.mode !== 'items' || menu.kind !== 'type' || !this.state.candidates?.category_matched?.length) return
    const generation = this.request
    // The listing names the inferred categories, not the words that matched. Ask the
    // same picker about ambiguous terms on removal, keeping alias rules in the package.
    const words = menu.query.match(/[\u3400-\u9fff]+|[^\u3400-\u9fff\s]+/gu) ?? []
    const terms = [...new Set(words.map(word => word.toLowerCase()).filter(word => !/^\d+$/.test(word)))]
    const remove = new Set<string>()
    try {
      for (const word of terms) {
        if (terms.length === 1) remove.add(word)
        else {
          const page = await this.callbacks.fetchCandidates('type', { q: word })
          if (generation !== this.request) return
          if (page.category_matched?.length) remove.add(word)
        }
      }
      const query = words.filter(word => !remove.has(word.toLowerCase())).join(' ')
      const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, '@type:' + query)
      this.commit(edited.text, edited.spans, edited.caret, null)
    } catch (error) {
      if (generation === this.request) this.publish({ error: error instanceof Error ? error.message : String(error) })
    }
  }
  backToCategories() {
    if (this.state.menu?.mode !== 'items' || this.state.menu.kind !== 'type' || this.state.composing || this.state.disabled) return
    this.pickKind('type')
  }
  // Indices Enter/Tab and the arrow keys can land on in the open picker.
  private choices(): number[] {
    const menu = this.state.menu
    if (!menu) return []
    return menu.mode === 'kinds' ? matchingKinds(menu.query).map((_, i) => i)
      : this.state.status !== 'ready' ? []
        : this.groups().flatMap(group => group.indices.filter(i => this.selectable(this.state.candidates!.items[i])))
  }
  groups() {
    const byCategory = new Map<string, number[]>()
    const candidates = this.state.candidates
    for (const [i, item] of (candidates?.items ?? []).entries()) {
      const key = item.kind === 'type' || item.kind === 'element' ? item.category ?? '' : ''
      if (this.state.elementCategory && this.state.elementCategory !== key) continue
      if (!byCategory.has(key)) byCategory.set(key, [])
      byCategory.get(key)!.push(i)
    }
    return Array.from(byCategory, ([category, indices]) => {
      const facet = candidates?.facets?.find(f => f.category === category)
      return { category, label: facet?.label ?? category, count: facet?.count ?? indices.length, indices }
    })
  }
  key(key: string, shift = false, modified = false, nativeComposing = false): boolean {
    const { disabled, composing, menu, text, spans, selection } = this.state
    if (disabled || composing || nativeComposing) return false
    if (key === 'Escape' && menu) { this.dismiss(); return true }
    if (key === 'Enter' && shift) return false
    if (key === 'Tab' && menu?.mode === 'items' && menu.kind === 'element') return false
    if (menu && (key === 'Enter' || key === 'Tab')) {
      if (this.choices().includes(this.state.active)) { this.choose(this.state.active); return true }
      if (menu.mode === 'items' && menu.kind === 'element' && this.state.checked.length) {
        this.insertChecked()
        return true
      }
      // Still loading: Enter or Tab here most likely means "pick it", so wait
      // instead of sending the half-typed label as plain text.
      if (menu.mode === 'items' && (this.state.status === 'loading' || this.state.status === 'idle')) return true
      // Nothing to pick (no match, every item disabled, or the list failed to
      // load): the picker must not swallow the key. Enter sends, Tab moves on.
      this.dismiss()
      if (key === 'Tab') return false
    }
    if (menu && (key === 'ArrowDown' || key === 'ArrowUp')) {
      const indices = this.choices()
      if (!indices.length) return false
      const current = indices.indexOf(this.state.active)
      const step = key === 'ArrowDown' ? 1 : -1
      const next = current < 0 ? (step > 0 ? 0 : indices.length - 1)
        : (current + step + indices.length) % indices.length
      this.publish({ active: indices[next] })
      return true
    }
    if (key === 'Enter') {
      if (text.trim()) this.callbacks.onSubmit(text, copies(this.state.mentions))
      return true
    }
    if (key === 'Backspace' || key === 'Delete') {
      const touched = selection.start === selection.end
        ? spans.find(span => key === 'Backspace' ? span.end === selection.start : span.start === selection.start)
        : spans.find(span => selection.start < span.end && selection.end > span.start)
      if (touched) {
        const start = Math.min(selection.start, touched.start)
        const end = Math.max(selection.end, touched.end)
        const edited = replaceKnownRange(text, spans, start, end, '')
        this.commit(edited.text, edited.spans, edited.caret)
        return true
      }
    }
    if (!shift && !modified && selection.start === selection.end && (key === 'ArrowLeft' || key === 'ArrowRight')) {
      const span = spans.find(item => key === 'ArrowLeft' ? item.end === selection.start : item.start === selection.end)
      if (span) { this.select(at(key === 'ArrowLeft' ? span.start : span.end)); return true }
    }
    return false
  }
}
