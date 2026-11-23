export type Role = 'analyst' | 'planner' | 'supervisor'

export interface StageEvent {
  type: 'stage'
  role: Role
  model: string
  status: 'start' | 'end'
}

export interface ToolEvent {
  type: 'tool'
  id: string
  role: Role
  name: string
  args: Record<string, unknown>
  status: 'running' | 'ok' | 'error'
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
