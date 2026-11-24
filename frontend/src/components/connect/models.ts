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
export const ROLES_ON = "Each message goes to the analyst first; the planner runs only when a tool or a read is needed, " +
  "and the supervisor reports each execution. Model settings filled in below replace the planner's model for your own " +
  "requests (and the model of every role that shares the planner's entry)."
export const ROLES_OFF = "This host runs the chat as one model loop (LLM_SINGLE_MODEL=1): the planner's model does " +
  "everything, and the analyst and supervisor rows are not used. Model settings filled in below replace it for your own requests."
/* The default since the chat modes: planner + supervisor (ROLES_ANALYST=off). */
export const ROLES_NO_ANALYST = "Each message goes to the planner, and the supervisor reports each execution; the analyst " +
  "runs only in the mode Analyst + planner + supervisor (ROLES_ANALYST=on on the host, or the task page's Mode menu " +
  "where the host offers it). Model settings filled in below replace the planner's model for your own requests (and " +
  "the model of every role that shares the planner's entry)."

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
