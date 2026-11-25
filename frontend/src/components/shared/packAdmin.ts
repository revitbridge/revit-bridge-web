/* Changing capability packs (edit, delete, save as pack) needs the admin password on a
   cloud host, like skill edits: the host answers 503 admin_disabled without
   ADMIN_PASSWORD and 403 admin_required without the right X-Admin-Token. The token
   is the one entered in Settings; the API client sends it with every request.
   A local host has no admin password: the controls are simply on. */

import { getConfig, type DeployMode } from '../../config'
import { useSessionStore } from '../../store'

export interface PackGate {
  canEdit: boolean
  note: string          // why the controls are off; '' when they are on
}

export function packGate(admin: boolean, adminToken: string, mode: DeployMode = 'cloud'): PackGate {
  if (mode === 'local') return { canEdit: true, note: '' }
  if (!admin) return { canEdit: false, note: 'This host has no admin password configured; packs are read-only here (running them still works).' }
  if (!adminToken) return { canEdit: false, note: 'Enter the admin password in Settings to edit, delete or save packs.' }
  return { canEdit: true, note: '' }
}

/* The gate for this host and this tab. */
export function usePackGate(): PackGate {
  const adminToken = useSessionStore(s => s.adminToken)
  const { admin, mode } = getConfig().features
  return packGate(admin, adminToken, mode)
}
