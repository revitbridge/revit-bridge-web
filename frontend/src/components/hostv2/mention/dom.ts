import type { MentionController } from './controller'
import type { TextSelection } from './model'

// The parts of a textarea and of its key and clipboard events the component
// reads. React's events satisfy these; the tests pass plain objects, so the
// IME and paste paths are covered without a browser.
export interface TextareaLike {
  selectionStart: number
  selectionEnd: number
  selectionDirection: TextSelection['direction']
  setSelectionRange(start: number, end: number, direction?: TextSelection['direction']): void
}
export interface KeyDownLike {
  key: string
  keyCode: number
  shiftKey: boolean
  ctrlKey: boolean
  metaKey: boolean
  altKey: boolean
  nativeEvent: { isComposing?: boolean }
  currentTarget: TextareaLike
  preventDefault(): void
}
export interface PasteLike {
  clipboardData: { getData(format: string): string }
  currentTarget: TextareaLike
  preventDefault(): void
}

export const selectionOf = (input: TextareaLike): TextSelection => ({
  start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection,
})

export function syncSelection(controller: MentionController, element: TextareaLike) {
  controller.select(selectionOf(element))
  const { selection, composing } = controller.getSnapshot()
  if (!composing && (element.selectionStart !== selection.start || element.selectionEnd !== selection.end))
    element.setSelectionRange(selection.start, selection.end, selection.direction)
}

// isComposing covers Chrome and Firefox; keyCode 229 covers Safari, whose
// Enter that confirms a composition arrives after compositionend.
export function handleKeyDown(controller: MentionController, event: KeyDownLike) {
  const composing = Boolean(event.nativeEvent.isComposing) || event.keyCode === 229
  if (!composing) syncSelection(controller, event.currentTarget)
  if (controller.key(event.key, event.shiftKey, event.ctrlKey || event.metaKey || event.altKey, composing))
    event.preventDefault()
}

export function handlePaste(controller: MentionController, event: PasteLike) {
  if (controller.getSnapshot().composing) return
  syncSelection(controller, event.currentTarget)
  if (controller.replaceSelection(event.clipboardData.getData('text/plain'), { paste: true })) event.preventDefault()
}
