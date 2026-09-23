/* The v1 endpoints the task and evidence pages need beyond api/bridge.ts.

   The request helper is components/shared/http.ts: api/client.ts with the
   rejected body kept, because the pages show the spec errors of /spec/confirm
   and the pack problems of /solidify line by line. Folded into api/bridge.ts
   with the device helpers. */

import type {
  ConfirmResponse, EvidenceRecord, ExecutionResult, Question, ReconcileResult, RevalidateReport,
  SolidifyRequest, SolidifyResponse, TaskSpec,
} from '../../types/api'
import { get, post } from '../shared/http'

const B = '/api/v1/bridge'
const enc = encodeURIComponent

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
