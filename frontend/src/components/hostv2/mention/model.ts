import type { Mention, MentionKind } from '../../../types/hostv2'

export interface MentionText { text: string; mentions: Mention[] }
export interface MentionSpan { start: number; end: number; index: number; mention: Mention }
export interface TextSelection { start: number; end: number; direction: 'forward' | 'backward' | 'none' }
export const mentionLabel = (mention: Mention) => `@${mention.kind}:${mention.name}${mention.kind === 'element' ? '#' + mention.id : ''}`
export const normalizeNewlines = (text: string) => text.replace(/\r\n?/g, '\n')
export const hasLineBreak = (name: string) => /[\r\n]/.test(name)
export function normalizeInputSelection(text: string, selection: TextSelection): TextSelection {
  return { ...selection, start: normalizeNewlines(text.slice(0, selection.start)).length,
    end: normalizeNewlines(text.slice(0, selection.end)).length }
}
export function indexSpans(spans: MentionSpan[]): MentionSpan[] {
  return [...spans].sort((a, b) => a.start - b.start)
    .map((span, index) => ({ ...span, index, mention: { ...span.mention } }))
}
export interface KnownEdit extends MentionText { spans: MentionSpan[]; caret: number }

// Only for new external state and the stateless public helpers. That contract
// has no offsets, so labels are bound by text, independently of the array order:
// longer labels claim first (so `@level:A` cannot take the start of `@level:A B`),
// equal lengths keep their array order (the only hint between identical labels),
// and each mention takes the first occurrence no other mention has claimed and
// that overlaps no claimed one. Spans come back in text order; span.index is the
// mention's position in the given array. Internal edits must use known spans
// instead; they must never rediscover identity by text.
export function mentionSpans(text: string, mentions: Mention[]): MentionSpan[] {
  const claims = mentions.map((mention, index) => ({ mention, index, label: mentionLabel(mention) }))
    .filter(claim => !hasLineBreak(claim.mention.name))
    .sort((a, b) => b.label.length - a.label.length)
  const spans: MentionSpan[] = []
  for (const { mention, index, label } of claims) {
    const free = (start: number) => !spans.some(span => start < span.end && start + label.length > span.start)
    let start = text.indexOf(label)
    while (start >= 0 && (!free(start) || (mention.kind === 'element' && /[0-9]/.test(text.charAt(start + label.length))))) start = text.indexOf(label, start + 1)
    if (start >= 0) spans.push({ start, end: start + label.length, index, mention })
  }
  return spans.sort((a, b) => a.start - b.start)
}

// Serialize editor state, not an HTTP request. Hosts can map mentions to
// {kind, id} when building a ChatRequest. Plain text is never promoted to an id.
// Mentions come back in text order; those not found in the text are left out.
export function serialize(text: string, mentions: Mention[]): MentionText {
  text = normalizeNewlines(text)
  return { text, mentions: mentionSpans(text, mentions).map(span => ({ ...span.mention })) }
}

// index is the zero-based position in the given mentions array, not an element
// id or a character offset. The remaining mentions come back in text order.
export function removeMentionAt(text: string, mentions: Mention[], index: number): MentionText {
  text = normalizeNewlines(text)
  const spans = mentionSpans(text, mentions)
  const span = spans.find(item => item.index === index)
  if (!span) return serialize(text, mentions)
  return {
    text: text.slice(0, span.start) + text.slice(span.end),
    mentions: spans.filter(item => item.index !== index).map(item => ({ ...item.mention })),
  }
}

export function normalizeSelection(text: string, mentions: Mention[], selection: TextSelection): TextSelection {
  return normalizeKnownSelection(text, mentionSpans(text, mentions), selection)
}

export function normalizeKnownSelection(text: string, spans: MentionSpan[], selection: TextSelection): TextSelection {
  let start = Math.max(0, Math.min(selection.start, text.length))
  let end = Math.max(start, Math.min(selection.end, text.length))
  const collapsed = start === end
  for (const span of spans) {
    if (collapsed && start > span.start && start < span.end) {
      const edge = start - span.start < span.end - start ? span.start : span.end
      return { start: edge, end: edge, direction: 'none' }
    }
    if (!collapsed) {
      if (start > span.start && start < span.end) start = span.start
      if (end > span.start && end < span.end) end = span.end
    }
  }
  return { start, end, direction: selection.direction }
}

// Also used for paste, cut, drag/drop and mobile input: if a native edit touched
// any part of a tag, expand its replacement to the entire tag and drop only that
// occurrence's metadata. Untouched duplicate names retain their ids.
export function replaceRange(
  text: string, mentions: Mention[], start: number, end: number, inserted: string,
): MentionText & { caret: number } {
  const { text: result, mentions: remaining, caret } = replaceKnownRange(text, mentionSpans(text, mentions), start, end, inserted)
  return { text: result, mentions: remaining, caret }
}

