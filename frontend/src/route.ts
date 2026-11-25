export const WORKBENCH_PATH = '/capabilities/workbench'
export const AUTHORING_PATH = '/capabilities/authoring'
export const TOKEN_PATH = '/token'
export const TABS = ['Chat', 'Tools', 'Evidence', 'Settings'] as const
export type Tab = typeof TABS[number]
export type CapabilitiesView = 'packs' | 'workbench' | 'authoring' | 'skills'
export type SettingsView = 'models' | 'connection' | 'usage'
export interface View {
  tab: Tab
  capabilities: CapabilitiesView
  settings?: SettingsView
}

export function viewOf(pathname: string): View {
  const path = pathname.replace(/\/+$/, '').toLowerCase()
  if (path === WORKBENCH_PATH) return { tab: 'Tools', capabilities: 'workbench' }
  if (path === AUTHORING_PATH) return { tab: 'Tools', capabilities: 'authoring' }
  if (path === '/tools') return { tab: 'Tools', capabilities: 'packs' }
  if (path === '/evidence') return { tab: 'Evidence', capabilities: 'packs' }
  if (path === TOKEN_PATH || path === '/settings' || path === '/settings/connection') {
    return { tab: 'Settings', capabilities: 'packs', settings: path === TOKEN_PATH ? 'usage' : path === '/settings' ? 'models' : 'connection' }
  }
  return { tab: 'Chat', capabilities: 'packs' }
}

export function pathOf(view: View): string {
  switch (view.tab) {
    case 'Tools': return view.capabilities === 'workbench' ? WORKBENCH_PATH : view.capabilities === 'authoring' ? AUTHORING_PATH : '/tools'
    case 'Evidence': return '/evidence'
    case 'Settings': return view.settings === 'usage' ? TOKEN_PATH : view.settings === 'connection' ? '/settings/connection' : '/settings'
    case 'Chat': return '/'
  }
}

export function currentPath(): string {
  return typeof window === 'undefined' ? '/' : window.location.pathname
}

export function showPath(view: View): void {
  if (typeof window === 'undefined') return
  const path = pathOf(view)
  if (window.location.pathname !== path) window.history.replaceState(window.history.state, '', path)
}
