/* The v1 endpoints the task and evidence pages need beyond api/bridge.ts.

   The request helper is components/shared/http.ts: api/client.ts with the
   rejected body kept, because the pages show the spec errors of /spec/confirm
   line by line. Folded into api/bridge.ts with the device helpers. */

import type {
  ConfirmResponse, EvidenceRecord, ExecutionResult, Question, ReconcileResult, RevalidateReport, TaskSpec,
} from '../../types/api'
import { get, post } from '../shared/http'
import type { FlowApi } from './flow'

const B = '/api/v1/bridge'
const enc = encodeURIComponent
const session = (sessionId?: string | null) => (sessionId ? { session_id: sessionId } : {})

export const taskApi = {
  /* The questions still open for a pack given the values already known, with the real options. */
  missingParams: (name: string, known: Record<string, unknown>, snapshot?: unknown, language?: string) =>
    post<Question[]>(`${B}/tools/${enc(name)}/missing-params`, { known, snapshot: snapshot ?? null, ...(language ? { language } : {}) }),

  /* The draft against the model; without a snapshot the server takes one. sessionId: the
     chat session the spec comes from, so a parameter bound to a pick checked there is accepted. */
  reconcile: (spec: TaskSpec, snapshot?: unknown, sessionId?: string | null) =>
    post<ReconcileResult>(`${B}/spec/reconcile`, { spec, snapshot: snapshot ?? null, ...session(sessionId) }),

  /* The designer confirmed the card: a one-time token bound to exactly this spec. */
  confirm: (spec: TaskSpec, confirmedBy = 'designer', sessionId?: string | null) =>
    post<ConfirmResponse>(`${B}/spec/confirm`, { spec, confirmed_by: confirmedBy, ...session(sessionId) }),

  /* Confirmed execution: values as they are in the spec (the projection hash must match). */
  runTool: (name: string, params: Record<string, unknown>, token: string) =>
    post<ExecutionResult>(`${B}/tools/${enc(name)}/run`, { params, token }),

  /* The ledger and the re-run of one record's assertion. */
  evidence: (limit = 50, tool = '') =>
    get<EvidenceRecord[]>(`${B}/evidence?limit=${limit}${tool ? `&tool=${enc(tool)}` : ''}`),
  validateEvidence: (id: string) => post<RevalidateReport>(`${B}/evidence/${enc(id)}/validate`, {}),
}

export type TaskApi = typeof taskApi

/* The task page's flow (components/task/flow.ts): the chat session goes with every spec. */
export const flowApi: FlowApi = {
  reconcile: (spec, sessionId) => taskApi.reconcile(spec, undefined, sessionId),
  confirm: (spec, sessionId) => taskApi.confirm(spec, 'designer', sessionId),
  runTool: (name, params, token) => taskApi.runTool(name, params, token),
}
