/* The rows of the Connect page's "Models by role": each role's model name as the host
   announces it (features.models), and the line saying where the names come from. */

import { ROLES, type RoleModels, type RoleName } from '../../config'

const ROLE_LABELS: Record<RoleName, { label: string; does: string }> = {
  analyst: { label: 'Analyst', does: 'sorts the request' },
  planner: { label: 'Planner', does: 'picks the tool and fills it in (the chat)' },
  supervisor: { label: 'Supervisor', does: 'reports the result' },
}

export const MODELS_FROM_FILE = "Set by the host's models file."
export const MODELS_FROM_ENV = "From the environment / this browser: the server's LLM_* settings, or the model settings below."

export interface RoleRow {
  role: RoleName
  label: string
  does: string
  model: string        // the name, or 'not set'
}

export function roleRows(models: RoleModels): RoleRow[] {
  return ROLES.map(role => ({ role, ...ROLE_LABELS[role], model: models[role] ?? 'not set' }))
}
