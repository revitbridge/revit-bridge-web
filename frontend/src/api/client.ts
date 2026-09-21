/* Base API client: apiBase comes from the runtime config, headers from the session. */

import { getConfig } from '../config'
import { adminHeaders, slotHeaders } from '../store'

function url(path: string): string {
  return `${getConfig().apiBase}${path}`
}

/* The message for a failed response: the bridge's {error, message?} body, FastAPI's
   {detail} (a string or a list), or the raw text; an HTML page means no backend. */
export function describeFailure(status: number, raw: string, contentType = ''): string {
  const isHtml = raw.trimStart().startsWith('<') || contentType.includes('text/html')
  if (isHtml) return `${status}: backend unreachable (HTML page instead of JSON)`
  let detail = raw.slice(0, 400)
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object') {
      if (typeof parsed.message === 'string' && parsed.message) {
        detail = typeof parsed.error === 'string' ? `${parsed.error}: ${parsed.message}` : parsed.message
      } else if (typeof parsed.error === 'string') {
        detail = parsed.error
      } else if (typeof parsed.detail === 'string') {
        detail = parsed.detail
      } else if (parsed.detail) {
        detail = JSON.stringify(parsed.detail)
      }
    }
  } catch { /* keep raw text */ }
  return `${status}: ${detail}`
}

async function fail(resp: Response): Promise<never> {
  const raw = await resp.text().catch(() => '')
  throw new Error(describeFailure(resp.status, raw, resp.headers.get('content-type') || ''))
}

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url(path), {
    ...init,
    headers: { 'Content-Type': 'application/json', ...slotHeaders(), ...adminHeaders(), ...init?.headers },
  })
  if (!resp.ok) await fail(resp)
  return resp.json()
}

export const apiGet = <T = unknown>(path: string) => apiFetch<T>(path)
export const apiPost = <T = unknown>(path: string, body: unknown) =>
  apiFetch<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const apiPut = <T = unknown>(path: string, body: unknown) =>
  apiFetch<T>(path, { method: 'PUT', body: JSON.stringify(body) })
export const apiPatch = <T = unknown>(path: string, body: unknown) =>
  apiFetch<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
export const apiDelete = <T = unknown>(path: string) => apiFetch<T>(path, { method: 'DELETE' })

/* Raw fetch for streaming endpoints (caller reads the body). */
export async function apiStream(path: string, body: unknown, extraHeaders: Record<string, string>, signal?: AbortSignal): Promise<Response> {
  const resp = await fetch(url(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...slotHeaders(), ...extraHeaders },
    body: JSON.stringify(body),
    signal,
  })
  if (!resp.ok) await fail(resp)
  return resp
}
