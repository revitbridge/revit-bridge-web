/* Base API client: apiBase comes from the runtime config, headers from the session. */

import { getConfig } from '../config'
import { adminHeaders, slotHeaders } from '../store'

function url(path: string): string {
  return `${getConfig().apiBase}${path}`
}

async function fail(resp: Response): Promise<never> {
  const raw = await resp.text().catch(() => '')
  const isHtml = raw.trimStart().startsWith('<') || (resp.headers.get('content-type') || '').includes('text/html')
  if (isHtml) throw new Error(`${resp.status}: backend unreachable (HTML page instead of JSON)`)
  let detail = raw.slice(0, 400)
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed.detail === 'string') detail = parsed.detail
    else if (parsed && parsed.detail) detail = JSON.stringify(parsed.detail)
  } catch { /* keep raw text */ }
  throw new Error(`${resp.status}: ${detail}`)
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
