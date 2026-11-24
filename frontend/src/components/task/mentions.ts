/* The chat's picks (@level, @type, @link): the picker's candidates from
   GET /api/v1/bridge/mentions, the picks as POST /api/chat takes them, and what the
   turn's `mentions` event says about each - the sent message shows it, and a pick the
   host found gone or not valid is marked in the input when it is used again. Nothing
   here decides what an item is or what the model gets: the host and the package do. */

import type { ChatMessage, MentionRef, MentionReport, SentMention } from '../../types/api'
import type { FetchCandidates, Mention, MentionCandidates, MentionKind } from '../../types/hostv2'
import { getErrorMessage, isAbortError } from '../../utils/errors'
import { BridgeApiError, request } from '../shared/http'

const B = '/api/v1/bridge'

/* A listing that hangs would hold Enter in the picker (it waits while candidates load):
   after this long it fails, and Enter sends again. */
export const MENTION_TIMEOUT_MS = 10_000

export const TIMEOUT_MESSAGE = 'Revit did not answer within 10 seconds. Keep typing, or press Enter to send.'

/* The candidates for one kind: the host's reply as it is. A reply that carries the
   package's `error` (Revit said no) fails with its message, as a refused request does
   (503: Revit is not connected). */
export async function fetchMentionCandidates(kind: MentionKind, opts: { category?: string; q?: string },
  timeoutMs = MENTION_TIMEOUT_MS): Promise<MentionCandidates> {
  const params = new URLSearchParams({ kind })
  if (opts.category) params.set('category', opts.category)
  if (opts.q) params.set('q', opts.q)
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const reply = await request<MentionCandidates & { error?: string; message?: string }>(
      `${B}/mentions?${params}`, { signal: controller.signal })
    if (reply.error) throw new Error(reply.message || reply.error)
    return reply
  } catch (e: unknown) {
    if (controller.signal.aborted || isAbortError(e)) throw new Error(TIMEOUT_MESSAGE)
    if (e instanceof BridgeApiError && e.status === 503) {
      throw new Error(`Revit is not connected, so there is nothing to pick: ${e.body?.message ?? e.message}`)
    }
    throw new Error(getErrorMessage(e))
  } finally {
    clearTimeout(timer)
  }
}

export const fetchCandidates: FetchCandidates = (kind, opts) => fetchMentionCandidates(kind, opts)

/* The most picks one message may carry (the host refuses more). */
export const MAX_PICKS = 20

/* Each model item once (by kind and id), in the order first picked. */
export function distinctMentions(mentions: Mention[]): Mention[] {
  const seen = new Set<string>()
  return mentions.filter(m => {
    const key = `${m.kind}#${m.id}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

/* Why a message cannot be sent with these picks, or null: more than MAX_PICKS different items. */
export function tooManyPicks(mentions: Mention[]): string | null {
  const count = distinctMentions(mentions).length
  return count > MAX_PICKS
    ? `This message picks ${count} model items; at most ${MAX_PICKS} can go with one message. Remove some tags, then send.`
    : null
}

/* The picks as POST /api/chat takes them: kind and element id, each item once. The name the
   page shows is not sent: the host reads the item from the model. */
export const mentionRefs = (mentions: Mention[]): MentionRef[] =>
  distinctMentions(mentions).map(m => ({ kind: m.kind, id: m.id }))

/* The picks of a message being sent, each item once: nothing is known about them yet. */
export const pendingMentions = (mentions: Mention[]): SentMention[] =>
  distinctMentions(mentions).map(m => ({ ...m, status: 'pending' }))

const sameItem = (kind: unknown, id: unknown, m: Mention) => kind === m.kind && id === m.id

/* Each pick of the sent message with what the turn's mentions event says about it. */
export function withReport(sent: SentMention[], report: MentionReport): SentMention[] {
  return sent.map(m => {
    if (report.resolved.some(item => sameItem(item.kind, item.id, m))) return { ...m, status: 'resolved', reason: undefined }
    for (const status of ['not_found', 'invalid', 'unresolved'] as const) {
      const problem = report[status].find(p => sameItem(p.kind, p.id, m))
      if (problem) return { ...m, status, reason: problem.reason }
    }
    return m
  })
}

/* The picks the host last found gone or not valid in this conversation (one per item;
   a later turn that verified it clears it). */
export function unavailableMentions(messages: ChatMessage[]): Mention[] {
  const latest = new Map<string, SentMention>()
  for (const message of messages) {
    for (const m of message.mentions ?? []) {
      if (m.status !== 'pending') latest.set(`${m.kind}#${m.id}`, m)
    }
  }
  return [...latest.values()]
    .filter(m => m.status === 'not_found' || m.status === 'invalid')
    .map(({ kind, id, name }) => ({ kind, id, name }))
}

/* Of the picks now in the input, those the host last found gone or not valid: the input marks
   them (and says so) only while they are there. */
export function unavailableInInput(messages: ChatMessage[], inInput: Mention[]): Mention[] {
  return unavailableMentions(messages).filter(m => inInput.some(p => p.kind === m.kind && p.id === m.id))
}

/* How a sent pick reads on its chip. */
export function mentionNote(m: SentMention): string {
  switch (m.status) {
    case 'resolved': return 'checked against the model'
    case 'not_found': return `not used: ${(m.reason ?? 'not_found').replace(/_/g, ' ')}`
    case 'invalid': return 'not used: not a model item'
    case 'unresolved': return `not checked: ${(m.reason ?? 'unresolved').replace(/_/g, ' ')}`
    default: return 'not checked yet'
  }
}
