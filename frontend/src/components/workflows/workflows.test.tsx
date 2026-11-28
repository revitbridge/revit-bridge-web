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
function hintMessages(marker: HTMLButtonElement) {
  expect(marker.type).toBe('button')
  expect(marker.getAttribute('aria-label')).toBe('What this field needs')
  const tooltip = document.getElementById(marker.getAttribute('aria-describedby')!)!
  expect(tooltip.getAttribute('role')).toBe('tooltip')
  expect(marker.contains(tooltip)).toBe(true)
  return Array.from(tooltip.children, line => line.textContent)
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
  expect(hintMessages(box.querySelector('.workflow-steps h3 .field-hint')!)).toEqual(['expected 2-8 steps'])
  expect(box.querySelector('.workflow-validation')?.textContent).toBe('1 item to complete before saving')
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
  const errors = ["step step_1, parameter height: input distance has type string, expected double",
    "step step_2: sill_height must be the designer's words, answer or a preference, not tool"]
  vi.mocked(workflowApi.validate).mockResolvedValueOnce({ errors })
  await validate()
  expect(steps().map(step => hintMessages(step.querySelector('.workflow-step-heading .field-hint')!)))
    .toEqual(errors.map(error => [error]))
  expect(buttons('Save workflow')[0].disabled).toBe(true)
  vi.mocked(workflowApi.validate).mockResolvedValueOnce({ errors: [] })
  select(steps()[0].querySelector('select')!, 'fixed')
  type(steps()[0].querySelector('input[type=number]')!, '3000')
  expect(buttons('Save workflow')[0].disabled).toBe(true)
  await validate()
  expect(buttons('Save workflow')[0].disabled).toBe(false)
})

it('links empty workflow fields to their hints and counts every validation message', async () => {
  vi.mocked(workflowApi.validate).mockResolvedValue({ errors: [
    'name: expected a tool identifier', 'display_name: required, non-empty string',
    'summary: required, non-empty string', 'summary:missing space', 'description: required, non-empty string',
    'steps: expected 2-8 steps',
  ] })
  await mount()
  await validate()

  expect(box.querySelector('.validation-errors')).toBeNull()
  expect(box.querySelectorAll('button.field-hint')).toHaveLength(5)
  const labels = [...box.querySelectorAll<HTMLLabelElement>('.workflow-meta label')]
  for (const [name, messages] of [
    ['Name', ['expected a tool identifier']], ['Display name', ['required, non-empty string']],
    ['Summary', ['required, non-empty string', 'summary:missing space']], ['Description', ['required, non-empty string']],
  ] as const) {
    const label = labels.find(label => label.textContent === name)!
    const marker = label.parentElement!.querySelector<HTMLButtonElement>('button.field-hint')!
    expect(hintMessages(marker)).toEqual(messages)
    expect(label.control?.getAttribute('aria-invalid')).toBe('true')
    expect(label.control?.getAttribute('aria-describedby')).toBe(marker.getAttribute('aria-describedby'))
  }
  expect(hintMessages(box.querySelector('.workflow-steps h3 .field-hint')!)).toEqual(['expected 2-8 steps'])
  const version = labels.find(label => label.textContent === 'Version')!
  expect(version.parentElement!.querySelector('.field-hint')).toBeNull()
  expect(version.control!.hasAttribute('aria-invalid')).toBe(false)
  expect(version.control!.hasAttribute('aria-describedby')).toBe(false)
  expect(box.querySelector('.workflow-validation')?.textContent).toBe('6 items to complete before saving')
})

it('lists unscoped messages in the summary alongside the count of all messages', async () => {
  const unscoped = 'inputs: two inputs have the same name'
  vi.mocked(workflowApi.validate).mockResolvedValue({ errors: ['version: expected a version number', unscoped] })
  await mount()
  await validate()

  const summary = box.querySelector('.workflow-validation')!
  expect(summary.querySelector('p')?.textContent).toBe('2 items to complete before saving')
  expect(Array.from(summary.querySelectorAll('li'), item => item.textContent)).toEqual([unscoped])
  expect(hintMessages(box.querySelector('.workflow-meta .field-hint')!)).toEqual(['expected a version number'])
})

