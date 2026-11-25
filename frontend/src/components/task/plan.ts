import type { PlanStatus } from '../../types/api'

const states = {
  pending: ['○', 'Pending', '待处理'],
  proposed: ['◉', 'Card proposed', '已出卡'],
  done: ['✓', 'Completed', '已完成'],
  failed: ['!', 'Failed', '失败'],
  skipped: ['–', 'Skipped', '已跳过'],
} as const

export function planStatus(status: PlanStatus, zh: boolean) {
  const [icon, english, chinese] = states[status]
  return { icon, label: zh ? chinese : english }
}
