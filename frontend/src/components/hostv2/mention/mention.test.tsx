import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { FetchCandidates, Mention, MentionCandidates, MentionItem } from '../../../types/hostv2'
import { MentionController } from './controller'
import { handleKeyDown, handlePaste, type KeyDownLike, type TextareaLike } from './dom'
import MentionInput, { MentionSuggestions } from './MentionInput'
import { applyNativeEdit, matchingKinds, mentionLabel, mentionSpans, menuAt, normalizeSelection, removeMentionAt, replaceRange, serialize } from './model'

const level: Mention = { kind: 'level', id: 10, name: 'Level 1' }
const sameName: Mention = { ...level, id: 11 }
const at = (end: number) => ({ start: end, end, direction: 'none' as const })
const selected = (start: number, end: number) => ({ start, end, direction: 'forward' as const })
const item = (kind = 'level', id: number | string = 10, name = 'Level 1', extra: Partial<MentionItem> = {}): MentionItem =>
  ({ kind, id, name, label: name, detail: '', ...extra })
const result = (kind: string, items: MentionItem[], truncated = false): MentionCandidates =>
  ({ kind, items, truncated, total: items.length + (truncated ? 100 : 0) })

function setup(fetchCandidates: FetchCandidates = vi.fn(async () => result('level', [])), text = '', mentions: Mention[] = []) {
  const onChange = vi.fn()
  const onSubmit = vi.fn()
  const controller = new MentionController(text, mentions, { onChange, onSubmit, fetchCandidates })
  return { controller, onChange, onSubmit, fetchCandidates }
}
const input = (controller: MentionController, text: string) => controller.editNative(text, at(text.length))
const popup = (controller: MentionController) => renderToStaticMarkup(
  <MentionSuggestions state={controller.getSnapshot()} controller={controller} id="test" />)

describe('mention text and atomic selection', () => {
  it('serializes labels with spaces and unicode, preserves ids and removes absent metadata', () => {
    const chinese: Mention = { kind: 'link', id: 20, name: '结构模型 A' }
    const text = `Use ${mentionLabel(level)} and ${mentionLabel(chinese)}.`
    expect(serialize(text, [level, chinese, { ...level, name: 'missing' }])).toEqual({ text, mentions: [level, chinese] })
    expect(serialize('@level:typed by hand', [])).toEqual({ text: '@level:typed by hand', mentions: [] })
  })
  it('removes exactly one occurrence of identical labels with different ids', () => {
    const label = mentionLabel(level)
    const text = `${label} ${label} ${label}`
    expect(removeMentionAt(text, [level, sameName, level], 1)).toEqual({ text: `${label}  ${label}`, mentions: [level, level] })
    expect(removeMentionAt(text, [level, sameName, level], 0).mentions).toEqual([sameName, level])
    expect(removeMentionAt(text, [level, sameName, level], 2).mentions).toEqual([level, sameName])
  })
  it('does not remove by id, tolerates an invalid index, and never mutates inputs', () => {
    const metadata = Object.freeze([Object.freeze({ ...level }), Object.freeze({ ...level })])
    const text = `${mentionLabel(level)} ${mentionLabel(level)}`
    expect(removeMentionAt(text, [...metadata], 1).mentions).toHaveLength(1)
    expect(removeMentionAt(text, [...metadata], 50)).toEqual({ text, mentions: metadata })
  })
  it('snaps a collapsed cursor to a tag edge and expands any partial selection', () => {
    const text = `a${mentionLabel(level)}b`
    const end = text.length - 1
    expect(normalizeSelection(text, [level], at(3))).toEqual(at(1))
    expect(normalizeSelection(text, [level], at(end - 1))).toEqual(at(end))
    expect(normalizeSelection(text, [level], selected(3, 5))).toEqual(selected(1, end))
    expect(normalizeSelection(text, [level], selected(0, 4))).toEqual(selected(0, end))
  })
  it('cut or paste over part of a token replaces the whole token and keeps the other occurrence', () => {
    const label = mentionLabel(level)
    const text = `${label} ${label}`
    expect(replaceRange(text, [level, sameName], 2, 5, 'new')).toEqual({ text: `new ${label}`, mentions: [sameName], caret: 3 })
    expect(replaceRange(text, [level, sameName], label.length + 3, label.length + 3, 'x').mentions).toEqual([level])
  })
  it('uses the selected occurrence to disambiguate deleting adjacent identical labels', () => {
    const label = mentionLabel(level)
    const text = label + label
    expect(applyNativeEdit(text, [level, sameName], label, selected(0, label.length)).mentions).toEqual([sameName])
    expect(applyNativeEdit(text, [level, sameName], label, selected(label.length, text.length)).mentions).toEqual([level])
  })
  it('repairs a mobile partial deletion and preserves tags on ordinary surrounding edits', () => {
    const label = mentionLabel(level)
    expect(applyNativeEdit(label, [level], label.slice(0, -1))).toEqual({ text: '', mentions: [], caret: 0 })
    expect(applyNativeEdit(`before ${label}`, [level], `new before ${label}`).mentions).toEqual([level])
  })
})