function expectHintOpen(marker: HTMLButtonElement, open: boolean) {
  const tooltip = document.getElementById(marker.getAttribute('aria-describedby')!)!
  expect(marker.getAttribute('aria-expanded')).toBe(String(open))
  expect(tooltip.hasAttribute('data-open')).toBe(open)
}

it('toggles one hint at a time and dismisses it with Escape or an outside click', async () => {
  vi.mocked(workflowApi.validate).mockResolvedValue({ errors: ['name: required', 'version: invalid'] })
  await mount()
  await validate()
  const [name, version] = box.querySelectorAll<HTMLButtonElement>('.workflow-meta .field-hint')
  const nameInput = box.querySelector<HTMLInputElement>('.workflow-meta input')!
  const versionTip = version.querySelector<HTMLElement>('[role=tooltip]')!
  expect(versionTip.classList.contains('field-hint-align-end')).toBe(true)
  expectHintOpen(name, false)
  click(name)
  expectHintOpen(name, true)
  expect(name.closest('label')).toBeNull()
  expect(document.activeElement).not.toBe(nameInput)
  click(name)
  expectHintOpen(name, false)

  click(name)
  click(version)
  expectHintOpen(name, false)
  expectHintOpen(version, true)
  act(() => versionTip.querySelector('span')!.click())
  expectHintOpen(version, true)
  act(() => {
    version.focus()
    version.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  })
  expectHintOpen(version, false)
  expect(document.activeElement).toBe(version)

  click(version)
  act(() => box.querySelector('h2')!.click())
  expectHintOpen(version, false)
})

it('previews hints on hover and keyboard focus and lets Escape dismiss either preview', async () => {
  await mount()
  await validate()
  const marker = box.querySelector<HTMLButtonElement>('.workflow-steps h3 .field-hint')!
  const tooltip = marker.querySelector('[role=tooltip]')!
  const pointer = (target: Element, type: string, relatedTarget: EventTarget | null = null) => act(() => {
    target.dispatchEvent(new MouseEvent(type, { bubbles: true, relatedTarget }))
  })
  const escape = () => act(() => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  })
  pointer(marker, 'pointerover')
  expectHintOpen(marker, true)
  pointer(marker, 'pointerout', tooltip)
  expectHintOpen(marker, true)
  pointer(tooltip, 'pointerout', box)
  expectHintOpen(marker, false)
  pointer(marker, 'pointerover')
  escape()
  expectHintOpen(marker, false)

  act(() => marker.focus())
  expectHintOpen(marker, true)
  escape()
  expectHintOpen(marker, false)
  expect(document.activeElement).toBe(marker)
})

it('keeps a previously open hint closed if its messages return after an edit', async () => {
  await mount()
  await validate()
  const marker = () => box.querySelector<HTMLButtonElement>('.workflow-steps h3 .field-hint')
  click(marker()!)
  expectHintOpen(marker()!, true)
  type(box.querySelector('.workflow-meta input')!, 'updated')
  expect(marker()).toBeNull()
  await validate()
  expectHintOpen(marker()!, false)
})

it('attaches quoted and unquoted host messages only to an exact step ID', async () => {
  const matched = [
    "step 'step_1', parameter 'height': required", 'step "step_1": bad', 'step step_1: bad',
    "step 'step_1': bad", 'step "step_1", parameter "height": required',
  ]
  const unknown = 'step step_10: bad'
  vi.mocked(workflowApi.validate).mockResolvedValue({ errors: [...matched, unknown] })
  await mount()
  click(buttons('Add')[0])
  await validate()
  expect(hintMessages(steps()[0].querySelector('.field-hint')!)).toEqual(matched)
  expect(Array.from(box.querySelectorAll('.workflow-validation li'), li => li.textContent)).toEqual([unknown])
})
