// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { FetchCandidates, Mention, MentionItem } from '../../../types/hostv2'
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
  expect(fetch).toHaveBeenLastCalledWith('element', { q: '', source: 'selection' })
  click('Walls (2)')
  click('#91')
  edit('@element:200')
  await debounce()
  expect(container.querySelector('[role=group]')?.getAttribute('aria-label')).toBe('Walls')
  expect(container.querySelector('[role=listbox]')?.textContent).not.toContain('#92')
  const list = container.querySelector('[role=listbox]')!
  Object.defineProperties(list, { clientHeight: { value: 200 }, scrollTop: { value: 300 }, scrollHeight: { value: 500 } })
  await act(async () => { list.dispatchEvent(new Event('scroll')) })
  expect(fetch).toHaveBeenLastCalledWith('element', { q: '200', source: 'selection', offset: 2 })
  click('#93')
  click('Insert selected (2)')
  const input = container.querySelector('textarea')!
  expect(input.value).toBe('@element:Basic 200#91 @element:Basic 200#93')
  expect(input).toBe(document.activeElement)
  act(() => input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })))
  expect(submit).toHaveBeenCalledWith(input.value, [91, 93].map(id => ({ kind: 'element', id, name: 'Basic 200' })))
})

it('shows the inferred category chip and clears its search when clicked', async () => {
  const fetch = vi.fn<FetchCandidates>(async (kind, opts) => ({
    kind, facets, total: 1, truncated: false, category_matched: opts.q ? ['OST_Walls'] : [],
    items: [{ ...wall(8), kind: 'type' }],
  }))
  act(() => root.render(<Editor fetch={fetch} submit={vi.fn()} />))
  edit('@type:wal 200')
  await debounce()
  expect(container.querySelector('[role=group]')?.textContent).toContain('Walls (2)')
  click('Walls ×')
  await debounce()
  expect(fetch).toHaveBeenLastCalledWith('type', { q: '' })
})
