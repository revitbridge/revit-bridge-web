import { apiDelete, apiFetch, apiGet, apiPost, apiPut, BridgeApiError } from './client'
import { getConfig } from '../config'
import { deviceHeaders, llmHeaders } from '../store'
import type { Language } from '../utils/language'

export type Literal = string | number | boolean
export type Binding = Literal | { input: string } | { from: string; path: string }
export interface WorkflowStep { id: string; title: string; tool: string; params: Record<string, Binding> }
export interface WorkflowInput { name: string; type: string; description: string; ask: boolean }
export interface Workflow {
  schema_version: 1; kind: 'workflow'; name: string; display_name: string; summary: string; description: string
  version: string; inputs: WorkflowInput[]; steps: WorkflowStep[]
}
export type TextSuggestion = Pick<Workflow, 'summary' | 'description'> & { model: string }

const base = '/api/v1/bridge/workflows'
const path = (name: string) => base + '/' + encodeURIComponent(name)
export const workflowApi = {
  list: () => apiGet<Workflow[]>(base),
  validate: (w: Workflow) => apiPost<{ errors: string[] }>(base + '/validate', w),
  optimizeText: (definition: Workflow, language: Language, signal: AbortSignal) =>
    apiFetch<TextSuggestion>(base + '/optimize-text', {
      method: 'POST', body: JSON.stringify({ definition, language }), headers: llmHeaders(), signal,
    }),
  save: (w: Workflow, overwrite: boolean) => apiPut<Workflow>(path(w.name) + '?overwrite=' + overwrite, w),
  remove: (name: string) => apiDelete(path(name)),
  importYaml: (yaml: string, overwrite: boolean) => apiFetch<Workflow>(base + '/import?overwrite=' + overwrite,
    { method: 'POST', body: yaml, headers: { 'Content-Type': 'text/yaml' } }),
  resultPaths: (tool: string) => apiGet<string[]>(base + '/tools/' + encodeURIComponent(tool) + '/result-paths'),
  exportYaml: async (name: string) => {
    const r = await fetch(getConfig().apiBase + path(name) + '/export', { headers: deviceHeaders() })
    const text = await r.text()
    if (!r.ok) throw new BridgeApiError(r.status, text)
    return text
  },
}
