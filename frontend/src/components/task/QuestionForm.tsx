import { useState } from 'react'
import type { ChatMessage, DesignerAnswers, DesignerForm } from '../../types/api'

interface Props {
  form: DesignerForm
  answered?: ChatMessage['answered']
  disabled?: boolean
  onAnswer: (message: string, answers: DesignerAnswers) => void
}

const formText = {
  en: { other: 'Other', submit: 'Continue', form: 'Submitted', text: 'Answered in chat',
    replaced: 'Replaced by a new question', invalid: 'This form has expired. Reply in the chat.',
    execution: 'Closed when the execution result was reported.' },
  zh: { other: '其他', submit: '继续', form: '已提交', text: '已用文字回答', replaced: '已被新问题替换',
    invalid: '此表单已失效，请在对话框中回复。', execution: '已收到执行回报，表单已关闭。' },
}

function typedChoice(q: DesignerForm['questions'][number], text: string) {
  const choice = q.options.find(o => o.value === text) ?? q.options.find(o => o.label === text)
  if (choice) return choice
  try {
    const scalar: unknown = JSON.parse(text)
    const typed = q.options.find(o => o.value === scalar)
    if (typed) return typed
  } catch { /* A free-text answer need not be a JSON scalar. */ }
  return { label: text, value: text }
}

export default function QuestionForm({ form, answered, disabled, onAnswer }: Props) {
  const languages = typeof navigator === 'undefined' ? [] : navigator.languages?.length
    ? navigator.languages : [navigator.language]
  const language = languages.map(tag => tag.toLowerCase().split('-')[0]).find(tag => tag === 'en' || tag === 'zh')
  const labels = formText[language === 'zh' ? 'zh' : 'en']
  const [picked, setPicked] = useState(new Map<string, number[]>())
  const [other, setOther] = useState(new Map<string, string>())
  const answers: DesignerAnswers = { ask_id: form.ask_id, items: [] }
  const lines: string[] = []
  let complete = true
  for (const [index, q] of form.questions.entries()) {
    const selected = picked.get(q.id) ?? []
    if (!selected.length || (selected.includes(-1) && !other.get(q.id)?.trim())) complete = false
    const choices = selected.map(i => i === -1 ? typedChoice(q, other.get(q.id)?.trim() ?? '') : q.options[i])
    const options = choices.filter((o, i) => choices.findIndex(choice => choice.value === o.value) === i)
    answers.items.push(...options.map(o => ({ ...o, id: q.id })))
    const separator = language === 'zh' ? '：' : ': '
    lines.push(`${index + 1}. ${q.text}${separator}${options.map(o => o.label).join(language === 'zh' ? '、' : ', ')}`)
  }

  function toggle(q: DesignerForm['questions'][number], index: number, checked: boolean) {
    setPicked(previous => new Map(previous).set(q.id, q.multiple
      ? [...(previous.get(q.id) ?? []).filter(i => i !== index), ...(checked ? [index] : [])] : [index]))
  }

  return <form className="inline-form" onSubmit={e => {
    e.preventDefault()
    if (complete && !disabled && !answered) onAnswer(lines.join('\n').slice(0, 8000), answers)
  }}>
    {form.questions.map((q, i) => <fieldset key={q.id} disabled={!!answered || disabled}>
      <legend>{i + 1}. {q.text}</legend>
      {[...q.options.map((o, index) => ({ label: o.label, index })),
        ...(q.allow_other ? [{ label: labels.other, index: -1 }] : [])].map(o => (
        <label key={o.index}>
          <input type={q.multiple ? 'checkbox' : 'radio'} name={form.ask_id + '/' + q.id}
            checked={(picked.get(q.id) ?? []).includes(o.index)} onChange={e => toggle(q, o.index, e.target.checked)} />
          {o.label}
        </label>
      ))}
      {picked.get(q.id)?.includes(-1) && <input aria-label={`${q.text} (${labels.other})`} maxLength={500}
        value={other.get(q.id) ?? ''} onChange={e => setOther(new Map(other).set(q.id, e.target.value))} />}
    </fieldset>)}
    {answered ? <p role="status">{labels[answered]}</p>
      : <button className="btn-primary" disabled={!complete || disabled}>{labels.submit}</button>}
  </form>
}
