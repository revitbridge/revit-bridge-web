import type { FetchCandidates, Mention, MentionCandidates, MentionItem, MentionKind } from '../../../types/hostv2'
import {
  applyKnownNativeEdit, hasLineBreak, indexSpans, matchingKinds, mentionLabel, mentionSpans, menuAt, normalizeKnownSelection,
  normalizeNewlines, normalizeInputSelection, replaceKnownRange, type MentionMenu, type MentionSpan, type TextSelection,
} from './model'

export interface MentionCallbacks {
  fetchCandidates: FetchCandidates
  onChange: (text: string, mentions: Mention[]) => void
  onSubmit: () => void
}
export interface MentionState {
  text: string
  mentions: Mention[]
  spans: MentionSpan[]
  selection: TextSelection
  menu: MentionMenu | null
  candidates: MentionCandidates | null
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: string
  active: number
  composing: boolean
  disabled: boolean
}

const at = (position: number): TextSelection => ({ start: position, end: position, direction: 'none' })
const sameMentions = (a: Mention[], b: Mention[]) => a.length === b.length &&
  a.every((mention, index) => mention.kind === b[index].kind && mention.id === b[index].id && mention.name === b[index].name)
const sameMenu = (a: MentionMenu | null, b: MentionMenu | null) => JSON.stringify(a) === JSON.stringify(b)

export function elementId(item: MentionItem): number | null {
  if (typeof item.id === 'string' && !/^\d+$/.test(item.id)) return null
  const id = Number(item.id)
  return Number.isSafeInteger(id) && id >= 0 ? id : null
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

  constructor(text: string, mentions: Mention[], callbacks: MentionCallbacks, disabled = false) {
    this.callbacks = callbacks
    text = normalizeNewlines(text)
    const spans = indexSpans(mentionSpans(text, mentions))
    this.state = {
      text, spans, mentions: spans.map(span => span.mention), selection: at(text.length), menu: null, candidates: null,
      status: 'idle', error: '', active: 0, composing: false, disabled,
    }
  }

  getSnapshot = () => this.state
  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }
  configure(callbacks: MentionCallbacks) {
    const changed = callbacks.fetchCandidates !== this.callbacks.fetchCandidates
    this.callbacks = callbacks
    if (changed && this.state.menu?.mode === 'items') this.schedule()
  }
  syncExternal(text: string, mentions: Mention[], disabled = false) {
    if (disabled !== this.state.disabled) {
      this.publish({ disabled })
      if (disabled) this.close()
    }
    text = normalizeNewlines(text)
    const spans = indexSpans(mentionSpans(text, mentions))
    const normalized = { text, spans, mentions: spans.map(span => span.mention) }
    // A controlled echo carries no offsets. Keep authoritative spans when text
    // and metadata agree instead of binding them to the first equal plain text.
    if (this.state.composing || (text === this.state.text && sameMentions(normalized.mentions, this.state.mentions))) return
    this.cancel()
    this.publish({ ...normalized, selection: at(Math.min(this.state.selection.end, text.length)),
      menu: null, candidates: null, status: 'idle', error: '' })
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
      this.publish({ menu: null, candidates: null, status: 'idle', error: '', active: 0 })
  }
  private refreshMenu(previous = this.state.menu) {
    const { text, spans, selection, disabled, composing } = this.state
    const menu = disabled || composing || selection.start !== selection.end
      ? null : menuAt(text, spans, selection.end, previous)
    if (sameMenu(menu, this.state.menu)) return
    this.cancel()
    this.publish({ menu, active: 0, candidates: null, status: 'idle', error: '' })
    if (menu?.mode === 'items') this.schedule()
  }
  private schedule() {
    this.cancel()
    const menu = this.state.menu
    if (menu?.mode !== 'items' || this.state.disabled || this.state.composing) return
    const generation = this.request
    this.publish({ status: 'loading', candidates: null, error: '', active: 0 })
    this.timer = setTimeout(async () => {
      try {
        const candidates = await this.callbacks.fetchCandidates(menu.kind, {
          ...(menu.category ? { category: menu.category } : {}), q: menu.query,
        })
        if (generation !== this.request) return
        this.publish({ status: 'ready', candidates, active: Math.max(0, candidates.items.findIndex(item => this.selectable(item))) })
      } catch (error) {
        if (generation !== this.request) return
        this.publish({ status: 'error', candidates: null,
          error: error instanceof Error ? error.message : String(error) })
      }
    }, 200)
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
    this.publish({ text, spans, mentions, selection: at(caret) })
    this.refreshMenu(previous)
    this.callbacks.onChange(text, mentions.map(mention => ({ ...mention })))
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
  replaceSelection(inserted: string) {
    if (this.state.disabled || this.state.composing) return false
    const { text, spans, selection } = this.state
    const edited = replaceKnownRange(text, spans, selection.start, selection.end, inserted)
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
    if (item.disabled || (item.kind !== 'category' && hasLineBreak(item.name))) return false
    const menu = this.state.menu
    if (menu?.mode !== 'items') return false
    return menu.kind === 'type' && !menu.category
      ? item.kind === 'category' && typeof item.id === 'string' && item.id.length > 0
      : item.kind === menu.kind && elementId(item) !== null
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
        { ...menu, category, categoryLabel: item.label || item.name })
      return
    }
    const mention: Mention = { kind: menu.kind, id: elementId(item)!, name: item.name }
    const label = mentionLabel(mention)
    const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, label)
    this.commit(edited.text, [...edited.spans, { start: edited.caret - label.length, end: edited.caret, index: -1, mention }], edited.caret)
    this.close()
  }
  private pickKind(kind: MentionKind) {
    const menu = this.state.menu
    if (!menu) return
    const prefix = `@${kind}:`
    const edited = replaceKnownRange(this.state.text, this.state.spans, menu.start, menu.end, prefix)
    this.commit(edited.text, edited.spans, edited.caret, null)
  }
  backToCategories() {
    if (this.state.menu?.mode !== 'items' || this.state.menu.kind !== 'type' || this.state.composing || this.state.disabled) return
    this.pickKind('type')
  }
  key(key: string, shift = false, modified = false, nativeComposing = false): boolean {
    const { disabled, composing, menu, text, spans, selection } = this.state
    if (disabled || composing || nativeComposing) return false
    if (key === 'Escape' && menu) { this.close(); return true }
    if (key === 'Enter' && shift) return false
    if (menu && (key === 'Enter' || key === 'Tab')) { this.choose(this.state.active); return true }
    if (menu && (key === 'ArrowDown' || key === 'ArrowUp')) {
      const indices = menu.mode === 'kinds' ? matchingKinds(menu.query).map((_, i) => i)
        : (this.state.candidates?.items ?? []).flatMap((item, i) => this.selectable(item) ? [i] : [])
      if (indices.length) {
        const current = indices.indexOf(this.state.active)
        const step = key === 'ArrowDown' ? 1 : -1
        const next = current < 0 ? (step > 0 ? 0 : indices.length - 1)
          : (current + step + indices.length) % indices.length
        this.publish({ active: indices[next] })
      }
      return true
    }
    if (key === 'Enter') {
      if (text.trim()) this.callbacks.onSubmit()
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
