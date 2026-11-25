import { create } from 'zustand'

const key = 'rb_execution_mode'
function initial(): boolean {
  try { return localStorage.getItem(key) === 'auto' } catch { return false }
}
export const useExecutionMode = create<{ auto: boolean; set: (auto: boolean) => void }>(set => ({
  auto: initial(), set: auto => {
    try { localStorage.setItem(key, auto ? 'auto' : 'confirm') } catch { /* tab memory still works */ }
    set({ auto })
  },
}))
