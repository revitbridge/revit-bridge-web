/* The evidence page's state: the ledger list, the expanded record, the re-validation
   reports. Transport injected, so the page's moves run under vitest without fetch. */

import { createStore } from 'zustand/vanilla'
import type { EvidenceRecord, RevalidateReport } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import { BridgeApiError } from '../shared/http'

export interface EvidenceApi {
  evidence(limit: number, tool: string): Promise<EvidenceRecord[]>
  validateEvidence(id: string): Promise<RevalidateReport>
}

export type Revalidation =
  | { status: 'running' }
  | { status: 'done'; report: RevalidateReport }
  | { status: 'failed'; error: string; message: string }

export interface EvidenceState {
  records: EvidenceRecord[]
  loading: boolean
  notice: string
  tool: string
  limit: number
  expanded: string | null
  revalidations: Record<string, Revalidation>
}

export function createEvidenceFlow(api: EvidenceApi) {
  const store = createStore<EvidenceState>(() => ({
    records: [], loading: false, notice: '', tool: '', limit: 50, expanded: null, revalidations: {},
  }))
  const { getState, setState } = store

  async function load() {
    const { limit, tool } = getState()
    setState({ loading: true })
    try {
      setState({ records: await api.evidence(limit, tool), notice: '', loading: false })
    } catch (e: unknown) {
      setState({ records: [], notice: `Cannot list the evidence: ${getErrorMessage(e)}`, loading: false })
    }
  }

  const setFilter = (tool: string) => setState({ tool })

  const expand = (id: string | null) => setState(s => ({ expanded: s.expanded === id ? null : id }))

  /* Re-run the recorded assertion against the model as it is now. */
  async function validate(id: string) {
    if (getState().revalidations[id]?.status === 'running') return
    setState(s => ({ revalidations: { ...s.revalidations, [id]: { status: 'running' } } }))
    try {
      const report = await api.validateEvidence(id)
      setState(s => ({ revalidations: { ...s.revalidations, [id]: { status: 'done', report } } }))
    } catch (e: unknown) {
      const body = e instanceof BridgeApiError ? e.body : null
      setState(s => ({
        revalidations: { ...s.revalidations, [id]: { status: 'failed', error: body?.error ?? 'request_failed', message: body?.message ?? getErrorMessage(e) } },
      }))
    }
  }

  return { store, load, setFilter, expand, validate }
}

export type EvidenceFlow = ReturnType<typeof createEvidenceFlow>
