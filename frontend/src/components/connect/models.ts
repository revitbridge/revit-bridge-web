/* The rows of the Connect page's "Models by role": each role's model name as the host
   announces it (features.models), and the line saying where the names come from. */

import { ROLES, type RoleModels, type RoleName } from '../../config'

const ROLE_LABELS: Record<RoleName, { label: string; does: string }> = {
  analyst: { label: 'Analyst', does: 'sorts the request' },
  planner: { label: 'Planner', does: 'picks the tool and fills it in (the chat)' },
  supervisor: { label: 'Supervisor', does: 'reports the result' },
}

export const MODELS_FROM_FILE = "Host models use the configured role mappings."
export const MODELS_FROM_ENV = "Host models can be configured in Settings."
export const ROLES_ON = "The analyst routes each message, the planner chooses tools, and the supervisor reports the result."
export const ROLES_OFF = "The planner handles every step in single-model mode."
export const ROLES_NO_ANALYST = "The planner handles messages and the supervisor reports each execution."

/* The line under the table: what runs by default (features.roles, features.chatModeDefault; a host
   from before the modes says only roles, which then meant the analyst too). */
export function rolesLine(roles: boolean, chatModeDefault: string | null | undefined): string {
  if (!roles || chatModeDefault === 'single') return ROLES_OFF
  return chatModeDefault === 'roles' ? ROLES_NO_ANALYST : ROLES_ON
}

export interface RoleRow {
  role: RoleName
  label: string
  does: string
  model: string        // the name, or 'not set'
}

export function roleRows(models: RoleModels): RoleRow[] {
  return ROLES.map(role => ({ role, ...ROLE_LABELS[role], model: models[role] ?? 'not set' }))
}
