import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore, type Ref } from 'react'
import type { FetchCandidates, Mention } from '../../../types/hostv2'
import { MentionController, type MentionState } from './controller'
import { handleKeyDown, handlePaste, selectionOf, syncSelection } from './dom'
import { hasLineBreak, matchingKinds } from './model'
import './mention.css'

export interface MentionInputProps {
  value: string
  mentions: Mention[]
  // Every edit, and once when the given mentions do not match the given text
  // (missing from it or out of text order): the component's text and mentions.
  onChange: (text: string, mentions: Mention[]) => void
  // Enter with the picker closed (or with nothing to pick) and non-blank text.
  // The arguments are what the component holds; mentions are in text order.
  onSubmit: (text: string, mentions: Mention[]) => void
  // Called only from the picker, debounced 200 ms; late answers are dropped.
  // Its identity may change between renders.
  fetchCandidates: FetchCandidates
  disabled?: boolean
  placeholder?: string
  // Tags with one of these kind + id pairs are shown as no longer available.
  invalid?: Mention[]
}

export function MentionInput({ value, mentions, onChange, onSubmit, fetchCandidates,
  disabled = false, placeholder = 'Write a message. Type @ to mention a model item.', invalid = [] }: MentionInputProps) {
  const [controller] = useState(() => new MentionController(value, mentions,
    { onChange, onSubmit, fetchCandidates }, disabled))
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot)
  const input = useRef<HTMLTextAreaElement>(null)
  const backdrop = useRef<HTMLDivElement>(null)
  const list = useRef<HTMLDivElement>(null)
  const id = useId()
  const listId = `${id}-candidates`

  useEffect(() => {
    controller.configure({ onChange, onSubmit, fetchCandidates })
    controller.syncExternal(value, mentions, disabled)
  }, [controller, value, mentions, disabled, onChange, onSubmit, fetchCandidates, state.text, state.mentions, state.composing])
  useEffect(() => () => controller.destroy(), [controller])
  useLayoutEffect(() => {
    const element = input.current
    if (!element || state.composing || element.ownerDocument.activeElement !== element) return
    element.setSelectionRange(state.selection.start, state.selection.end, state.selection.direction)
  }, [state.selection, state.composing])
  useEffect(() => {
    list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [state.active, state.candidates])

  const spans = state.spans
  const highlighted = spans.flatMap((span, index) => {
    const prefix = state.text.slice(index === 0 ? 0 : spans[index - 1].end, span.start)
    const isInvalid = invalid.some(item => item.kind === span.mention.kind && item.id === span.mention.id)
    return [prefix, <span key={span.index} className={`mention-tag${isInvalid ? ' is-invalid' : ''}`}
      title={isInvalid ? 'This model item is no longer available.' : undefined}>
      {state.text.slice(span.start, span.end)}
    </span>]
  })
  highlighted.push(state.text.slice(spans.at(-1)?.end ?? 0))
  const kinds = state.menu?.mode === 'kinds' ? matchingKinds(state.menu.query) : []
  const items = state.menu?.mode === 'items' ? state.candidates?.items ?? [] : []
  const hasActive = state.menu?.mode === 'kinds' ? kinds.length > 0
    : items[state.active] && controller.selectable(items[state.active])

  // The note speaks of the tags in the text only: a pick found gone earlier and not here says nothing.
  const invalidHere = spans.some(span => invalid.some(item => item.kind === span.mention.kind && item.id === span.mention.id))
  const dropsUnrepresentable = state.dropped.some(mention => hasLineBreak(mention.name))
  const dropsMissing = state.dropped.some(mention => !hasLineBreak(mention.name))

  return (
    <div className={`mention-input${disabled ? ' is-disabled' : ''}`}>
      <div className="mention-editor">
        <div ref={backdrop} className="mention-backdrop" aria-hidden="true">{highlighted}{'\n'}</div>
        <textarea ref={input} className="mention-textarea" value={state.text}
          role="combobox" aria-label="Message" aria-autocomplete="list" aria-controls={state.menu ? listId : undefined}
          aria-expanded={Boolean(state.menu)} aria-activedescendant={state.menu && hasActive ? `${id}-option-${state.active}` : undefined}
          aria-describedby={`${id}-hint`} placeholder={placeholder} disabled={disabled} rows={3}
          onChange={event => controller.editNative(event.currentTarget.value, selectionOf(event.currentTarget))}
          onSelect={event => syncSelection(controller, event.currentTarget)}
          onKeyDown={event => handleKeyDown(controller, event)}
          onPaste={event => handlePaste(controller, event)}
          onCut={event => {
            if (state.composing || disabled) return
            syncSelection(controller, event.currentTarget)
            const selection = controller.getSnapshot().selection
            if (selection.start === selection.end) return
            event.clipboardData.setData('text/plain', state.text.slice(selection.start, selection.end))
            controller.replaceSelection('')
            event.preventDefault()
          }}
          onCompositionStart={() => controller.compositionStart()}
          onCompositionEnd={event => controller.compositionEnd(event.currentTarget.value, selectionOf(event.currentTarget))}
          onScroll={event => {
            if (backdrop.current) {
              backdrop.current.scrollTop = event.currentTarget.scrollTop
              backdrop.current.scrollLeft = event.currentTarget.scrollLeft
            }
          }}
          onBlur={() => controller.close()} />
      </div>
      <div className="mention-hint" id={`${id}-hint`}>
        Enter to send. Shift+Enter for a new line. Type @ to select a model item.
        {invalidHere && <span className="mention-invalid-note"> Some model items are unavailable. Remove or replace the marked tags.</span>}
        {dropsUnrepresentable && <span className="mention-invalid-note"> Names with line breaks cannot be represented as tags. Remove or rename those model items.</span>}
        {dropsMissing && <span className="mention-invalid-note"> Some selected model items were not found in the text and were removed.</span>}
      </div>
      <MentionSuggestions state={state} controller={controller} id={id} listRef={list} />
    </div>
  )
}

