import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

const media = '(min-width: 1040px)'
const wide = () => window.matchMedia?.(media).matches ?? true
function subscribe(notify: () => void) {
  const query = window.matchMedia?.(media)
  query?.addEventListener('change', notify)
  return () => query?.removeEventListener('change', notify)
}

export function useWidePanel() {
  return useSyncExternalStore(subscribe, wide, () => true)
}

export function useContextLayout(compare: boolean) {
  const desktop = useWidePanel()
  const [rail, setRail] = useState(true)
  const [sheet, setSheet] = useState(false)
  const docked = desktop && !compare
  return { docked, open: docked ? rail : sheet, toggle: () => docked ? setRail(!rail) : setSheet(!sheet),
    close: () => docked ? setRail(false) : setSheet(false) }
}

export function usePanelDialog(docked: boolean, open: boolean) {
  const modal = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const node = modal.current
    if (!node || !open) return
    if (docked) {
      node.open = true
      return () => { node.open = false }
    }
    node.showModal()
    return () => node.close()
  }, [open, docked])
  return modal
}
