export const WORKBENCH_PATH = '/capabilities/workbench'
export const AUTHORING_PATH = '/capabilities/authoring'
export const TOKEN_PATH = '/token'
export const TABS = ['Chat', 'Tools', 'Workflows', 'Evidence', 'Settings'] as const
export type Tab = typeof TABS[number]
export type CapabilitiesView = 'packs' | 'workbench' | 'authoring' | 'skills'
export type SettingsView = 'models' | 'connection' | 'usage' | 'remote-mcp'
export interface View {
  tool?: string
  brief?: string
  tab: Tab
  capabilities: CapabilitiesView
  settings?: SettingsView
}

export function viewOf(pathname: string): View {
  const [base, query] = pathname.split('?')
  const path = base.replace(/\/+$/, '').toLowerCase()
  const name = new URLSearchParams(query).get('tool')
  const tool = name && /^[a-zA-Z_][a-zA-Z_0-9]{0,99}$/.test(name) ? { tool: name } : {}
  if (path === WORKBENCH_PATH) return { tab: 'Tools', capabilities: 'workbench' }
  if (path === AUTHORING_PATH) return { tab: 'Tools', capabilities: 'authoring' }
  if (path === '/tools') return { tab: 'Tools', capabilities: 'packs', ...tool }
  if (path === '/workflows') return { tab: 'Workflows', capabilities: 'packs' }
  if (path === '/evidence') return { tab: 'Evidence', capabilities: 'packs' }
  if (path === '/settings/remote-mcp') return { tab: 'Settings', capabilities: 'packs', settings: 'remote-mcp' }
  if (path === TOKEN_PATH || path === '/settings' || path === '/settings/connection') {
    return { tab: 'Settings', capabilities: 'packs', settings: path === TOKEN_PATH ? 'usage' : path === '/settings' ? 'models' : 'connection' }
  }
  return { tab: 'Chat', capabilities: 'packs', ...tool }
}

export function pathOf(view: View): string {
  const linked = view.tool ? '?tool=' + encodeURIComponent(view.tool) : ''
  switch (view.tab) {
    case 'Tools': return view.capabilities === 'workbench' ? WORKBENCH_PATH : view.capabilities === 'authoring' ? AUTHORING_PATH : '/tools' + linked
    case 'Evidence': return '/evidence'
    case 'Workflows': return '/workflows'
    case 'Settings': return view.settings === 'usage' ? TOKEN_PATH : view.settings && view.settings !== 'models' ? '/settings/' + view.settings : '/settings'
    case 'Chat': return '/' + linked
  }
}

export function currentPath(): string {
  return typeof window === 'undefined' ? '/' : window.location.pathname + window.location.search
}

export function showPath(view: View): void {
  if (typeof window === 'undefined') return
  const path = pathOf(view)
  if (currentPath() !== path) window.history.replaceState(window.history.state, '', path)
}
