// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { bridgeApi } from '../../api/bridge'
import { workflowApi } from '../../api/workflows'
import * as config from '../../config'
import type { ToolInfo } from '../../types/api'
import WorkflowsPage from './WorkflowsPage'

const pack = (name: string): ToolInfo => ({
  name, display_name: name, version: '1.0.0', description: '', builtin: true, origin: 'builtin',
  parameters: [{ name: 'height', type: 'double', required: true, unit: 'mm' }], used: 0,
  preconditions: [], validator: null,
})
const box = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  document.body.append(box); root = createRoot(box)
  vi.spyOn(bridgeApi, 'listTools').mockResolvedValue([pack('walls'), pack('windows')])
  vi.spyOn(workflowApi, 'list').mockResolvedValue([])
  vi.spyOn(workflowApi, 'resultPaths').mockResolvedValue(['Items.0.Id'])
  vi.spyOn(workflowApi, 'validate').mockResolvedValue({ errors: ['steps: expected 2-8 steps'] })
})
afterEach(() => {
  act(() => root.unmount()); box.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers()
})
const mount = () => act(async () => root.render(<WorkflowsPage />))
const steps = () => [...box.querySelectorAll<HTMLElement>('.workflow-steps ol > li')]
const buttons = (text: string) => [...box.querySelectorAll('button')].filter(b => b.textContent === text)
const click = (b: HTMLButtonElement) => act(() => b.click())
const select = (el: HTMLSelectElement, value: string) => act(() => {
  el.value = value; el.dispatchEvent(new Event('change', { bubbles: true }))
})
const type = (el: HTMLInputElement, value: string) => act(() => {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(el, value)
  el.dispatchEvent(new Event('input', { bubbles: true }))
})
const validate = async () => {
  await act(async () => vi.advanceTimersByTimeAsync(210))
  return vi.mocked(workflowApi.validate).mock.lastCall![0]
}

it('adds searched packs, moves steps from the keyboard and removes them', async () => {
  await mount()
  const search = box.querySelector<HTMLInputElement>('[type=search]')!
  type(search, 'walls'); click(buttons('Add')[0])
  type(search, 'windows'); click(buttons('Add')[0])
  const handle = steps()[0].querySelector<HTMLElement>('[draggable]')!
  handle.focus()
  act(() => { handle.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, bubbles: true })) })
  expect((await validate()).steps.map(s => s.tool)).toEqual(['windows', 'walls'])
  click(buttons('Move up')[1])
  expect((await validate()).steps.map(s => s.tool)).toEqual(['walls', 'windows'])
  click(buttons('Remove')[0])
  expect((await validate()).steps.map(s => s.tool)).toEqual(['windows'])
  expect(box.textContent).toContain('steps: expected 2-8 steps')
})

it('reuses named inputs, sets typed literals and validates a custom path into an earlier result', async () => {
  await mount()
  click(buttons('Add')[0]); click(buttons('Add')[1])
  let draft = await validate()
  expect(draft.inputs.map(i => i.name)).toEqual(['height'])
  expect(draft.steps.map(s => s.params.height)).toEqual([{ input: 'height' }, { input: 'height' }])
  select(steps()[0].querySelector('select')!, 'fixed')
  type(steps()[0].querySelector('input[type=number]')!, '125')
  // Clicking the already active segment must preserve the edited literal.
  click(buttons('Fixed value')[0])
  expect(steps()[0].querySelector<HTMLInputElement>('input[type=number]')!.value).toBe('125')
  select(steps()[1].querySelector('select')!, 'from')
  await act(async () => {})
  expect(workflowApi.resultPaths).toHaveBeenCalledWith('walls')
  expect(steps()[1].querySelector('datalist option')?.getAttribute('value')).toBe('Items.0.Id')
  type(steps()[1].querySelector('input[list]')!, 'unknown.0.leaf')
  draft = await validate()
  expect(draft.steps.map(s => s.params.height)).toEqual([125, { from: draft.steps[0].id, path: 'unknown.0.leaf' }])
  expect(draft.inputs).toEqual([])
})

it('drags packs into the step list and reorders by stable step id', async () => {
  await mount()
  const drag = (source: Element, target: Element) => act(() => {
    const values = new Map<string, string>()
    const dataTransfer = { setData: (key: string, value: string) => values.set(key, value),
      getData: (key: string) => values.get(key) ?? '' }
    for (const [element, type] of [[source, 'dragstart'], [target, 'drop']] as const) {
      element.dispatchEvent(Object.assign(new Event(type, { bubbles: true, cancelable: true }), { dataTransfer }))
    }
  })
  const packs = box.querySelectorAll('.workflow-packs li')
  const column = box.querySelector('.workflow-steps')!
  drag(packs[0], column); drag(packs[1], column)
  const before = (await validate()).steps
  drag(steps()[1].querySelector('[draggable]')!, steps()[0])
  const after = (await validate()).steps
  expect(after).toEqual([before[1], before[0]])
})

it('shows host binding errors and keeps Save disabled until the edited draft passes', async () => {
  const runtime = config.getConfig()
  vi.spyOn(config, 'getConfig').mockReturnValue({ ...runtime, features: { ...runtime.features, mode: 'local' } })
  await mount()
  click(buttons('Add')[0]); click(buttons('Add')[1])
  const errors = ["step wall, parameter height: input distance has type string, expected double",
    "step window: sill_height must be the designer's words, answer or a preference, not tool"]
  vi.mocked(workflowApi.validate).mockResolvedValueOnce({ errors })
  await validate()
  for (const error of errors) expect(box.textContent).toContain(error)
  expect(buttons('Save workflow')[0].disabled).toBe(true)
  vi.mocked(workflowApi.validate).mockResolvedValueOnce({ errors: [] })
  select(steps()[0].querySelector('select')!, 'fixed')
  type(steps()[0].querySelector('input[type=number]')!, '3000')
  expect(buttons('Save workflow')[0].disabled).toBe(true)
  await validate()
  expect(buttons('Save workflow')[0].disabled).toBe(false)
})
