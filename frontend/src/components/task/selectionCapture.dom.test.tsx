// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as client from '../../api/client'
import type { ChatMessage } from '../../types/api'
import ChatPanel from './ChatPanel'
import { readFileSync } from 'node:fs'

const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
const onSend = vi.fn()
const picked = { kind: 'element', id: 7, name: 'Door', document_id: 'doc' }
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  HTMLElement.prototype.scrollTo = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})
function render(messages: ChatMessage[] = [], props: Partial<Parameters<typeof ChatPanel>[0]> = {}) {
  act(() => root.render(<ChatPanel title="Task" transcript={{ messages, sessionId: 'session', streaming: false }}
    onSend={onSend} {...props} />))
}
async function click(text: string) {
  const button = [...container.querySelectorAll('button')].find(b => b.textContent === text)
  expect(button).toBeDefined()
  await act(async () => button!.click())
}

const tick = (ms = 0) => act(async () => { await vi.advanceTimersByTimeAsync(ms) })
const question = (): ChatMessage => ({ role: 'assistant', content: '', form: {
  ask_id: 'q', questions: [], selection: { mode: 'single', prompt: 'Pick a wall' }
} })
function edit() {
  const input = container.querySelector('textarea')!
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, 'Inspect')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}
it.each([true, false])('shows the verified selection and honors removal %s on send', async remove => {
  const selection = { count: 3, stamp: 'walls', document_id: 'doc' }
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValue(selection)
  render([], { support: 'ready' })
  await tick()
  expect(container.textContent).toContain('3 selected in Revit')
  expect(container.textContent).not.toContain('Select in Revit')
  expect(fetch.mock.calls.map(c => c[0])).toEqual(['/api/v1/bridge/selection-state'])
  if (remove) await click('Current selection ×')
  edit()
  await click('Send')
  expect(onSend.mock.calls[0]).toEqual(remove ? ['Inspect', []] : ['Inspect', [], selection])
})
it('pauses while hidden or streaming and backs off failed native state reads', async () => {
  const fetch = vi.spyOn(client, 'apiFetch').mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValue({ count: 0, stamp: 'empty', document_id: 'doc' })
  render([], { support: 'ready' })
  await tick()
  await tick(2000)
  expect(fetch).toHaveBeenCalledTimes(1)
  await tick(2000)
  expect(container.textContent).toContain('0 selected in Revit')
  expect(container.textContent).not.toContain('Current selection ×')
  await tick(2000)
  expect(fetch).toHaveBeenCalledTimes(3)
  const hide = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  await tick(6000)
  expect(fetch).toHaveBeenCalledTimes(3)
  hide.mockReturnValue(false)
  render([], { support: 'ready', disabled: true })
  await tick(6000)
  expect(fetch).toHaveBeenCalledTimes(3)
})
it('takes legacy selection manually, without polling', async () => {
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValue({ items: [picked] })
  render([], { support: 'legacy' }); await tick(5000)
  expect(fetch).not.toHaveBeenCalled()
  await click('Take current selection')
  await click('Send')
  expect(onSend).toHaveBeenCalledWith('@element:Door#7', [picked])
})
it('refreshes the chip on a stale send and attaches only the new identity', async () => {
  const state = { count: 21, stamp: 'walls', document_id: 'doc' }
  const latest = { ...state, stamp: 'doors', count: 33 }
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValueOnce(state).mockResolvedValueOnce(latest)
  render([], { support: 'ready' })
  await tick()
  edit()
  await click('Send')
  expect(onSend).toHaveBeenCalledWith('Inspect', [], state)
  render([], { support: 'ready', transcript: { messages: [], sessionId: 'session', streaming: false, selectionRevision: 1 } })
  expect(container.textContent).not.toContain('21 selected')
  await tick()
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(container.textContent).toContain('33 selected in Revit')
  edit()
  await click('Send')
  expect(onSend).toHaveBeenLastCalledWith('Inspect', [], latest)
})
it('keeps the live selection out of a submitted form and reconciler option', async () => {
  vi.spyOn(client, 'apiFetch').mockResolvedValue({ count: 5, stamp: 'walls', document_id: 'doc' })
  const submit = vi.fn(), option = vi.fn()
  const options = [{ label: 'North', value: 1 }, { label: 'South', value: 2 }]
  const q = { id: 'dir', text: 'Direction?', options, multiple: false, allow_other: false }
  render([{ role: 'assistant', content: '', form: { ask_id: 'q', questions: [q] } }],
    { support: 'ready', onSubmitForm: submit })
  await tick()
  expect(container.textContent).toContain('5 selected in Revit')
  act(() => container.querySelector<HTMLInputElement>('input[type=radio]')!.click())
  act(() => container.querySelector<HTMLButtonElement>('form button')!.click())
  expect(submit.mock.calls[0]).toHaveLength(2)
  render([{ role: 'assistant', content: '' }], { support: 'ready', questions: [{ ...q, param: 'dir', why: '' }],
    questionMessage: 0, onAnswer: option })
  await click('North')
  expect(option.mock.calls[0]).toHaveLength(2)
})
it('starts a planner watch automatically and answers the same question via legacy fallback', async () => {
  const select = vi.fn()
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValueOnce({ error: 'unsupported_addin' })
    .mockResolvedValueOnce({ items: [picked] })
  render([question()], { onSelectionAnswer: select, support: 'ready' })
  await tick()
  expect(fetch.mock.calls[0][0]).toBe('/api/v1/bridge/watch-selection')
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toMatchObject({ prompt: 'Pick a wall', mode: 'single' })
  expect(container.querySelectorAll('.selection-watch')).toHaveLength(1)
  await click('Take current selection')
  expect(select).toHaveBeenCalledWith('q', [picked])
})
it.each(['Cancel', 'unmount', 'pagehide'])('stops the watch on %s', async end => {
  const fetch = vi.spyOn(client, 'apiFetch').mockImplementation((_url, init) => init?.method === 'DELETE'
    ? Promise.resolve({ cancelled: true }) : new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', reject)))
  render([question()]); await tick()
  if (end === 'Cancel') await click(end)
  else if (end === 'unmount') render([])
  else await act(async () => { window.dispatchEvent(new Event('pagehide')) })
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true)
  expect(fetch.mock.calls[1][1]?.method).toBe('DELETE')
})
it.each(['timed_out', 'document_changed', 'cancelled', 'Cancel'])('keeps the watch when hidden and retries after %s', async end => {
  let finish!: (reply: object) => void
  const fetch = vi.spyOn(client, 'apiFetch').mockImplementationOnce((_url, init) =>
    new Promise((resolve, reject) => { finish = resolve; init?.signal?.addEventListener('abort', reject) }))
    .mockImplementation(async (_url, init) => init?.method === 'DELETE' ? {} : { items: [picked] })
  const select = vi.fn()
  render([question()], { onSelectionAnswer: select })
  await tick()
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(false)
  hidden.mockReturnValue(false)
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  if (end === 'Cancel') await click(end)
  else await act(async () => finish({ [end]: true }))
  await click('Watch again')
  expect(fetch.mock.calls.filter(call => call[1]?.method === 'POST')).toHaveLength(2)
  expect(select).toHaveBeenCalledWith('q', [picked])
})
it.each(['Escape', 'outside', 'button'])('opens the viewport modal and restores focus and scroll on %s', async end => {
  const hide = vi.fn()
  render([{ role: 'assistant', content: '', images: [{ width: 1, height: 1, view_name: 'Floor plan',
    png_base64: 'aW1hZ2U=', capture_id: 'handle' }] }], { onHide: hide })
  expect(container.textContent).toContain('Used as context')
  expect(container.querySelector<HTMLInputElement>('[role=switch]')!.checked).toBe(false)
  const opener = container.querySelector<HTMLButtonElement>('[aria-label="Enlarge view"]')!
  document.body.style.overflow = 'auto'
  act(() => opener.click())
  const modal = document.querySelector('dialog')!
  expect(modal.open).toBe(true)
  expect(document.body.style.overflow).toBe('hidden')
  const close = modal.querySelector('button')!
  expect(document.activeElement).toBe(close)
  const tab = new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true })
  act(() => { close.dispatchEvent(tab); modal.querySelector('img')!.click() })
  expect(tab.defaultPrevented).toBe(true)
  expect(modal.open).toBe(true)
  act(() => { if (end === 'Escape') close.dispatchEvent(new KeyboardEvent('keydown', { key: end, bubbles: true }))
    else if (end === 'outside') modal.click(); else close.click() })
  expect(document.querySelector('dialog')).toBeNull()
  expect(document.activeElement).toBe(opener)
  expect(document.body.style.overflow).toBe('auto')
  act(() => container.querySelector<HTMLInputElement>('[role=switch]')!.click())
  expect(hide).toHaveBeenCalledWith('handle')
})
it.each(['authoring', 'remote_mcp', 'dismiss'] as const)('handles the missing-tool choice %s locally', async choice => {
  const go = vi.fn(), dismiss = vi.fn()
  const form = { request: 'Draw a spiral', question: 'Add one now?', choices: { authoring: 'Author', remote_mcp: 'Remote MCP', dismiss: 'Not now' } }
  render([{ role: 'assistant', content: '', missingTool: form }], { onDestination: go, onDismiss: dismiss })
  await click(form.choices[choice])
  if (choice === 'dismiss') expect(go).not.toHaveBeenCalled()
  else expect(go).toHaveBeenCalledWith(choice, 'Draw a spiral')
  expect(dismiss).toHaveBeenCalledWith(0)
  expect(onSend).not.toHaveBeenCalled()
})

