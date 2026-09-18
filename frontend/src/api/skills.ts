/* /api/skills - list/read for everyone, edits need the admin token. */

import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from './client'
import type { Skill } from '../types/api'

const enc = (id: string) => id.split('/').map(encodeURIComponent).join('/')

export const skillsApi = {
  list: () => apiGet<{ skills: Skill[] }>('/api/skills').then(r => r.skills),
  get: (id: string) => apiGet<Skill>(`/api/skills/${enc(id)}`),
  create: (payload: { name: string; description: string; author: string; content: string }) =>
    apiPost<Skill>('/api/skills', payload),
  update: (id: string, payload: { name?: string; description?: string; content?: string }) =>
    apiPut<Skill>(`/api/skills/${enc(id)}`, payload),
  toggle: (id: string, enabled: boolean) => apiPatch<Skill>(`/api/skills/${enc(id)}`, { enabled }),
  remove: (id: string) => apiDelete<{ status: string; deleted: string }>(`/api/skills/${enc(id)}`),
  importFromGitHub: (url: string) => apiPost<Skill & { imported_from: string }>('/api/skills/import', { url }),
}
