/* The page's addresses. The tabs live at '/'; the workbench also has a path of its own,
   /capabilities/workbench, so a link opens it - the one the chat model writes when no saved
   tool fits (a relative Markdown link, opened in a new tab), a bookmark, a typed address. The
   server answers every such path with the page itself. */

export const WORKBENCH_PATH = '/capabilities/workbench'

export const TABS = ['Connect', 'Task', 'Capabilities', 'Evidence'] as const
export const CAPABILITIES_TAB = 2

export type CapabilitiesView = 'packs' | 'workbench' | 'skills'

export interface View {
  tab: number
  capabilities: CapabilitiesView
}

/* What a path opens: the workbench for its path (a trailing slash or any case too), the first
   tab for everything else, as before. */
export function viewOf(pathname: string): View {
  const path = pathname.replace(/\/+$/, '').toLowerCase()
  if (path === WORKBENCH_PATH) return { tab: CAPABILITIES_TAB, capabilities: 'workbench' }
  return { tab: 0, capabilities: 'packs' }
}

/* The address a view is shown at: the workbench's own path, '/' for the rest. */
export function pathOf(view: View): string {
  return view.tab === CAPABILITIES_TAB && view.capabilities === 'workbench' ? WORKBENCH_PATH : '/'
}

/* The address bar's path; '/' where there is none (server rendering in tests). */
export function currentPath(): string {
  return typeof window === 'undefined' ? '/' : window.location.pathname
}

/* Show the view's address without a new history entry or a reload. */
export function showPath(view: View): void {
  if (typeof window === 'undefined') return
  const path = pathOf(view)
  if (window.location.pathname !== path) window.history.replaceState(window.history.state, '', path)
}