describe('candidate and keyboard flow', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('opens three kinds, filters by prefix, and uses Tab to enter a kind', async () => {
    const fetch = vi.fn(async () => result('level', [item()]))
    const { controller, onSubmit } = setup(fetch)
    input(controller, '@')
    expect(matchingKinds('')).toEqual(['link', 'type', 'level'])
    expect(popup(controller)).toContain('@type')
    input(controller, '@le')
    expect(matchingKinds(controller.getSnapshot().menu!.query)).toEqual(['level'])
    expect(controller.key('Tab')).toBe(true)
    expect(controller.getSnapshot().text).toBe('@level:')
    await vi.advanceTimersByTimeAsync(199)
    expect(fetch).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(fetch).toHaveBeenCalledWith('level', { q: '' })
    expect(controller.key('Enter')).toBe(true)
    expect(controller.getSnapshot().text).toBe('@level:Level 1')
    expect(controller.getSnapshot().mentions).toEqual([level])
    expect(onSubmit).not.toHaveBeenCalled()
  })
  it('selects type category then type and includes category on subsequent searches', async () => {
    const fetch = vi.fn<FetchCandidates>(async (_kind, opts) => opts.category
      ? result('type', [item('type', '42', 'Concrete 200', { category: opts.category, family: 'Basic Wall' })])
      : result('type', [item('category', 'OST_Walls', 'Walls')]))
    const { controller } = setup(fetch)
    input(controller, '@ty')
    controller.key('Enter')
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('type', { q: '' })
    controller.key('Enter')
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('type', { category: 'OST_Walls', q: '' })
    input(controller, '@type:Concrete')
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('type', { category: 'OST_Walls', q: 'Concrete' })
    expect(popup(controller)).toContain('Categories / Walls')
    controller.key('Tab')
    expect(controller.getSnapshot().mentions).toEqual([{ kind: 'type', id: 42, name: 'Concrete 200' }])
    expect(controller.getSnapshot().text).toBe('@type:Concrete 200')
  })
  it('can go back from a type category without retaining the old category', async () => {
    const fetch = vi.fn(async () => result('type', [item('category', 'OST_Walls', 'Walls')]))
    const { controller } = setup(fetch)
    input(controller, '@type:')
    await vi.advanceTimersByTimeAsync(200)
    controller.choose(0)
    controller.backToCategories()
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('type', { q: '' })
  })
  it('debounces rapid searches and discards a late success from the previous query', async () => {
    let finishOld!: (value: MentionCandidates) => void
    const fetch = vi.fn<FetchCandidates>((_kind, opts) => opts.q === 'old'
      ? new Promise(resolve => { finishOld = resolve }) : Promise.resolve(result('level', [item('level', 2, 'new')])) )
    const { controller } = setup(fetch)
    input(controller, '@level:old')
    await vi.advanceTimersByTimeAsync(200)
    input(controller, '@level:n')
    await vi.advanceTimersByTimeAsync(100)
    input(controller, '@level:new')
    await vi.advanceTimersByTimeAsync(199)
    expect(fetch).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    finishOld(result('level', [item('level', 1, 'old')]))
    await Promise.resolve()
    expect(controller.getSnapshot().candidates?.items[0].name).toBe('new')
    expect(fetch).toHaveBeenCalledTimes(2)
  })
  it('discards old errors and responses after Escape, disabling and unmount', async () => {
    let rejectOld!: (error: Error) => void
    const fetch = vi.fn<FetchCandidates>(() => new Promise((_resolve, reject) => { rejectOld = reject }))
    const { controller } = setup(fetch)
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    controller.key('Escape')
    rejectOld(new Error('stale failure'))
    await Promise.resolve()
    expect(controller.getSnapshot().menu).toBeNull()
    expect(controller.getSnapshot().error).toBe('')
    input(controller, '')
    input(controller, '@link:')
    expect(controller.getSnapshot().menu?.mode).toBe('items')
    controller.syncExternal('@link:', [], true)
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenCalledTimes(1)
    controller.syncExternal('@link:', [], false)
    input(controller, '@link:x')
    controller.destroy()
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('waits while the list loads, shows a load error inline, and the error blocks neither typing nor Enter', async () => {
    const { controller, onSubmit } = setup(async () => { throw new Error('Model offline') })
    input(controller, '@level:')
    expect(controller.key('Enter')).toBe(true)
    expect(controller.key('Tab')).toBe(true)
    expect(onSubmit).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(200)
    expect(popup(controller)).toContain('role="alert"')
    expect(popup(controller)).toContain('Model offline')
    input(controller, '@level:still editable')
    expect(controller.getSnapshot().text).toBe('@level:still editable')
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.getSnapshot().status).toBe('error')
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('@level:still editable', [])
    expect(controller.getSnapshot().menu).toBeNull()
    input(controller, '@level:still editable!')
    expect(controller.getSnapshot().menu).toBeNull()
  })
  it('keeps disabled links visible with reasons and skips them during keyboard navigation', async () => {
    const { controller, onSubmit } = setup(async () => result('link', [
      item('link', 1, 'Unloaded', { disabled: true, detail: 'The link is unloaded.' }),
      item('link', 2, 'Structure'), item('link', 3, 'Architecture'),
    ], true))
    input(controller, '@link:')
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.getSnapshot().active).toBe(1)
    expect(popup(controller)).toContain('The link is unloaded.')
    expect(popup(controller)).toContain('aria-disabled="true" disabled=""')
    expect(popup(controller)).toContain('More items are available.')
    controller.choose(0)
    expect(controller.getSnapshot().mentions).toEqual([])
    controller.key('ArrowDown')
    expect(controller.getSnapshot().active).toBe(2)
    controller.key('ArrowDown')
    expect(controller.getSnapshot().active).toBe(1)
    controller.key('ArrowUp')
    expect(controller.getSnapshot().active).toBe(2)
    controller.key('Enter')
    expect(controller.getSnapshot().mentions).toEqual([{ kind: 'link', id: 3, name: 'Architecture' }])
    expect(onSubmit).not.toHaveBeenCalled()
  })
  it('rejects malformed element ids and non-matching candidate kinds', async () => {
    const { controller } = setup(async () => result('level', [item('level', 'bad'), item('type', 12)]))
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    controller.choose(0)
    controller.choose(1)
    expect(controller.getSnapshot().mentions).toEqual([])
  })
  it('Backspace and Delete remove an entire tag and only its metadata occurrence', () => {
    const label = mentionLabel(level)
    const { controller, onChange } = setup(undefined, label + label, [level, sameName])
    controller.select(at(label.length))
    expect(controller.key('Backspace')).toBe(true)
    expect(onChange).toHaveBeenLastCalledWith(label, [sameName])
    expect(controller.getSnapshot().selection).toEqual(at(0))
    expect(controller.key('Delete')).toBe(true)
    expect(onChange).toHaveBeenLastCalledWith('', [])
  })
  it('arrow keys jump tags, mouse selections snap, and shift arrows can select a whole tag', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label + ' end', [level])
    controller.select(at(0))
    expect(controller.key('ArrowRight')).toBe(true)
    expect(controller.getSnapshot().selection).toEqual(at(label.length))
    expect(controller.key('ArrowLeft')).toBe(true)
    expect(controller.getSnapshot().selection).toEqual(at(0))
    controller.select(at(2))
    expect(controller.getSnapshot().selection).toEqual(at(0))
    expect(controller.key('ArrowRight', true)).toBe(false)
    controller.select(selected(0, 1))
    expect(controller.getSnapshot().selection).toEqual(selected(0, label.length))
  })
  it('inserts mentions in document order when editing before an existing tag', async () => {
    const label = mentionLabel(level)
    const { controller } = setup(async () => result('level', [item('level', 11)]), ` ${label}`, [level])
    controller.select(at(0))
    controller.replaceSelection('@level:')
    await vi.advanceTimersByTimeAsync(200)
    controller.choose(0)
    expect(controller.getSnapshot().mentions).toEqual([sameName, level])
    expect(mentionSpans(controller.getSnapshot().text, controller.getSnapshot().mentions).map(span => span.mention.id)).toEqual([11, 10])
  })
  it('Shift+Enter allows a native newline and a closed popup Enter submits', () => {
    const { controller, onSubmit } = setup()
    controller.key('Enter')
    expect(onSubmit).not.toHaveBeenCalled()
    input(controller, 'hello')
    expect(controller.key('Enter', true)).toBe(false)
    input(controller, 'hello\n')
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledTimes(1)
  })
  it('does not select, rewrite or submit during Chinese composition', async () => {
    const fetch = vi.fn(async () => result('level', [item()]))
    const { controller, onSubmit, onChange } = setup(fetch)
    input(controller, '@level:')
    controller.compositionStart()
    input(controller, '@level:楼')
    expect(controller.key('Enter')).toBe(false)
    expect(controller.key('Tab')).toBe(false)
    controller.choose(0)
    expect(controller.getSnapshot().mentions).toEqual([])
    await vi.advanceTimersByTimeAsync(500)
    expect(fetch).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(onChange).toHaveBeenLastCalledWith('@level:楼', [])
    controller.compositionEnd('@level:楼层', at('@level:楼层'.length))
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('level', { q: '楼层' })
    expect(controller.key('Enter', false, false, true)).toBe(false)
    expect(onSubmit).not.toHaveBeenCalled()
  })
  it('disambiguates identical labels replaced by an IME composition', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label + label, [level, sameName])
    controller.select(selected(0, label.length))
    controller.compositionStart()
    input(controller, '中' + label)
    controller.compositionEnd('中文' + label, at(2))
    expect(controller.getSnapshot().mentions).toEqual([sameName])
  })
  it('disabled input ignores keyboard, paste and candidate selection', () => {
    const { controller, onChange, onSubmit } = setup()
    controller.syncExternal('', [], true)
    input(controller, '@')
    controller.key('Enter')
    controller.replaceSelection('paste')
    controller.choose(0)
    expect(controller.getSnapshot().text).toBe('')
    expect(onChange).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('keeps an existing tag identity after equal plain text is pasted before it and echoed', () => {
    const label = mentionLabel(level)
    const { controller, onChange } = setup(undefined, label, [level])
    controller.select(at(0))
    controller.replaceSelection(label + ' ')
    const echoed = controller.getSnapshot()
    expect(echoed.spans).toEqual([{ start: label.length + 1, end: label.length * 2 + 1, index: 0, mention: level }])
    controller.syncExternal(echoed.text, echoed.mentions.map(mention => ({ ...mention })))
    expect(controller.getSnapshot().spans[0].start).toBe(label.length + 1)
    controller.select(at(3))
    expect(controller.getSnapshot().selection).toEqual(at(3))
    controller.select(at(echoed.text.length))
    expect(controller.key('Backspace')).toBe(true)
    expect(onChange).toHaveBeenLastCalledWith(label + ' ', [])
    expect(controller.getSnapshot().spans).toEqual([])
  })

  it('never promotes equal plain text typed in front of an existing tag', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label, [level])
    controller.select(at(0))
    for (const character of label + ' ') {
      const before = controller.getSnapshot()
      const caret = before.selection.end
      controller.editNative(before.text.slice(0, caret) + character + before.text.slice(caret), at(caret + 1))
      const echo = controller.getSnapshot()
      controller.syncExternal(echo.text, echo.mentions)
    }
    expect(controller.getSnapshot().spans[0].start).toBe(label.length + 1)
    controller.select(at(label.length))
    expect(controller.key('Backspace')).toBe(false)
    controller.editNative(label.slice(0, -1) + ' ' + label, at(label.length - 1))
    expect(controller.getSnapshot().mentions).toEqual([level])
    expect(controller.getSnapshot().spans[0].start).toBe(label.length)
  })

  it('removes only the targeted duplicate id after inserting an unbound equal label', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label + ' ' + label, [level, level])
    controller.select(at(0))
    controller.replaceSelection(label + ' ')
    expect(controller.getSnapshot().spans.map(span => span.start)).toEqual([label.length + 1, 2 * (label.length + 1)])
    controller.select(at(controller.getSnapshot().text.length))
    controller.key('Backspace')
    expect(controller.getSnapshot().mentions).toEqual([level])
    expect(controller.getSnapshot().spans).toEqual([{ start: label.length + 1, end: 2 * label.length + 1, index: 0, mention: level }])
  })

  it('creates a span only for an explicit candidate selection, even beside equal plain text', async () => {
    const label = mentionLabel(level)
    const { controller } = setup(async () => result('level', [item()]), label + ' ', [])
    controller.replaceSelection('@level:')
    await vi.advanceTimersByTimeAsync(200)
    controller.choose(0)
    expect(controller.getSnapshot().mentions).toEqual([level])
    expect(controller.getSnapshot().spans[0].start).toBe(label.length + 1)
    expect(controller.getSnapshot().text).toBe(label + ' ' + label)
  })

  it('keeps span positions across IME text insertion equal to an existing tag', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label, [level])
    controller.select(at(0))
    controller.compositionStart()
    controller.editNative(label + label, at(label.length))
    controller.compositionEnd(label + label, at(label.length))
    expect(controller.getSnapshot().spans[0].start).toBe(label.length)
    expect(controller.getSnapshot().mentions).toEqual([level])
  })

  it('normalizes initial CRLF and CR before binding tags and deleting them', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, '\r\n' + label + '\rend', [level])
    expect(controller.getSnapshot().text).toBe('\n' + label + '\nend')
    expect(controller.getSnapshot().spans[0].start).toBe(1)
    controller.select(at(1 + label.length))
    controller.key('Backspace')
    expect(controller.getSnapshot().text).toBe('\n\nend')
    expect(controller.getSnapshot().mentions).toEqual([])
  })

  it('normalizes external value and compares normalized echoes without publishing in a loop', () => {
    const label = mentionLabel(level)
    const { controller } = setup()
    controller.syncExternal('a\r\n' + label, [level])
    const listener = vi.fn()
    const unsubscribe = controller.subscribe(listener)
    controller.syncExternal('a\r\n' + label, [{ ...level }])
    controller.syncExternal('a\n' + label, [{ ...level }])
    expect(listener).not.toHaveBeenCalled()
    expect(controller.getSnapshot().spans[0].start).toBe(2)
    unsubscribe()
  })

  it('normalizes pasted line endings before shifting an existing span', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label, [level])
    controller.select(at(0))
    controller.replaceSelection('a\r\nb\r')
    expect(controller.getSnapshot().text).toBe('a\nb\n' + label)
    expect(controller.getSnapshot().selection).toEqual(at(4))
    expect(controller.getSnapshot().spans[0].start).toBe(4)
    controller.key('ArrowRight')
    expect(controller.getSnapshot().selection).toEqual(at(4 + label.length))
  })

  it('normalizes native input and its raw selection offsets at the input boundary', () => {
    const label = mentionLabel(level)
    const { controller } = setup(undefined, label, [level])
    controller.select(at(0))
    controller.editNative('a\r\n' + label, at(3))
    expect(controller.getSnapshot().selection).toEqual(at(2))
    expect(controller.getSnapshot().spans[0].start).toBe(2)
    expect(controller.getSnapshot().text).toBe('a\n' + label)
  })

  it('rejects multiline candidate names explicitly instead of rewriting their identity', async () => {
    const multiline = item('level', 10, 'Level\r\nOne')
    const { controller } = setup(async () => result('level', [multiline]))
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    controller.choose(0)
    expect(controller.getSnapshot().mentions).toEqual([])
    expect(popup(controller)).toContain('Names with line breaks cannot be inserted as tags.')
    expect(multiline.name).toBe('Level\r\nOne')
  })
})