it('centres the viewer and disables its short entrance animations under reduced motion', () => {
  const style = document.createElement('style')
  style.textContent = readFileSync('src/components/task/toolDrawer.css', 'utf8')
  document.head.append(style)
  try {
    const rules = Array.from(style.sheet!.cssRules)
    const declaration = (selector: string) =>
      (rules.find(rule => (rule as CSSStyleRule).selectorText === selector) as CSSStyleRule).style
    expect(declaration('.capture-viewer[open]').position).toBe('fixed')
    expect(declaration('.capture-viewer[open]').getPropertyValue('place-items')).toBe('center')
    for (const selector of ['.capture-viewer::backdrop', '.capture-viewer img']) {
      const animation = declaration(selector).getPropertyValue('animation')
      const duration = Number(/(\d+)ms/.exec(animation)?.[1])
      expect(duration).toBeGreaterThanOrEqual(150)
      expect(duration).toBeLessThanOrEqual(200)
    }
    const reduced = rules.find(rule => (rule as CSSMediaRule).conditionText === '(prefers-reduced-motion: reduce)') as CSSMediaRule
    const override = reduced.cssRules[0] as CSSStyleRule
    expect(override.selectorText).toContain('.capture-viewer::backdrop')
    expect(override.selectorText).toContain('.capture-viewer img')
    expect(override.style.getPropertyValue('animation')).toBe('none')
  } finally { style.remove() }
})
