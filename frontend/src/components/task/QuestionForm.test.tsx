// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { DesignerForm } from '../../types/api'
import QuestionForm from './QuestionForm'

const levels = [{ value: 1, label: 'L1' }, { value: 2, label: 'L2' }]
const form: DesignerForm = { ask_id: 'form-1', questions: [
  { id: 'base', text: 'Base?', multiple: false, allow_other: false, options: levels },
  { id: 'levels', text: 'Levels?', multiple: true, allow_other: true, options: levels },
] }

const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

it('requires each question and nonblank other text, then submits the displayed choices with their typed values', () => {
  const submit = vi.fn()
  act(() => root.render(<QuestionForm form={form} onAnswer={submit} />))
  const button = container.querySelector('button')!
  const click = (selector: string) => act(() => container.querySelector<HTMLInputElement>(selector)!.click())
  expect(button.disabled).toBe(true)
  click('input[type=radio]')
  click('label:last-of-type input[type=radio]')
  click('input[type=checkbox]')
  expect(button.disabled).toBe(false)
  click('label:last-of-type input[type=checkbox]')
  expect(button.disabled).toBe(true)
  const other = container.querySelector<HTMLInputElement>('input[aria-label]')!
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(other, '  Mezzanine  ')
    other.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(button.disabled).toBe(false)
  click('button')
  expect(submit).toHaveBeenCalledWith(expect.stringContaining('L1、Mezzanine'), {
    ask_id: form.ask_id, items: [
      { id: 'base', ...levels[1] }, { id: 'levels', ...levels[0] },
      { id: 'levels', value: 'Mezzanine', label: 'Mezzanine' },
    ],
  })
})

it.each([['form', '已提交'], ['text', '已用文字回答'], ['replaced', '已被新问题替换'],
  ['invalid', '此表单已失效']] as const)('disables a closed %s form and explains its state', (answered, message) => {
  act(() => root.render(<QuestionForm form={form} answered={answered} onAnswer={vi.fn()} />))
  expect(container.querySelector('[role=status]')?.textContent).toContain(message)
  expect([...container.querySelectorAll('fieldset')].every(field => field.disabled)).toBe(true)
  expect(container.querySelector('button')).toBeNull()
})
