/* /api/usage - the model calls the chat made; admin only on a cloud host (the API client
   sends the admin token entered in Settings or the Token page). */

import { apiGet } from './client'
import type { UsageRun } from '../types/api'

export const usageApi = {
  runs: () => apiGet<{ items: UsageRun[] }>('/api/usage/runs').then(r => r.items),
}
