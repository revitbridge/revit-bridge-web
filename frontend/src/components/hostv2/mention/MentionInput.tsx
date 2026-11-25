import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore, type Ref } from 'react'
import type { FetchCandidates, Mention } from '../../../types/hostv2'
import { MentionController, type MentionState } from './controller'
import { handleKeyDown, handlePaste, selectionOf, syncSelection } from './dom'
import { hasLineBreak, matchingKinds } from './model'
import { browserLanguage } from '../../../utils/language'
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
  disabled = false, placeholder, invalid = [] }: MentionInputProps) {
  const zh = browserLanguage() === 'zh'
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
    list.current?.querySelector('[id="' + id + '-option-' + state.active + '"]')?.scrollIntoView?.({ block: 'nearest' })
  }, [id, state.active])

  const spans = state.spans
  const highlighted = spans.flatMap((span, index) => {
    const prefix = state.text.slice(index === 0 ? 0 : spans[index - 1].end, span.start)
    const isInvalid = invalid.some(item => item.kind === span.mention.kind && item.id === span.mention.id)
    return [prefix, <span key={span.index} className={`mention-tag${isInvalid ? ' is-invalid' : ''}`}
      title={isInvalid ? (zh ? '这个模型项已不可用。' : 'This model item is no longer available.') : undefined}>
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
    <div className={`mention-input${disabled ? ' is-disabled' : ''}`}
      onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) controller.close() }}
      onKeyDown={event => {
        if (event.key === 'Escape' && state.menu && event.target !== input.current) {
          controller.key('Escape')
          input.current?.focus()
        }
      }}>
      <div className="mention-editor">
        <div ref={backdrop} className="mention-backdrop" aria-hidden="true">{highlighted}{'\n'}</div>
        <textarea ref={input} className="mention-textarea" value={state.text}
          role="combobox" aria-label={zh ? '消息' : 'Message'} aria-autocomplete="list" aria-controls={state.menu ? listId : undefined}
          aria-expanded={Boolean(state.menu)} aria-activedescendant={state.menu && hasActive ? `${id}-option-${state.active}` : undefined}
          aria-describedby={`${id}-hint`} placeholder={placeholder ?? (zh ? '输入消息。输入 @ 引用模型项。' : 'Write a message. Type @ to mention a model item.')} disabled={disabled} rows={3}
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
          />
      </div>
      <div className="mention-hint" id={`${id}-hint`}>
        {zh ? '按 Enter 发送，Shift+Enter 换行。输入 @ 选择模型项。' : 'Enter to send. Shift+Enter for a new line. Type @ to select a model item.'}
        {invalidHere && <span className="mention-invalid-note">{zh ? '部分模型项已不可用，请移除或替换标记的标签。' : ' Some model items are unavailable. Remove or replace the marked tags.'}</span>}
        {dropsUnrepresentable && <span className="mention-invalid-note">{zh ? '名称中含换行的模型项无法作为标签插入，请移除或重命名。' : ' Names with line breaks cannot be represented as tags. Remove or rename those model items.'}</span>}
        {dropsMissing && <span className="mention-invalid-note">{zh ? '部分已选模型项在文本中找不到，已移除。' : ' Some selected model items were not found in the text and were removed.'}</span>}
      </div>
      <MentionSuggestions state={state} controller={controller} id={id} listRef={list} focusInput={() => input.current?.focus()} />
    </div>
  )
}

