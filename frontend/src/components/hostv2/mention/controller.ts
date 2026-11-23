import type { FetchCandidates, Mention, MentionCandidates, MentionItem, MentionKind } from '../../../types/hostv2'
import {
  applyNativeEdit, matchingKinds, mentionLabel, mentionSpans, menuAt, normalizeSelection,
  replaceRange, serialize, type MentionMenu, type TextSelection,
} from './model'

export interface MentionCallbacks {
  fetchCandidates: FetchCandidates
  onChange: (text: string, mentions: Mention[]) => void
  onSubmit: () => void
}
export interface MentionState {
  text: string
  mentions: Mention[]
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
  private beforeComposition: { text: string; mentions: Mention[]; selection: TextSelection } | null = null

  constructor(text: string, mentions: Mention[], callbacks: MentionCallbacks, disabled = false) {
    this.callbacks = callbacks
    this.state = {
      ...serialize(text, mentions), selection: at(text.length), menu: null, candidates: null,
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
    const normalized = serialize(text, mentions)
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
    const { text, mentions, selection, disabled, composing } = this.state
    const menu = disabled || composing || selection.start !== selection.end
      ? null : menuAt(text, mentions, selection.end, previous)
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
    const next = normalizeSelection(this.state.text, this.state.mentions, selection)
    if (JSON.stringify(next) !== JSON.stringify(this.state.selection)) {
      this.publish({ selection: next })
      this.refreshMenu()
    }
  }
  private commit(text: string, mentions: Mention[], caret: number, previous = this.state.menu) {
    this.publish({ text, mentions, selection: at(caret) })
    this.refreshMenu(previous)
    this.callbacks.onChange(text, mentions.map(mention => ({ ...mention })))
  }
  editNative(next: string, selection: TextSelection) {
    if (this.state.disabled) return
    if (this.state.composing) {
      // Let the native IME own the value and caret until compositionend. The
      // parent gets its controlled text updates, but no tags are rewritten yet.
      this.publish({ text: next, selection })
      this.callbacks.onChange(next, this.state.mentions)
      return
    }
    const { text, mentions, selection: oldSelection } = this.state
    // A selected duplicate token cannot be identified from longest-prefix diff
    // alone; use the actual old selection whenever it explains the native edit.
    const edited = applyNativeEdit(text, mentions, next, oldSelection)
    this.commit(edited.text, edited.mentions, edited.text === next ? selection.end : edited.caret)
  }
  replaceSelection(inserted: string) {
    if (this.state.disabled || this.state.composing) return false
    const { text, mentions, selection } = this.state
    const edited = replaceRange(text, mentions, selection.start, selection.end, inserted)
    this.commit(edited.text, edited.mentions, edited.caret)
    return true
  }
  compositionStart() {
    if (this.state.disabled) return
    this.beforeComposition = { text: this.state.text, mentions: this.state.mentions, selection: this.state.selection }
    this.close()
    this.publish({ composing: true })
  }
  compositionEnd(text: string, selection: TextSelection) {
    const previous = this.beforeComposition
    this.beforeComposition = null
    this.publish({ composing: false })
    if (this.state.disabled) return
    if (previous) {
      const edited = applyNativeEdit(previous.text, previous.mentions, text, previous.selection)
      this.commit(edited.text, edited.mentions, edited.text === text ? selection.end : edited.caret)
    } else this.editNative(text, selection)
  }
  selectable(item: MentionItem): boolean {
    if (item.disabled) return false
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
      this.commit(this.state.text.slice(0, menu.start) + '@type:' + this.state.text.slice(menu.end),
        this.state.mentions, menu.start + 6,
        { ...menu, category, categoryLabel: item.label || item.name })
      return
    }
    const mention: Mention = { kind: menu.kind, id: elementId(item)!, name: item.name }
    const label = mentionLabel(mention)
    const spans = mentionSpans(this.state.text, this.state.mentions)
    const before = spans.filter(span => span.end <= menu.start).map(span => span.mention)
    const after = spans.filter(span => span.start >= menu.end).map(span => span.mention)
    this.commit(this.state.text.slice(0, menu.start) + label + this.state.text.slice(menu.end),
      [...before, mention, ...after], menu.start + label.length)
    this.close()
  }
  private pickKind(kind: MentionKind) {
    const menu = this.state.menu
    if (!menu) return
    const prefix = `@${kind}:`
    this.commit(this.state.text.slice(0, menu.start) + prefix + this.state.text.slice(menu.end),
      this.state.mentions, menu.start + prefix.length, null)
  }
  backToCategories() {
    if (this.state.menu?.mode !== 'items' || this.state.menu.kind !== 'type' || this.state.composing || this.state.disabled) return
    this.pickKind('type')
  }
  key(key: string, shift = false, modified = false, nativeComposing = false): boolean {
    const { disabled, composing, menu, text, mentions, selection } = this.state
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
    const spans = mentionSpans(text, mentions)
    if (key === 'Backspace' || key === 'Delete') {
      const touched = selection.start === selection.end
        ? spans.find(span => key === 'Backspace' ? span.end === selection.start : span.start === selection.start)
        : spans.find(span => selection.start < span.end && selection.end > span.start)
      if (touched) {
        const start = Math.min(selection.start, touched.start)
        const end = Math.max(selection.end, touched.end)
        const edited = replaceRange(text, mentions, start, end, '')
        this.commit(edited.text, edited.mentions, edited.caret)
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
