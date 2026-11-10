/* Changing capability packs (edit, delete, save as pack) needs the admin password,
   like skill edits: the host answers 503 admin_disabled without ADMIN_PASSWORD and
   403 admin_required without the right X-Admin-Token. The token is the one entered
   on the Connect page; the API client sends it with every request. */

import { getConfig } from '../../config'
import { useSessionStore } from '../../store'

export interface PackGate {
  canEdit: boolean
  note: string          // why the controls are off; '' when they are on
}

export function packGate(admin: boolean, adminToken: string): PackGate {
  if (!admin) return { canEdit: false, note: 'This host has no admin password configured; packs are read-only here (running them still works).' }
  if (!adminToken) return { canEdit: false, note: 'Enter the admin password on the Connect page to edit, delete or save packs.' }
  return { canEdit: true, note: '' }
}

/* The gate for this host and this tab. */
export function usePackGate(): PackGate {
  const adminToken = useSessionStore(s => s.adminToken)
  return packGate(getConfig().features.admin, adminToken)
}