describe('MentionInput rendering', () => {
  it('renders escaped labels, invalid identity and accessible disabled input without fetching', () => {
    const fetch = vi.fn()
    const hostile: Mention = { kind: 'link', id: 15, name: '<script>alert(1)</script>' }
    const text = `${mentionLabel(level)} ${mentionLabel(sameName)} ${mentionLabel(hostile)}`
    const html = renderToStaticMarkup(<MentionInput value={text} mentions={[level, sameName, hostile]}
      invalid={[sameName]} disabled onChange={vi.fn()} onSubmit={vi.fn()} fetchCandidates={fetch} />)
    expect(html.match(/mention-tag is-invalid/g)).toHaveLength(1)
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).toContain('role="combobox"')
    expect(html).toContain('disabled=""')
    expect(html).toContain('Some model items are unavailable.')
    expect(fetch).not.toHaveBeenCalled()
  })
  it('renders the placeholder for empty input and has no popup', () => {
    const html = renderToStaticMarkup(<MentionInput value="" mentions={[]} placeholder="Ask Revit"
      onChange={vi.fn()} onSubmit={vi.fn()} fetchCandidates={vi.fn()} />)
    expect(html).toContain('placeholder="Ask Revit"')
    expect(html).not.toContain('mention-popup')
  })
  it('reports an unrepresentable external mention without changing its name', () => {
    const multiline = { ...level, name: 'Level\r\nOne' }
    const html = renderToStaticMarkup(<MentionInput value={mentionLabel(multiline)} mentions={[multiline]}
      onChange={vi.fn()} onSubmit={vi.fn()} fetchCandidates={vi.fn()} />)
    expect(html).toContain('Names with line breaks cannot be represented as tags.')
    expect(multiline.name).toBe('Level\r\nOne')
    expect(serialize('\r\n' + mentionLabel(level), [level])).toEqual({ text: '\n' + mentionLabel(level), mentions: [level] })
  })
})

