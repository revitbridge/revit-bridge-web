/* The v1 endpoints the task and evidence pages need beyond api/bridge.ts.

   Same base URL and session headers as api/client.ts; the one difference is
   that a rejected request keeps its body (TaskApiError.body) because the
   pages show the spec errors of /spec/confirm and the pack problems of
   /solidify line by line. Folded into api/bridge.ts before 6.5. */

import { describeFailure } from '../../api/client'
import { getConfig } from '../../config'
import { adminHeaders, slotHeaders } from '../../store'
import type {
  ApiErrorBody, ConfirmResponse, EvidenceRecord, ExecutionResult, Question, ReconcileResult, RevalidateReport,
  SolidifyRequest, SolidifyResponse, TaskSpec,
} from '../../types/api'

const B = '/api/v1/bridge'
const enc = encodeURIComponent

export class TaskApiError extends Error {
  readonly status: number
  readonly body: ApiErrorBody | null

  constructor(status: number, raw: string, contentType = '') {
    super(describeFailure(status, raw, contentType))
    this.name = 'TaskApiError'
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${getConfig().apiBase}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...slotHeaders(), ...adminHeaders(), ...init?.headers },
  })
  if (!resp.ok) {
    const raw = await resp.text().catch(() => '')
    throw new TaskApiError(resp.status, raw, resp.headers.get('content-type') || '')
  }
  return resp.json()
}

const get = <T>(path: string) => request<T>(path)
const post = <T>(path: string, body: unknown) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const taskApi = {
  /* The questions still open for a pack given the values already known, with the real options. */
  missingParams: (name: string, known: Record<string, unknown>, snapshot?: unknown, language?: string) =>
    post<Question[]>(`${B}/tools/${enc(name)}/missing-params`, { known, snapshot: snapshot ?? null, ...(language ? { language } : {}) }),

  /* The draft against the model; without a snapshot the server takes one. */
  reconcile: (spec: TaskSpec, snapshot?: unknown) =>
    post<ReconcileResult>(`${B}/spec/reconcile`, { spec, snapshot: snapshot ?? null }),

  /* The designer confirmed the card: a one-time token bound to exactly this spec. */
  confirm: (spec: TaskSpec, confirmedBy = 'designer') =>
    post<ConfirmResponse>(`${B}/spec/confirm`, { spec, confirmed_by: confirmedBy }),

  /* Confirmed execution: values as they are in the spec (the projection hash must match). */
  runTool: (name: string, params: Record<string, unknown>, token: string) =>
    post<ExecutionResult>(`${B}/tools/${enc(name)}/run`, { params, token }),
  execute: (code: string, parameters: unknown[], token: string) =>
    post<ExecutionResult>(`${B}/execute`, { code, parameters, token }),

  /* Code that worked, saved as a v1 pack. */
  solidify: (payload: SolidifyRequest) => post<SolidifyResponse>(`${B}/solidify`, payload),

  /* The ledger and the re-run of one record's assertion. */
  evidence: (limit = 50, tool = '') =>
    get<EvidenceRecord[]>(`${B}/evidence?limit=${limit}${tool ? `&tool=${enc(tool)}` : ''}`),
  validateEvidence: (id: string) => post<RevalidateReport>(`${B}/evidence/${enc(id)}/validate`, {}),
}

export type TaskApi = typeof taskApi
