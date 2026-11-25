// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { bridgeApi } from '../../api/bridge'
import type { ToolInfo } from '../../types/api'
import ChatPanel from './ChatPanel'

const wall: ToolInfo = { name: 'create_wall', display_name: 'Create wall', description: 'Place a wall',
  parameters: [{ name: 'height', unit: 'mm', required: true, default: 0 }, { name: 'level' },
    { name: 'suffix', default: '' }], preconditions: [],
  builtin: true, origin: 'builtin', version: '1', used: 0, validator: 'created_ids', not_for: ['Roofs'],
  revit_versions: ['2026'] }
const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
const send = vi.fn()
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  HTMLElement.prototype.scrollTo = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.open = true }
  HTMLDialogElement.prototype.close = function () { this.open = false }
  document.body.append(container)
  root = createRoot(container)
  vi.spyOn(bridgeApi, 'revitHealth').mockResolvedValue({ revit_connected: false } as Awaited<ReturnType<typeof bridgeApi.revitHealth>>)
})
afterEach(() => { act(() => root.unmount()); container.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); send.mockClear() })
async function show(items: ToolInfo[]) {
  vi.spyOn(bridgeApi, 'listTools').mockResolvedValue(items)
  await act(async () => root.render(<ChatPanel title="Task" transcript={{ messages: [], sessionId: null, streaming: false }}
    showTrace onSend={send} />))
  await click('Tools (')
}
async function click(label: string) {
  const button = [...container.querySelectorAll('button')].find(b => b.textContent?.includes(label))!
  await act(async () => button.click())
}
it('shows an empty offline catalog', async () => {
  await show([])
  expect(container.textContent).toContain('No tools available')
  expect(container.textContent).toContain('Disconnected')
})
it('searches offline and puts the example into the real composer without sending', async () => {
  vi.spyOn(bridgeApi, 'getTool').mockResolvedValue({ ...wall, source_query: 'Build a wall on L1', applies_when: [],
    not_for: ['Roofs'], code_template: '', tags: [], execution_count: 0, validator: null, display_name: 'Create wall' })
  await show([wall, { ...wall, name: 'beam', display_name: 'Beam', description: 'Place a beam' }])
  const search = container.querySelector<HTMLInputElement>('input[type=search]')!
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'wall')
    search.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(container.querySelector('dialog')?.textContent).not.toContain('Beam')
  await click('Create wall')
  expect(container.textContent).toContain('Roofs')
  expect(container.textContent).toMatch(/level\s*· Required/)
  expect(container.textContent).toContain('Default ""')
  await click('Build a wall on L1')
  expect(container.querySelector('textarea')?.value).toBe('Build a wall on L1')
  expect(send).not.toHaveBeenCalled()
})
