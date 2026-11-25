/* Page helpers retain the parsed bridge error body and accept per-request device headers. */

import { apiFetch as request } from '../../api/client'
export { BridgeApiError, apiFetch as request } from '../../api/client'

export const get = <T>(path: string, headers?: Record<string, string>) => request<T>(path, { headers })
export const post = <T>(path: string, body: unknown, headers?: Record<string, string>) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body), headers })
export const del = <T>(path: string, headers?: Record<string, string>) =>
  request<T>(path, { method: 'DELETE', headers })
