/* Drive one chat turn: fold the SSE events into the assistant's text.

   Every change of the text - tokens, a model error, the empty-reply fallback -
   goes through onUpdate, so what the user sees never depends on which event
   came last. Pure, so it can be unit-tested without React or fetch. */

import type { ChatEvent } from '../api/chat'

export interface ChatRunCallbacks {
  onSession: (id: string) => void
  onUpdate: (assistantText: string) => void
}

export const NO_RESPONSE = '(no response)'

export async function consumeChat(events: AsyncIterable<ChatEvent>, cb: ChatRunCallbacks): Promise<string> {
  let reply = ''
  for await (const evt of events) {
    if (evt.type === 'session') { cb.onSession(evt.id); continue }
    if (evt.type === 'error') {
      reply += `${reply ? '\n\n' : ''}**Model error:** ${evt.detail}`
      cb.onUpdate(reply)
      break
    }
    reply += evt.text
    cb.onUpdate(reply)
  }
  if (!reply) {
    reply = NO_RESPONSE
    cb.onUpdate(reply)
  }
  return reply
}
