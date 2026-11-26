/* The Token page's state: the runs, the two picked for comparison. Transport injected, so
   the page's moves run under vitest without fetch. */

import { uiText } from '../../utils/strings'
import { createStore } from 'zustand/vanilla'
import type { UsageRun } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import { toggleSelection } from './runs'

export interface UsageApi {
  runs(): Promise<UsageRun[]>
}

export interface TokenState {
  runs: UsageRun[] | null      // null until the first answer
  selected: string[]           // run labels, in the order they were picked (A, then B)
  loading: boolean
  notice: string
}

export function createTokenFlow(api: UsageApi) {
  const store = createStore<TokenState>(() => ({ runs: null, selected: [], loading: false, notice: '' }))
  const { getState, setState } = store

  async function load() {
    setState({ loading: true })
    try {
      const runs = await api.runs()
      const labels = new Set(runs.map(run => run.run_label))
      // A picked run that is gone (aged out of the log) is no longer picked.
      setState(s => ({ runs, selected: s.selected.filter(label => labels.has(label)), notice: '', loading: false }))
    } catch (e: unknown) {
      setState({ notice: uiText("Cannot read the runs: {0}", getErrorMessage(e)), loading: false })
    }
  }

  const toggle = (label: string) => setState(s => ({ selected: toggleSelection(s.selected, label) }))

  /* The two picked runs, A first; null until two are picked. */
  function pair(state: TokenState = getState()): [UsageRun, UsageRun] | null {
    if (state.selected.length !== 2 || !state.runs) return null
    const [a, b] = state.selected.map(label => state.runs!.find(run => run.run_label === label))
    return a && b ? [a, b] : null
  }

  return { store, load, toggle, pair }
}

export type TokenFlow = ReturnType<typeof createTokenFlow>