const f1: Mention = { kind: 'level', id: 21, name: 'F1' }
const f2: Mention = { kind: 'level', id: 22, name: 'F2' }
const ids = (controller: MentionController) => controller.getSnapshot().spans.map(span => span.mention.id)

describe('binding mentions to the text, whatever the array order', () => {
  const a: Mention = { kind: 'level', id: 1, name: 'A' }
  const b: Mention = { kind: 'link', id: 2, name: 'B' }
  const ab: Mention = { kind: 'level', id: 3, name: 'A B' }

  it('binds a reversed array by text and returns text order', () => {
    const text = `${mentionLabel(a)} and ${mentionLabel(b)}`
    expect(serialize(text, [b, a])).toEqual({ text, mentions: [a, b] })
    expect(removeMentionAt(text, [b, a], 0)).toEqual({ text: `${mentionLabel(a)} and `, mentions: [a] })
    expect(removeMentionAt(text, [b, a], 1)).toEqual({ text: ` and ${mentionLabel(b)}`, mentions: [b] })
    expect(removeMentionAt(text, [b, a, f1], 2)).toEqual({ text, mentions: [a, b] })
  })
  it('lets the longer of two names that prefix each other claim first, in either order', () => {
    const text = `${mentionLabel(ab)} then ${mentionLabel(a)}`
    expect(mentionSpans(text, [a, ab]).map(span => [span.start, span.mention.id]))
      .toEqual([[0, 3], [mentionLabel(ab).length + ' then '.length, 1]])
    expect(serialize(text, [a, ab]).mentions).toEqual([ab, a])
    expect(serialize(text, [ab, a]).mentions).toEqual([ab, a])
    const flipped = `${mentionLabel(a)} then ${mentionLabel(ab)}`
    expect(serialize(flipped, [ab, a]).mentions).toEqual([a, ab])
    expect(removeMentionAt(flipped, [ab, a], 0)).toEqual({ text: `${mentionLabel(a)} then `, mentions: [a] })
  })
  it('keeps identical labels with different ids in array order and drops only the absent one', () => {
    const text = `${mentionLabel(level)} x ${mentionLabel(level)}`
    expect(serialize(text, [sameName, level]).mentions).toEqual([sameName, level])
    expect(serialize(text, [level, { ...level, id: 12, name: 'Gone' }, sameName]).mentions).toEqual([level, sameName])
  })
})

