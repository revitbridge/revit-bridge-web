// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FetchCandidates, FetchPreview, Mention, MentionItem } from '../../../types/hostv2'
import MentionInput from './MentionInput'

const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('navigator', { languages: ['en-US'] })
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function Editor({ fetch, submit }: { fetch: FetchCandidates; submit: (text: string, picks: Mention[]) => void }) {
  const [value, setValue] = useState('')
  const [picks, setPicks] = useState<Mention[]>([])
  return <MentionInput value={value} mentions={picks} onChange={(text, items) => { setValue(text); setPicks(items) }}
    fetchCandidates={fetch} onSubmit={submit} />
}
function edit(value: string) {
  const input = container.querySelector('textarea')!
  act(() => {
    input.focus()
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
function click(text: string) {
  const button = [...container.querySelectorAll('button')].find(b => b.textContent?.includes(text))
  expect(button, text).toBeTruthy()
  act(() => button!.click())
}
const debounce = () => act(async () => { await vi.advanceTimersByTimeAsync(200) })
const facets = [{ category: 'OST_Walls', label: 'Walls', count: 2 }, { category: 'OST_Doors', label: 'Doors', count: 1 }]
const wall = (id: number): MentionItem => ({ kind: 'element', id, name: 'Basic 200', label: 'Basic 200',
  detail: 'L1', category: 'OST_Walls' })

it('keeps checked identities and the category through scrolling and typing, and submits both IDs', async () => {
  const submit = vi.fn()
  const fetch = vi.fn<FetchCandidates>(async (kind, opts) => ({
    kind, facets, total: 3, truncated: !opts.offset,
    items: opts.offset ? [wall(93)] : [wall(91), { ...wall(92), category: 'OST_Doors' }],
  }))
  act(() => root.render(<Editor fetch={fetch} submit={submit} />))
  edit('@')
  click('Get current Revit selection')
  await debounce()
  expect(fetch).toHaveBeenLastCalledWith('element', { q: '' })
  click('Walls (2)')
  click('#91')
  edit('@element:200')
  await debounce()
  expect(container.querySelector('[role=group]')?.getAttribute('aria-label')).toBe('Walls')
  expect(container.querySelector('[role=listbox]')?.textContent).not.toContain('#92')
  const list = container.querySelector('[role=listbox]')!
  Object.defineProperties(list, { clientHeight: { value: 200 }, scrollTop: { value: 300 }, scrollHeight: { value: 500 } })
  await act(async () => { list.dispatchEvent(new Event('scroll')) })
  expect(fetch).toHaveBeenLastCalledWith('element', { q: '200', offset: 2 })
  click('#93')
  click('Insert selected (2)')
  const input = container.querySelector('textarea')!
  expect(input.value).toBe('@element:Basic 200#91 @element:Basic 200#93')
  expect(input).toBe(document.activeElement)
  act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  expect(submit).toHaveBeenCalledWith(input.value, [91, 93].map(id => ({ kind: 'element', id, name: 'Basic 200' })))
})

it('shows Chinese picker instructions and caps a selection at 20 distinct elements', async () => {
  vi.stubGlobal('navigator', { languages: ['zh-CN'] })
  const rows = Array.from({ length: 21 }, (_, i) => wall(100 + i))
  const fetch = vi.fn<FetchCandidates>(async kind => ({ kind, total: rows.length, items: rows, truncated: false }))
  act(() => root.render(<Editor fetch={fetch} submit={vi.fn()} />))
  const input = container.querySelector('textarea')!
  expect(input.placeholder).toContain('输入消息')
  expect(input.getAttribute('aria-label')).toBe('消息')
  expect(container.textContent).toContain('按 Enter 发送')
  edit('@')
  click('取 Revit 当前选择')
  await debounce()
  expect(container.querySelector('[role=listbox]')?.getAttribute('aria-label')).toBe('可选模型项')
  for (const row of rows.slice(0, 20)) click('#' + row.id)
  const last = [...container.querySelectorAll<HTMLButtonElement>('[role=option]')].at(-1)!
  expect(last.disabled).toBe(true)
  expect(container.textContent).toContain('最多引用 20 个')
  click('#100')
  expect(last.disabled).toBe(false)
  click('#120')
  click('插入所选 (20)')
  expect(input.value.match(/@element:/g)).toHaveLength(20)
  expect(input.value).not.toContain('#100')
  expect(input.value).toContain('#120')
})

it('shows the inferred category chip and keeps the remaining search words when clicked', async () => {
  const fetch = vi.fn<FetchCandidates>(async (kind, opts) => ({
    kind, facets, total: 1, truncated: false, category_matched: opts.q?.toLowerCase().includes('wal') ? ['OST_Walls'] : [],
    items: [{ ...wall(8), kind: 'type' }],
  }))
  act(() => root.render(<Editor fetch={fetch} submit={vi.fn()} />))
  edit('@type:wal Basic 200')
  await debounce()
  expect(container.querySelector('[role=group]')?.textContent).toContain('Walls (2)')
  click('Walls ×')
  await debounce()
  expect(container.querySelector('textarea')!.value).toBe('@type:Basic 200')
  expect(fetch).toHaveBeenLastCalledWith('type', { q: 'Basic 200' })
})

it('says when the whole model is searched and pictures the element the picker is on', async () => {
  // jsdom has no canvas: the picture's caption is what is checked here, the drawing in shape.test.ts.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  const fetch = vi.fn<FetchCandidates>(async kind => ({
    kind, source: 'model', facets, total: 2, truncated: false, items: [wall(91), { ...wall(92), document_id: 'doc' }],
  }))
  const preview = vi.fn<FetchPreview>(async id => ({
    kind: 'element', id, name: 'Basic 200', category: 'OST_Walls', category_label: 'Walls', shape: 'box',
    size_mm: [4000, 200, 3000], vertices: [], triangles: [], partial: false, approximate: true,
  }))
  function WithPreview() {
    const [value, setValue] = useState('')
    const [picks, setPicks] = useState<Mention[]>([])
    return <MentionInput value={value} mentions={picks} onChange={(text, items) => { setValue(text); setPicks(items) }}
      fetchCandidates={fetch} fetchPreview={preview} onSubmit={() => {}} />
  }
  act(() => root.render(<WithPreview />))
  edit('@element:')
  await debounce()
  expect(fetch).toHaveBeenLastCalledWith('element', { q: '' })
  expect(container.textContent).toContain('Nothing is selected in Revit: searching the whole model.')
  // The row the keyboard is on is pictured, after a short wait.
  expect(container.querySelector('.mention-preview')).toBeTruthy()
  expect(preview).not.toHaveBeenCalled()
  await debounce()
  expect(preview).toHaveBeenCalledTimes(1)
  expect(preview).toHaveBeenLastCalledWith(91)
  expect(container.querySelector('.mention-preview-caption')?.textContent).toContain('Walls #91')
  expect(container.querySelector('.mention-preview-caption')?.textContent).toContain('4000 × 200 × 3000 mm')
  expect(container.querySelector('.mention-preview-caption')?.textContent).toContain('Approximate size only')
  // Pointing at another row pictures that one.
  const second = [...container.querySelectorAll('[role=option]')].find(b => b.textContent?.includes('#92'))!
  act(() => { second.dispatchEvent(new MouseEvent('mouseover', { bubbles: true })) })
  await debounce()
  expect(preview).toHaveBeenLastCalledWith(92)
  expect(container.querySelector('.mention-preview-caption')?.textContent).toContain('Walls #92')
})

it('shows no picture without a preview function, and the selection wording for a selection', async () => {
  const fetch = vi.fn<FetchCandidates>(async kind => ({ kind, source: 'selection', selected: 1, facets, total: 0, truncated: false, items: [] }))
  act(() => root.render(<Editor fetch={fetch} submit={() => {}} />))
  edit('@element:')
  await debounce()
  expect(container.querySelector('.mention-preview')).toBeNull()
  expect(container.textContent).toContain('No matching selected elements on this page.')
  expect(container.textContent).not.toContain('searching the whole model')
})