export function replaceKnownRange(
  text: string, spans: MentionSpan[], start: number, end: number, inserted: string,
): KnownEdit {
  inserted = normalizeNewlines(inserted)
  const touched = spans.filter(span => (start < span.end && end > span.start) ||
    (start === end && start > span.start && start < span.end))
  for (const span of touched) {
    start = Math.min(start, span.start)
    end = Math.max(end, span.end)
  }
  const removed = new Set(touched.map(span => span.index))
  const delta = inserted.length - (end - start)
  const remaining = indexSpans(spans.filter(span => !removed.has(span.index)).map(span =>
    span.start >= end ? { ...span, start: span.start + delta, end: span.end + delta } : span))
  return {
    text: text.slice(0, start) + inserted + text.slice(end),
    spans: remaining,
    mentions: remaining.map(span => ({ ...span.mention })),
    caret: start + inserted.length,
  }
}

export function applyNativeEdit(previous: string, mentions: Mention[], next: string, selection?: TextSelection) {
  const { text, mentions: remaining, caret } = applyKnownNativeEdit(previous, mentionSpans(previous, mentions), next, selection)
  return { text, mentions: remaining, caret }
}

export function applyKnownNativeEdit(previous: string, spans: MentionSpan[], next: string, selection?: TextSelection): KnownEdit {
  if (selection) {
    const insertedLength = next.length - previous.length + selection.end - selection.start
    const inserted = next.slice(selection.start, selection.start + Math.max(0, insertedLength))
    if (insertedLength >= 0 && next === previous.slice(0, selection.start) + inserted + previous.slice(selection.end))
      return replaceKnownRange(previous, spans, selection.start, selection.end, inserted)
  }
  let start = 0
  while (start < previous.length && start < next.length && previous[start] === next[start]) start++
  let oldEnd = previous.length
  let newEnd = next.length
  while (oldEnd > start && newEnd > start && previous[oldEnd - 1] === next[newEnd - 1]) {
    oldEnd--
    newEnd--
  }
  return replaceKnownRange(previous, spans, start, oldEnd, next.slice(start, newEnd))
}

export type MentionMenu =
  | { mode: 'kinds'; start: number; end: number; query: string }
  | { mode: 'items'; start: number; end: number; query: string; kind: MentionKind; category?: string; categoryLabel?: string }

export const mentionKinds: MentionKind[] = ['link', 'type', 'level', 'element']
export function matchingKinds(prefix: string): MentionKind[] {
  return mentionKinds.filter(kind => kind.startsWith(prefix.toLowerCase()))
}

// The '@' the caret is typing after: the last one before the caret on the same
// line that is not part of a tag. Used to remember a dismissed picker until the
// caret leaves that '@'.
export function mentionStartAt(text: string, spans: MentionSpan[], caret: number): number | null {
  if (caret <= 0) return null
  const start = text.lastIndexOf('@', caret - 1)
  if (start < 0 || text.slice(start, caret).includes('\n')) return null
  return spans.some(span => start >= span.start && start < span.end) ? null : start
}

// An '@' right after an ASCII letter or digit is inside a word ("bob@level", an e-mail
// address): it opens nothing. After a space, punctuation, the start of the text or a
// line, or any other character - a Chinese character, Chinese being written without
// spaces - it does.
const IN_A_WORD = /[A-Za-z0-9]/

export function menuAt(text: string, spans: MentionSpan[], caret: number, previous: MentionMenu | null): MentionMenu | null {
  const start = mentionStartAt(text, spans, caret)
  if (start === null) return null
  if (start > 0 && IN_A_WORD.test(text[start - 1])) return null
  const query = text.slice(start, caret)
  const items = /^@(link|type|level|element):([^\r\n@]*)$/i.exec(query)
  if (items) {
    const kind = items[1].toLowerCase() as MentionKind
    const keep = previous?.mode === 'items' && previous.start === start && previous.kind === kind ? previous : undefined
    return { mode: 'items', start, end: caret, query: items[2], kind, category: keep?.category, categoryLabel: keep?.categoryLabel }
  }
  // Letters that start no kind (an e-mail address, "@corp") open nothing, so
  // Enter still sends.
  const kinds = /^@([a-z]*)$/i.exec(query)
  return kinds && matchingKinds(kinds[1]).length > 0 ? { mode: 'kinds', start, end: caret, query: kinds[1] } : null
}