describe('two mentions of the same kind keep their ids', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('binds a reversed parent array, reports the text order once, and keeps ids through edits, deletion and re-ordering', async () => {
    const text = `on ${mentionLabel(f1)} and ${mentionLabel(f2)}`
    const { controller, onChange } = setup(async () => result('level', [item('level', 21, 'F1')]), text, [f2, f1])
    expect(controller.getSnapshot().mentions).toEqual([f1, f2])
    controller.syncExternal(text, [f2, f1])
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenLastCalledWith(text, [f1, f2])
    controller.syncExternal(text, [f2, f1])
    controller.syncExternal(text, [f1, f2])
    expect(onChange).toHaveBeenCalledTimes(1)

    controller.select(at(0))
    controller.replaceSelection('build ')
    controller.select(at(controller.getSnapshot().text.indexOf(' and')))
    controller.replaceSelection(' first')
    expect(controller.getSnapshot().text).toBe(`build on ${mentionLabel(f1)} first and ${mentionLabel(f2)}`)
    expect(ids(controller)).toEqual([21, 22])
    expect(onChange).toHaveBeenLastCalledWith(controller.getSnapshot().text, [f1, f2])

    controller.select(at(controller.getSnapshot().spans[0].end))
    expect(controller.key('Backspace')).toBe(true)
    expect(controller.getSnapshot().mentions).toEqual([f2])

    controller.select(at(controller.getSnapshot().text.length))
    controller.replaceSelection(' then @level:')
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.key('Enter')).toBe(true)
    const after = controller.getSnapshot()
    expect(after.text).toBe(`build on  first and ${mentionLabel(f2)} then ${mentionLabel(f1)}`)
    expect(after.mentions).toEqual([f2, f1])
    expect(ids(controller)).toEqual([22, 21])
    expect(onChange).toHaveBeenLastCalledWith(after.text, [f2, f1])
    controller.syncExternal(after.text, [f2, f1])
    expect(ids(controller)).toEqual([22, 21])
  })
  it('keeps two identical labels apart through an edit between them and the deletion of the first', () => {
    const label = mentionLabel(level)
    const { controller, onChange } = setup(undefined, `${label} ${label}`, [level, sameName])
    controller.select(at(label.length))
    controller.replaceSelection(' and')
    expect(controller.getSnapshot().text).toBe(`${label} and ${label}`)
    expect(ids(controller)).toEqual([10, 11])
    controller.select(at(0))
    expect(controller.key('Delete')).toBe(true)
    expect(onChange).toHaveBeenLastCalledWith(` and ${label}`, [sameName])
  })
  it('a caret carried over to new parent text never lands inside a tag', () => {
    const { controller } = setup(undefined, 'abcdefghij')
    controller.syncExternal(`${mentionLabel(level)} end`, [level])
    expect(controller.getSnapshot().selection).toEqual(at(mentionLabel(level).length))
  })
  it('reports a parent mention that is not in the text once, shows a notice, and submits what it holds', () => {
    const text = `${mentionLabel(f1)} only`
    const { controller, onChange, onSubmit } = setup(undefined, text, [f1, f2])
    expect(controller.getSnapshot().mentions).toEqual([f1])
    expect(controller.getSnapshot().dropped).toEqual([f2])
    controller.syncExternal(text, [f1, f2])
    controller.syncExternal(text, [f1, f2])
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenLastCalledWith(text, [f1])
    controller.syncExternal(text, [f1])
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().dropped).toEqual([f2])
    expect(renderToStaticMarkup(<MentionInput value={text} mentions={[f1, f2]} onChange={vi.fn()}
      onSubmit={vi.fn()} fetchCandidates={vi.fn()} />)).toContain('were not found in the text and were removed')
    input(controller, text + '!')
    expect(controller.getSnapshot().dropped).toEqual([])
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith(text + '!', [f1])
  })
})

