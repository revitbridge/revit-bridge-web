export type Role = 'analyst' | 'planner' | 'supervisor'

/* 'interrupted' never comes from the server: the page sets it on a stage or tool that was
   still open when its turn's stream ended (aborted, broken off). */
export interface StageEvent {
  type: 'stage'
  role: Role
  model: string
  status: 'start' | 'end' | 'interrupted'
}

/* args: the arguments as the model gave them, or, when their JSON text is long,
   {_truncated: true, _chars: <length>, _head: <first 2000 characters>}. */
export interface ToolEvent {
  type: 'tool'
  id: string
  role: Role
  name: string
  args: Record<string, unknown>
  status: 'running' | 'ok' | 'error' | 'interrupted'
  ms?: number
  size?: number
  preview?: string
  error?: string
}

export interface UsageEvent {
  type: 'usage'
  role: Role
  model: string
  round: number
  input: number | null
  output: number | null
  cache_read: number | null
  cache_write: number | null
  ms: number
}

export type TraceEvent = StageEvent | ToolEvent | UsageEvent

// X3c: mention input contract. The other hostv2 packages append their own types.
export type MentionKind = 'level' | 'type' | 'link'
export interface MentionItem {
  kind: string
  id: number | string
  name: string
  label: string
  detail: string
  category?: string
  family?: string
  disabled?: boolean
}
export interface MentionCandidates {
  kind: string
  items: MentionItem[]
  total: number
  truncated: boolean
}
export interface Mention { kind: MentionKind; id: number; name: string }
export type FetchCandidates =
  (kind: MentionKind, opts: { category?: string; q?: string }) => Promise<MentionCandidates>
