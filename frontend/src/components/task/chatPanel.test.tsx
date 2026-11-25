/* ChatPanel with the mention input: what it hands the input, what it sends, and how a sent
   message shows its picks. The input itself is tested in components/hostv2/mention; here it
   is replaced by a stub that keeps the props it was given (no browser needed). */

import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage } from '../../types/api'
import type { Mention } from '../../types/hostv2'
import type { MentionInputProps } from '../hostv2/mention'

const seen = vi.hoisted(() => ({ props: null as MentionInputProps | null }))
vi.mock('../hostv2/mention', () => ({
  MentionInput: (props: MentionInputProps) => {
    seen.props = props
    return null
  },
}))

import ChatPanel from './ChatPanel'
import { fetchCandidates } from './mentions'
import type { Transcript } from './flow'

const f2: Mention = { kind: 'level', id: 312, name: 'F2' }
const mep: Mention = { kind: 'link', id: 502, name: 'MEP.rvt' }
const wall: Mention = { kind: 'type', id: 401, name: 'Generic - 200mm' }

function render(transcript: Transcript, props: Partial<Parameters<typeof ChatPanel>[0]> = {}) {
  seen.props = null
  const html = renderToStaticMarkup(<ChatPanel title="With the bridge" transcript={transcript} {...props} />)
  return { html, input: seen.props as MentionInputProps | null }
}

const empty: Transcript = { messages: [], sessionId: null, streaming: false }

beforeEach(() => vi.stubGlobal('navigator', { languages: ['en-US'] }))
afterEach(() => vi.unstubAllGlobals())

describe('ChatPanel and the mention input', () => {
  it('shows the reader and its expandable result without a card', () => {
    const { html } = render({ ...empty, messages: [{ role: 'assistant', content: '', trace: [{
      type: 'tool', role: 'planner', id: '1', name: 'propose_spec', args: {}, status: 'ok',
      read: { name: 'get_element_geometry', result: { midpoint: 137 } },
    }] }] })
    expect(html).toContain('<summary>Read: get_element_geometry</summary>')
    expect(html).toContain('137')
  })
  it('the input is the mention input, fed by the host, off while a turn streams', () => {
    const { input } = render(empty, { onSend: vi.fn(), placeholder: 'e.g. a wall' })
    expect(input).toMatchObject({ value: '', mentions: [], disabled: undefined, placeholder: 'e.g. a wall', invalid: [] })
    expect(input?.fetchCandidates).toBe(fetchCandidates)
    expect(render({ ...empty, streaming: true }, { onSend: vi.fn(), disabled: true }).input?.disabled).toBe(true)
    const own = vi.fn()
    expect(render(empty, { onSend: vi.fn(), fetchCandidates: own }).input?.fetchCandidates).toBe(own)
    // The no-bridge mirror has no input at all.
    expect(render(empty).input).toBeNull()
  })

  it('Enter in the input sends the text and the picks in it', () => {
    const onSend = vi.fn()
    const { input } = render(empty, { onSend })
    input?.onSubmit('  a wall on @level:F2  ', [f2])
    expect(onSend).toHaveBeenCalledWith('a wall on @level:F2', [f2])
    input?.onSubmit('   ', [])
    expect(onSend).toHaveBeenCalledTimes(1)
    // Disabled (a turn streaming): nothing goes out.
    const off = vi.fn()
    render(empty, { onSend: off, disabled: true }).input?.onSubmit('x', [])
    expect(off).not.toHaveBeenCalled()
  })

  it('picks found gone earlier are marked only while they are in the input: an empty box marks none', () => {
    const messages: ChatMessage[] = [
      { role: 'user', content: 'on @level:F2 in @link:MEP.rvt as @type:Generic - 200mm', mentions: [
        { ...f2, status: 'resolved' }, { ...mep, status: 'not_found', reason: 'not_loaded' },
        { ...wall, status: 'invalid', reason: 'invalid' }] },
      { role: 'assistant', content: 'MEP.rvt is not loaded.' },
    ]
    // The box starts empty (what is in it is tested with unavailableInInput in mentions.test.ts).
    expect(render({ ...empty, messages }, { onSend: vi.fn() }).input?.invalid).toEqual([])
  })

  it('each item is sent once, and a message with more than 20 different items is not sent', () => {
    const onSend = vi.fn()
    const { input } = render(empty, { onSend })
    input?.onSubmit('@level:F2 @level:F2 @link:MEP.rvt', [f2, f2, mep])
    expect(onSend).toHaveBeenCalledWith('@level:F2 @level:F2 @link:MEP.rvt', [f2, mep])
    const many = Array.from({ length: 21 }, (_, i): Mention => ({ kind: 'level', id: 1000 + i, name: `L${i}` }))
    input?.onSubmit('too many', many)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('a sent message shows its picks with what the host found', () => {
    const messages: ChatMessage[] = [
      { role: 'user', content: 'on @level:F2 in @link:MEP.rvt', mentions: [
        { ...f2, status: 'resolved' }, { ...mep, status: 'not_found', reason: 'not_loaded' }] },
      { role: 'user', content: 'again @level:F2', mentions: [{ ...f2, status: 'unresolved', reason: 'revit_unreachable' }] },
      { role: 'user', content: 'plain words' },
    ]
    const { html } = render({ ...empty, messages }, { onSend: vi.fn() })
    expect(html).toContain('<span class="chat-mention is-resolved" title="checked against the model">@level:F2'
      + '<span class="chat-mention-note">checked against the model</span></span>')
    expect(html).toContain('<span class="chat-mention is-not_found" title="not used: not loaded">@link:MEP.rvt')
    expect(html).toContain('<span class="chat-mention is-unresolved" title="not checked: revit unreachable">@level:F2')
    expect(html.match(/class="chat-mentions"/g)).toHaveLength(2)      // none under a message without picks
  })

  it('a used-up output limit shows as the model error notice in the reply, with its whole text', () => {
    const notice = 'Output limit used up: the analyst (small-model) spent its whole output limit (max_tokens 16384) ' +
      'before it wrote a reply or called a tool - most likely on thinking, which counts toward the limit.'
    const transcript: Transcript = { ...empty, sessionId: 's1', messages: [
      { role: 'user', content: 'a wall' }, { role: 'assistant', content: `**Model error:** ${notice}` }] }
    const { html } = render(transcript, { onSend: vi.fn() })
    expect(html).toMatch(/<div class="chat-bubble assistant"><div class="markdown-body">.*<strong>Model error:<\/strong> Output limit used up: the analyst \(small-model\)/s)
    expect(html).toContain('(max_tokens 16384)')
  })
})