describe('Enter is never swallowed by a picker with nothing to pick', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('sends after an e-mail address: letters that start no kind open no picker', () => {
    const { controller, onSubmit } = setup()
    input(controller, 'write to bob@corp')
    expect(controller.getSnapshot().menu).toBeNull()
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('write to bob@corp', [])
    input(controller, 'see @x')
    expect(controller.getSnapshot().menu).toBeNull()
    input(controller, 'see @L')
    expect(controller.getSnapshot().menu).toMatchObject({ mode: 'kinds', query: 'L' })
  })
  it('after Escape the same @ stays closed while the caret stays after it, so the next key and Enter are plain text', async () => {
    const fetch = vi.fn(async () => result('level', [item()]))
    const { controller, onSubmit } = setup(fetch)
    input(controller, '@level: hello')
    expect(controller.getSnapshot().menu?.mode).toBe('items')
    expect(controller.key('Escape')).toBe(true)
    input(controller, '@level: hello!')
    expect(controller.getSnapshot().menu).toBeNull()
    await vi.advanceTimersByTimeAsync(500)
    expect(fetch).not.toHaveBeenCalled()
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('@level: hello!', [])
    controller.select(at(0))
    controller.select(at('@level: hello!'.length))
    expect(controller.getSnapshot().menu?.mode).toBe('items')
  })
  it('Escape on the kind list holds while letters are added and removed after the same @', () => {
    const { controller, onSubmit } = setup()
    input(controller, 'x @le')
    controller.key('Escape')
    input(controller, 'x @le ')
    input(controller, 'x @le')
    input(controller, 'x @lev')
    expect(controller.getSnapshot().menu).toBeNull()
    controller.key('Enter')
    expect(onSubmit).toHaveBeenCalledWith('x @lev', [])
    input(controller, 'x @lev @')
    expect(controller.getSnapshot().menu?.mode).toBe('kinds')
  })
  it('a closed picker (click outside) is not dismissed: the next key reopens it', () => {
    const { controller } = setup()
    input(controller, '@l')
    controller.close()
    expect(controller.getSnapshot().menu).toBeNull()
    input(controller, '@le')
    expect(controller.getSnapshot().menu).toMatchObject({ mode: 'kinds', query: 'le' })
  })
  it('Enter sends when the open list has no match or only disabled items; Tab and arrows are not trapped', async () => {
    let items: MentionItem[] = []
    const { controller, onSubmit } = setup(async () => result('link', items))
    input(controller, 'on @link:Nope')
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.getSnapshot().status).toBe('ready')
    expect(popup(controller)).toContain('No matching items')
    expect(controller.key('ArrowDown')).toBe(false)
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenLastCalledWith('on @link:Nope', [])
    expect(controller.getSnapshot().menu).toBeNull()

    items = [item('link', 1, 'Unloaded', { disabled: true, detail: 'The link is unloaded.' })]
    input(controller, 'on @link:')
    input(controller, 'on @link:U')
    expect(controller.getSnapshot().menu).toBeNull()
    input(controller, 'on ')
    input(controller, 'on @link:U')
    await vi.advanceTimersByTimeAsync(200)
    expect(popup(controller)).toContain('The link is unloaded.')
    expect(controller.key('Tab')).toBe(false)
    expect(controller.getSnapshot().menu).toBeNull()
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenLastCalledWith('on @link:U', [])
    expect(onSubmit).toHaveBeenCalledTimes(2)
  })
  it('a new fetchCandidates identity neither clears nor refetches the open list', async () => {
    const first = vi.fn(async () => result('level', [item()]))
    const { controller, onChange, onSubmit } = setup(first)
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    const second = vi.fn(async () => result('level', [item('level', 99, 'Other')]))
    controller.configure({ onChange, onSubmit, fetchCandidates: second })
    await vi.advanceTimersByTimeAsync(500)
    expect(second).not.toHaveBeenCalled()
    expect(controller.getSnapshot().candidates?.items[0].id).toBe(10)
    input(controller, '@level:O')
    await vi.advanceTimersByTimeAsync(200)
    expect(second).toHaveBeenCalledWith('level', { q: 'O' })
    expect(first).toHaveBeenCalledTimes(1)
  })
  it('lists, inserts and deletes a very long item name intact', async () => {
    const long = 'Basic Wall - Generic '.repeat(200) + 'end'
    const { controller } = setup(async () => result('level', [item('level', 7, long, { detail: 'x'.repeat(500) })]))
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    expect(popup(controller)).toContain(long)
    expect(controller.key('Enter')).toBe(true)
    expect(controller.getSnapshot().text).toBe(`@level:${long}`)
    expect(controller.getSnapshot().mentions).toEqual([{ kind: 'level', id: 7, name: long }])
    expect(controller.key('Backspace')).toBe(true)
    expect(controller.getSnapshot().text).toBe('')
  })
})

