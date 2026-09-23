/* The page-side request helper: api/client.ts, but the rejected body survives.

   The pages show what the bridge refused in its own words - the spec errors of
   /spec/confirm, the pack problems of /solidify, unknown_device or
   invalid_device_key from the device routes - so the error carries the parsed
   body, not only a sentence. Folded into api/ with components/task/api.ts. */

import { describeFailure } from '../../api/client'
import { getConfig } from '../../config'
import { adminHeaders, deviceHeaders } from '../../store'
import type { ApiErrorBody } from '../../types/api'

export class BridgeApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody | null

  constructor(status: number, raw: string, contentType = '') {
    super(describeFailure(status, raw, contentType))
    this.name = 'BridgeApiError'
    this.status = status
    this.body = parseBody(raw)
  }

  /* The bridge's error code, when the body carried one. */
  get code(): string | null {
    return this.body && typeof this.body.error === 'string' ? this.body.error : null
  }
}

function parseBody(raw: string): ApiErrorBody | null {
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && typeof parsed.error === 'string' ? parsed : null
  } catch {
    return null
  }
}

/* Same base URL and session headers as api/client.ts; `headers` overrides them,
   which is how a device is driven by its own key before it is the selected one. */
export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${getConfig().apiBase}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...deviceHeaders(), ...adminHeaders(), ...init?.headers },
  })
  if (!resp.ok) {
    const raw = await resp.text().catch(() => '')
    throw new BridgeApiError(resp.status, raw, resp.headers.get('content-type') || '')
  }
  return resp.json()
}

export const get = <T>(path: string, headers?: Record<string, string>) => request<T>(path, { headers })
export const post = <T>(path: string, body: unknown, headers?: Record<string, string>) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body), headers })
export const del = <T>(path: string, headers?: Record<string, string>) =>
  request<T>(path, { method: 'DELETE', headers })