export function MentionSuggestions({ state, controller, id, listRef, focusInput }: {
  state: MentionState; controller: MentionController; id: string; listRef?: Ref<HTMLDivElement>; focusInput?: () => void
}) {
  if (!state.menu) return null
  const kinds = state.menu.mode === 'kinds' ? matchingKinds(state.menu.query) : []
  const items = state.menu.mode === 'items' ? state.candidates?.items ?? [] : []
  const menu = state.menu
  const candidates = state.candidates
  const checking = menu.mode === 'items' && menu.kind === 'element'
  const zh = browserLanguage() === 'zh'
  const autoCategories = menu.mode === 'items' && !menu.category ? candidates?.category_matched ?? [] : []
  const categoryName = (key: string) => candidates?.facets?.find(f => f.category === key)?.label || key
  const filter = menu.mode === 'items' ? menu.category || state.elementCategory : null
  const groups = controller.groups()
  const nameOf = (index: number) => {
    const item = items[index]
    const checked = state.checked.some(m => m.id === Number(item.id)) ||
      state.mentions.some(m => m.kind === 'element' && m.id === Number(item.id))
    return <button key={item.kind + '-' + item.id} type="button" role="option" id={id + '-option-' + index}
      aria-selected={checking ? checked : state.active === index && controller.selectable(item)}
      className={state.active === index ? 'is-active' : undefined}
      aria-disabled={!controller.selectable(item)} disabled={!controller.selectable(item)}
      onClick={() => { focusInput?.(); controller.choose(index) }}>
      <span className="mention-item-label">{checking && <span aria-hidden="true">{checked ? '☑ ' : '☐ '}</span>}
        {item.label || item.name}{checking && item.name && ' #' + item.id}</span>
      <span className="mention-item-detail">{item.summary || item.detail}</span>
      {item.kind !== 'category' && hasLineBreak(item.name ?? '') &&
        <span className="mention-item-detail">{zh ? '名称中含换行，无法作为标签插入。' : 'Names with line breaks cannot be inserted as tags.'}</span>}
    </button>
  }
  return (
    <div className="mention-popup" onMouseDown={event => event.preventDefault()}>
      <div className="mention-popup-heading">
        <button type="button" onClick={() => { focusInput?.(); controller.readSelection() }}>{zh ? '取 Revit 当前选择' : 'Get current Revit selection'}</button>
        {checking && <button type="button" disabled={!state.checked.length} onClick={() => { focusInput?.(); controller.insertChecked() }}>
          {zh ? '插入所选' : 'Insert selected'} ({state.checked.length})</button>}
        {filter && <button type="button" onClick={() => controller.filterCategory('', '')}>
          {menu.mode === 'items' && menu.categoryLabel || categoryName(filter)} ×</button>}
        {!!autoCategories.length && <button type="button" title={zh ? '移除类别关键词' : 'Remove the matching category terms'}
          onClick={() => controller.clearMatched()}>{autoCategories.map(categoryName).join(' / ')} ×</button>}
      </div>
      {menu.mode === 'items' && (menu.kind === 'type' || checking) && !!candidates?.facets?.length &&
        <div className="mention-facets" aria-label={zh ? '按类别筛选' : 'Filter by category'}>
          {candidates.facets.map(f => <button type="button" key={f.category} aria-pressed={filter === f.category}
            onClick={() => controller.filterCategory(filter === f.category ? '' : f.category, f.label)}>
            {f.label} ({f.count})</button>)}
        </div>}
      <div ref={listRef} className="mention-options" id={id + '-candidates'} role="listbox" aria-label={zh ? '可选模型项' : 'Mention candidates'}
        aria-multiselectable={checking || undefined} onScroll={event => {
          const list = event.currentTarget
          if (!state.error && list.scrollTop + list.clientHeight >= list.scrollHeight - 2) controller.loadMore()
        }}>
        {kinds.map((kind, index) => <button key={kind} type="button" role="option" id={id + '-option-' + index}
          aria-selected={state.active === index} onClick={() => { focusInput?.(); controller.choose(index) }}>@{kind}</button>)}
        {groups.map(group => group.category
          ? <div key={group.category} role="group" aria-label={group.label}>
            <div className="mention-group-heading">{group.label} ({group.count})</div>
            {group.indices.map(nameOf)}</div> : group.indices.map(nameOf))}
      </div>
      {(state.status === 'loading' || state.loadingMore) && <div role="status" className="mention-message">{zh ? '正在读取…' : 'Loading candidates...'}</div>}
      {state.error && <div role="alert" className="mention-error">{state.error}</div>}
      {state.status === 'ready' && !groups.length && <div className="mention-message">
        {checking ? (zh ? '当前页没有匹配的选中元素。' : 'No matching selected elements on this page.')
          : (zh ? '没有匹配项。' : 'No matching items. Keep typing to search, or press Enter to send.')}</div>}
      {checking && controller.availableSlots() <= state.checked.length && <div className="mention-message">{zh ? '每条消息最多引用 20 个构件或模型项。' : 'A message can include up to 20 model items.'}</div>}
      {candidates?.warnings?.map((warning, index) => <div role="status" className="mention-message" key={index}>{warning}</div>)}
      {candidates?.facets_partial && <div className="mention-message">{zh ? '类别数量为已读取的部分。' : 'Category counts cover the items read so far.'}</div>}
      {candidates?.truncated && state.nextOffset === null && !candidates.warnings?.length &&
        <div className="mention-message">{zh ? '结果不完整，可缩小搜索范围。' : 'More items may be available. Narrow the search.'}</div>}
      {state.nextOffset !== null && <button className="mention-more" type="button" disabled={state.loadingMore}
        onClick={() => controller.loadMore()}>{zh ? '加载更多' : 'Load more'}</button>}
    </div>
  )
}

export default MentionInput