describe('DOM adapter: IME and paste', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  const textarea = (controller: MentionController): TextareaLike => {
    const { selection } = controller.getSnapshot()
    const element: TextareaLike = {
      selectionStart: selection.start, selectionEnd: selection.end, selectionDirection: selection.direction,
      setSelectionRange(start, end) { element.selectionStart = start; element.selectionEnd = end },
    }
    return element
  }
  const keyDown = (controller: MentionController, key: string, native: { isComposing?: boolean; keyCode?: number } = {}) => {
    const event: KeyDownLike = {
      key, keyCode: native.keyCode ?? (key === 'Enter' ? 13 : 0), shiftKey: false, ctrlKey: false, metaKey: false, altKey: false,
      nativeEvent: { isComposing: native.isComposing ?? false }, currentTarget: textarea(controller), preventDefault: vi.fn(),
    }
    handleKeyDown(controller, event)
    return event.preventDefault
  }
  const paste = (controller: MentionController, text: string) => {
    const event = { clipboardData: { getData: () => text }, currentTarget: textarea(controller), preventDefault: vi.fn() }
    handlePaste(controller, event)
    return event.preventDefault
  }

  it('the Enter that confirms a Chinese composition neither picks nor sends (Chrome isComposing, Safari keyCode 229)', async () => {
    const floor: Mention = { kind: 'level', id: 30, name: '楼层 1' }
    const fetch = vi.fn(async () => result('level', [item('level', 30, '楼层 1')]))
    const { controller, onSubmit } = setup(fetch)
    input(controller, '@level:')
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.getSnapshot().status).toBe('ready')

    controller.compositionStart()
    controller.editNative('@level:lou', at(10))
    expect(keyDown(controller, 'Enter', { isComposing: true, keyCode: 229 })).not.toHaveBeenCalled()
    controller.compositionEnd('@level:楼', at(8))
    expect(keyDown(controller, 'Enter', { keyCode: 229 })).not.toHaveBeenCalled()
    expect(keyDown(controller, 'Tab', { keyCode: 229 })).not.toHaveBeenCalled()
    expect(controller.getSnapshot().text).toBe('@level:楼')
    expect(controller.getSnapshot().mentions).toEqual([])
    expect(onSubmit).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('level', { q: '楼' })
    expect(keyDown(controller, 'Enter')).toHaveBeenCalled()
    expect(controller.getSnapshot().mentions).toEqual([floor])
    expect(onSubmit).not.toHaveBeenCalled()

    const text = controller.getSnapshot().text
    controller.compositionStart()
    controller.editNative(text + 'hao', at(text.length + 3))
    expect(keyDown(controller, 'Enter', { isComposing: true, keyCode: 229 })).not.toHaveBeenCalled()
    controller.compositionEnd(text + '好', at(text.length + 1))
    expect(keyDown(controller, 'Enter', { keyCode: 229 })).not.toHaveBeenCalled()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(keyDown(controller, 'Enter')).toHaveBeenCalled()
    expect(onSubmit).toHaveBeenCalledWith(text + '好', [floor])
  })
  it('pasted text with @ opens no picker and creates no tag, and Enter sends it', () => {
    const { controller, onSubmit } = setup()
    const pasted = 'see @level:Level 1 and mail bob@le'
    expect(paste(controller, pasted)).toHaveBeenCalled()
    expect(controller.getSnapshot().text).toBe(pasted)
    expect(controller.getSnapshot().menu).toBeNull()
    expect(controller.getSnapshot().mentions).toEqual([])
    input(controller, pasted + 'v')
    expect(controller.getSnapshot().menu).toBeNull()
    expect(keyDown(controller, 'Enter')).toHaveBeenCalled()
    expect(onSubmit).toHaveBeenCalledWith(pasted + 'v', [])
  })
  it('a name pasted after a typed @kind: keeps the picker searching', async () => {
    const fetch = vi.fn(async () => result('level', [item('level', 22, 'F2')]))
    const { controller } = setup(fetch)
    input(controller, 'on @level:')
    paste(controller, 'F2')
    expect(controller.getSnapshot().menu).toMatchObject({ mode: 'items', query: 'F2' })
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenLastCalledWith('level', { q: 'F2' })
    expect(keyDown(controller, 'Enter')).toHaveBeenCalled()
    expect(controller.getSnapshot().mentions).toEqual([f2])
  })
})