export function MentionSuggestions({ state, controller, id, listRef }: {
  state: MentionState; controller: MentionController; id: string; listRef?: Ref<HTMLDivElement>
}) {
  if (!state.menu) return null
  const kinds = state.menu.mode === 'kinds' ? matchingKinds(state.menu.query) : []
  const items = state.menu.mode === 'items' ? state.candidates?.items ?? [] : []
  return (
    <div className="mention-popup" onMouseDown={event => event.preventDefault()}>
      {state.menu.mode === 'items' && (
        <div className="mention-popup-heading">
          <span>{state.menu.kind === 'type' && !state.menu.category ? 'Choose a type category' : `Choose a ${state.menu.kind}`}</span>
          {state.menu.category && <button type="button" onClick={() => controller.backToCategories()}>Categories / {state.menu.categoryLabel}</button>}
        </div>
      )}
      <div ref={listRef} className="mention-options" id={`${id}-candidates`} role="listbox" aria-label="Mention candidates">
        {kinds.map((kind, index) => (
          <button key={kind} type="button" role="option" id={`${id}-option-${index}`}
            aria-selected={state.active === index} onClick={() => controller.choose(index)}>@{kind}</button>
        ))}
        {items.map((item, index) => (
          <button key={`${item.kind}-${item.id}-${index}`} type="button" role="option" id={`${id}-option-${index}`}
            aria-selected={state.active === index && controller.selectable(item)} aria-disabled={!controller.selectable(item)}
            disabled={!controller.selectable(item)} onClick={() => controller.choose(index)}>
            <span className="mention-item-label">{item.label || item.name}</span>
            <span className="mention-item-detail">{item.detail}</span>
            {item.kind !== 'category' && hasLineBreak(item.name) && <span className="mention-item-detail">Names with line breaks cannot be inserted as tags.</span>}
          </button>
        ))}
      </div>
      {state.status === 'loading' && <div role="status" className="mention-message">Loading candidates...</div>}
      {state.status === 'error' && <div role="alert" className="mention-error">{state.error}</div>}
      {state.status === 'ready' && items.length === 0 &&
        <div className="mention-message">No matching items. Keep typing to search, or press Enter to send.</div>}
      {state.candidates?.truncated && <div className="mention-message">More items are available. Keep typing to narrow the search.</div>}
    </div>
  )
}

export default MentionInput
