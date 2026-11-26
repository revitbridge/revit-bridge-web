import { useEffect, useRef, useState } from 'react'
import { apiFetch } from '../../api/client'
import type { SelectionState } from '../../types/api'

type Reading = SelectionState & { assistant_set?: boolean }
export type SelectionSupport = 'ready' | 'legacy' | 'offline'

export function useSelectionState(device: string, support: SelectionSupport, paused: boolean, revision = 0) {
  const [result, setResult] = useState<{device: string; revision: number; value?: Reading; legacy?: boolean}>()
  const unsupported = useRef<string | null>(null)
  useEffect(() => {
    if (support === 'offline' || unsupported.current !== device) unsupported.current = null
    if (paused || support !== 'ready' || unsupported.current === device) return
    let controller: AbortController | undefined
    let timer: ReturnType<typeof setTimeout>
    let delay = 2000
    function stop() {
      clearTimeout(timer)
      controller?.abort()
    }
    async function poll() {
      if (document.hidden) return
      const run = new AbortController()
      controller = run
      try {
        const value = await apiFetch<Reading | {error: string}>('/api/v1/bridge/selection-state', { signal: run.signal })
        if (run.signal.aborted) return
        if ('error' in value) {
          if (value.error === 'unsupported_addin') {
            unsupported.current = device
            setResult({ device, revision, legacy: true })
            return
          }
          throw new Error(value.error)
        }
        setResult({ device, revision, value })
        delay = 2000
      } catch {
        if (run.signal.aborted) return
        setResult({ device, revision })
        delay = Math.min(delay * 2, 30000)
      }
      timer = setTimeout(poll, delay)
    }
    function changed() {
      stop()
      if (unsupported.current === device) return
      setResult(undefined)
      if (!document.hidden) void poll()
    }
    timer = setTimeout(poll, 0)
    document.addEventListener('visibilitychange', changed)
    return () => { stop(); document.removeEventListener('visibilitychange', changed) }
  }, [device, paused, support, revision])
  return !paused && support === 'ready' && result?.device === device && result.revision === revision ? result : undefined
}