describe('which @ opens the picker (planner ruling: not inside an ASCII word)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('an @ right after an ASCII letter or digit opens nothing, so Enter sends', () => {
    const { controller, onSubmit } = setup()
    for (const text of ['bob@le', 'mail bob@le', 'room3@le', 'x@level:F', 'B2@link:']) {
      input(controller, text)
      expect(controller.getSnapshot().menu, text).toBeNull()
    }
    input(controller, 'mail bob@le')
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).toHaveBeenCalledWith('mail bob@le', [])
  })
  it('after a space, punctuation, the start of a line or any non-ASCII character it opens', () => {
    const { controller } = setup()
    for (const text of ['@le', 'x @le', '(@le', '"@le', '_@le', 'x\n@le', '看@le', '在标高@le', 'é@le']) {
      input(controller, text)
      expect(controller.getSnapshot().menu, JSON.stringify(text)).toMatchObject({ mode: 'kinds', query: 'le' })
    }
  })
  it('Chinese without spaces reaches the items too', async () => {
    const fetch = vi.fn(async () => result('level', [item('level', 22, 'F2')]))
    const { controller } = setup(fetch)
    input(controller, '在@level:F')
    expect(controller.getSnapshot().menu).toMatchObject({ mode: 'items', kind: 'level', query: 'F' })
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenCalledWith('level', { q: 'F' })
    expect(controller.key('Enter')).toBe(true)
    expect(controller.getSnapshot().text).toBe('在@level:F2')
    expect(controller.getSnapshot().mentions).toEqual([{ kind: 'level', id: 22, name: 'F2' }])
  })
  it('the rule is about the character before the @, wherever the caret is', () => {
    expect(menuAt('bob@le', [], 6, null)).toBeNull()
    expect(menuAt('看@le', [], 4, null)).toMatchObject({ mode: 'kinds', start: 1 })
    expect(menuAt('(@le', [], 4, null)).toMatchObject({ mode: 'kinds', start: 1 })
    expect(menuAt('@le', [], 3, null)).toMatchObject({ mode: 'kinds', start: 0 })
  })
})
