import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { FetchCandidates, Mention, MentionCandidates, MentionItem } from '../../../types/hostv2'
import { MentionController } from './controller'
import MentionInput, { MentionSuggestions } from './MentionInput'
import { applyNativeEdit, matchingKinds, mentionLabel, mentionSpans, normalizeSelection, removeMentionAt, replaceRange, serialize } from './model'

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
    input(controller, '@link:')
    controller.syncExternal('@link:', [], true)
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenCalledTimes(1)
    controller.syncExternal('@link:', [], false)
    input(controller, '@link:x')
    controller.destroy()
    await vi.advanceTimersByTimeAsync(200)
    expect(fetch).toHaveBeenCalledTimes(1)
  })
  it('never submits while a popup is loading, empty or errored, and Escape closes it', async () => {
    const { controller, onSubmit } = setup(async () => { throw new Error('Model offline') })
    input(controller, '@level:')
    expect(controller.key('Enter')).toBe(true)
    await vi.advanceTimersByTimeAsync(200)
    expect(controller.key('Enter')).toBe(true)
    expect(onSubmit).not.toHaveBeenCalled()
    expect(popup(controller)).toContain('role="alert"')
    expect(popup(controller)).toContain('Model offline')
    input(controller, '@level:still editable')
    expect(controller.getSnapshot().text).toContain('still editable')
    controller.key('Escape')
    controller.select(at(controller.getSnapshot().text.length))
    controller.key('Enter')
    expect(onSubmit).toHaveBeenCalledTimes(1)
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
