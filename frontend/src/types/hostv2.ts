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
