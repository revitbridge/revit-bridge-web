// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as client from '../../api/client'
import type { ChatMessage } from '../../types/api'
import ChatPanel from './ChatPanel'

const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
const onSend = vi.fn()
const picked = { kind: 'element', id: 7, name: 'Door', document_id: 'doc' }
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  HTMLElement.prototype.scrollTo = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
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
it('inserts document-scoped element tags into the composer and waits for Send', async () => {
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValue({ items: [picked] })
  render()
  await click('Select in Revit')
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toMatchObject({ session_id: 'session', mode: 'single' })
  expect(container.querySelector('textarea')?.value.trim()).toBe('@element:Door#7')
  expect(container.querySelector('.mention-tag')?.textContent).toBe('@element:Door#7')
  expect(onSend).not.toHaveBeenCalled()
  await click('Send')
  expect(onSend).toHaveBeenCalledWith('@element:Door#7', [picked])
})
it('waits for a selection-question click, offers the old picker on unsupported add-ins, and answers that form', async () => {
  const select = vi.fn()
  const fetch = vi.spyOn(client, 'apiFetch').mockResolvedValueOnce({ error: 'unsupported_addin' })
    .mockResolvedValueOnce({ items: [picked] })
  render([{ role: 'assistant', content: '', form: { ask_id: 'q', selection: { mode: 'box', prompt: 'Choose a door' },
    questions: [] } }], { onSelectionAnswer: select })
  expect(fetch).not.toHaveBeenCalled()
  await click('Select in Revit')
  expect(container.textContent).toContain('Upgrade the Revit add-in')
  await click('Take current selection')
  expect(fetch.mock.calls[1][0]).toContain('mentions?kind=element&source=selection')
  expect(select).toHaveBeenCalledWith('q', [picked])
  expect(onSend).not.toHaveBeenCalled()
})
it.each(['Cancel', 'pagehide'])('aborts the active watch on %s', async end => {
  const fetch = vi.spyOn(client, 'apiFetch').mockImplementation((_url, init) => init?.method === 'DELETE'
    ? Promise.resolve({ cancelled: true }) : new Promise((_resolve, reject) => init?.signal?.addEventListener('abort', reject)))
  render()
  await click('Select in Revit')
  expect(container.textContent).toContain('Click one new element')
  if (end === 'Cancel') await click(end)
  else await act(async () => { window.dispatchEvent(new Event('pagehide')) })
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true)
  expect(fetch.mock.calls[1][1]?.method).toBe('DELETE')
  expect(onSend).not.toHaveBeenCalled()
})
it('opens the captured view and starts with attachment off', async () => {
  const attach = vi.fn()
  render([{ role: 'assistant', content: '', images: [{ width: 1, height: 1, view_name: 'Floor plan',
    png_base64: 'aW1hZ2U=', capture_id: 'handle' }] }], { onAttach: attach })
  expect(container.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,aW1hZ2U=')
  const toggle = container.querySelector<HTMLInputElement>('[role=switch]')!
  expect(toggle.checked).toBe(false)
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="Enlarge view"]')!.click())
  expect(container.querySelector('dialog')?.open).toBe(true)
  act(() => toggle.click())
  expect(attach).toHaveBeenCalledWith('handle')
})
