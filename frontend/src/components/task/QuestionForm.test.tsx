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
  vi.stubGlobal('navigator', { languages: ['zh-TW', 'en'] })
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

it.each(['Mezzanine', '"east"'])('requires each question and preserves unmatched text %s', freeText => {
  const submit = vi.fn()
  const choices = { ...form, questions: [form.questions[0], {
    ...form.questions[1], options: [...levels, { value: 'east', label: 'East level' }],
  }] }
  act(() => root.render(<QuestionForm form={choices} onAnswer={submit} />))
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
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(other, '  ' + freeText + '  ')
    other.dispatchEvent(new Event('input', { bubbles: true }))
  })
  expect(button.disabled).toBe(false)
  click('button')
  expect(submit).toHaveBeenCalledWith(expect.stringContaining('L1、' + freeText), {
    ask_id: form.ask_id, items: [
      { id: 'base', ...levels[1] }, { id: 'levels', ...levels[0] },
      { id: 'levels', value: freeText, label: freeText },
    ],
  })
})

it.each([['form', '已提交', 'Submitted'], ['text', '已用文字回答', 'Answered in chat'],
  ['replaced', '已被新问题替换', 'Replaced by a new question'], ['invalid', '此表单已失效', 'This form has expired'],
  ['execution', '已收到执行回报', 'Closed when the execution result was reported']] as const)(
  'disables a closed %s form and explains its state in either language', (answered, message, english) => {
  act(() => root.render(<QuestionForm form={form} answered={answered} onAnswer={vi.fn()} />))
  expect(container.querySelector('[role=status]')?.textContent).toContain(message)
  expect([...container.querySelectorAll('fieldset')].every(field => field.disabled)).toBe(true)
  expect(container.querySelector('button')).toBeNull()
  vi.stubGlobal('navigator', { languages: ['fr', 'en-US', 'zh'] })
  act(() => root.render(<QuestionForm form={form} answered={answered} onAnswer={vi.fn()} />))
  expect(container.querySelector('[role=status]')?.textContent).toContain(english)
})

it.each([['en-US', 'Other', 'Continue', 'L1'], ['zh-CN', '其他', '继续', '1']])(
  'uses %s form controls and submits a typed option only once', (language, other, button, typed) => {
    vi.stubGlobal('navigator', { languages: [language] })
    const submit = vi.fn()
    act(() => root.render(<QuestionForm form={form} onAnswer={submit} />))
    expect(container.querySelector('button')?.textContent).toBe(button)
    expect(container.querySelector('label:last-of-type input[type=checkbox]')?.parentElement?.textContent).toBe(other)
    for (const selector of ['input[type=radio]', 'input[type=checkbox]', 'label:last-of-type input[type=checkbox]']) {
      act(() => container.querySelector<HTMLInputElement>(selector)!.click())
    }
    const text = container.querySelector<HTMLInputElement>('input[aria-label]')!
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(text, typed)
      text.dispatchEvent(new Event('input', { bubbles: true }))
    })
    act(() => container.querySelector('button')!.click())
    expect(submit).toHaveBeenCalledWith(expect.any(String), {
      ask_id: form.ask_id, items: [{ id: 'base', ...levels[0] }, { id: 'levels', ...levels[0] }],
    })
  })
