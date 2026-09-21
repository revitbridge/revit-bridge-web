/* /api/v1/bridge/* - every call goes through the package on the server. */

import { apiDelete, apiGet, apiPost, apiPut } from './client'
import type {
  ExecutionResponse, ProjectSnapshot, QueryAnswer, RevitHealthResponse, SlotsStatus, SolidifyResponse,
  ToolChoiceItem, ToolDetail, ToolInfo, ToolParam, ToolUpdatePayload,
} from '../types/api'

const B = '/api/v1/bridge'
const enc = encodeURIComponent

export const bridgeApi = {
  revitHealth: () => apiGet<RevitHealthResponse>(`${B}/revit-health`),
  slots: () => apiGet<SlotsStatus>(`${B}/slots`),

  /* Read-only model access: the package's snapshot and query(kind, args). */
  snapshot: (categories?: string[]) =>   // undefined: the default categories; []: no family types
    apiGet<ProjectSnapshot>(`${B}/snapshot${categories ? `?categories=${enc(categories.join(','))}` : ''}`),
  query: (kind: string, args: Record<string, unknown> = {}) => apiPost<QueryAnswer>(`${B}/query`, { kind, args }),

  execute: (code: string) => apiPost<ExecutionResponse>(`${B}/execute`, { code }),

  solidify: (payload: { name: string; code: string; description: string; parameters: ToolParam[]; tags: string[]; source_query: string }) =>
    apiPost<SolidifyResponse>(`${B}/solidify`, payload),

  listTools: () => apiGet<ToolInfo[]>(`${B}/tools`),
  getTool: (name: string) => apiGet<ToolDetail>(`${B}/tools/${enc(name)}`),
  updateTool: (name: string, payload: ToolUpdatePayload) => apiPut<ToolDetail & { revit_synced: boolean }>(`${B}/tools/${enc(name)}`, payload),
  deleteTool: (name: string) => apiDelete<{ status: string; name: string }>(`${B}/tools/${enc(name)}`),
  getToolChoices: (name: string) => apiGet<Record<string, ToolChoiceItem[]>>(`${B}/tools/${enc(name)}/choices`),
  runTool: (name: string, params: Record<string, string>) => apiPost<ExecutionResponse>(`${B}/tools/${enc(name)}/run`, { params }),

  triggerSelection: () => apiPost<{ elements: Array<Record<string, unknown>> }>(`${B}/trigger-selection`, {}),
}
